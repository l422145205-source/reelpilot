"""Controlled journal records verify ETL contracts, not user/model quality."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import time

import pytest

from reelpilot.admission import Admission
from reelpilot.analytics import DataQualityError, ingest_journal, report_warehouse
from reelpilot.journal import Journal


def collect(journal, target, source_id='local', **kwargs):
    return ingest_journal(journal.db, target, source_id=source_id, **kwargs)


def read(target, sql):
    with sqlite3.connect(target) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(sql)]


def request(journal, name='job', **body):
    return Admission(journal).reserve(name, {'provider': 'explicit', 'mode': 'lexical', **body})


def test_receipt_grain_prevents_fanout_and_repeated_collection(tmp_path):
    j = Journal(tmp_path/'source')
    t = request(j)
    j.create('job', {'fixture': True})
    owner = j.claim('job', request_owner=t.owner)
    j.stage('job', owner, 'render', {}, lambda: {'fixture': True})
    j.stage('job', owner, 'render', {}, lambda: pytest.fail('must hit cache'))
    j.release('job', owner)
    t.finish()
    w = tmp_path/'mart.sqlite'
    first = collect(j, w)
    again = collect(j, w)
    assert first['changed_requests'] == first['changed_stages'] == 1
    assert (again['inserted_events'], again['changed_requests'], again['changed_stages']) == (0, 0, 0)
    rows = report_warehouse(w)['daily']
    assert len(rows) == 1
    assert rows[0]['logical_requests'] == rows[0]['api_attempts'] == 1
    assert rows[0]['stage_commits'] == rows[0]['cache_hits'] == rows[0]['render_commits'] == 1


def test_retry_updates_one_logical_request_and_latest_attempt_latency(tmp_path):
    j = Journal(tmp_path/'source')
    request(j).finish(error='controlled fixture failure')
    w = tmp_path/'mart.sqlite'
    collect(j, w)
    t = request(j)
    t.finish()
    result = collect(j, w)
    assert result['changed_requests'] == 1
    row = report_warehouse(w)['daily'][0]
    assert row['logical_requests'] == row['resubmitted_requests'] == row['complete_after_resubmission'] == 1
    assert row['api_attempts'] == 2 and row['failed'] == 0 and row['complete'] == 1
    latency = report_warehouse(w)['last_terminal_api_attempt_latency']
    assert latency['measured_requests'] == 1 and latency['p50_seconds'] >= 0


def test_mutable_state_updates_even_without_new_events(tmp_path):
    j = Journal(tmp_path/'source')
    t = request(j)
    w = tmp_path/'mart.sqlite'
    collect(j, w)
    with j.connect() as db:
        db.execute("UPDATE submissions SET status='failed',phase='failed',lease_until=0,updated=updated+1")
    result = collect(j, w)
    assert result['changed_requests'] == 1 and result['inserted_events'] == 0
    assert report_warehouse(w)['daily'][0]['failed'] == 1
    t.stop()


def test_late_event_uses_sequence_not_timestamp(tmp_path):
    j = Journal(tmp_path/'source')
    request(j).finish()
    w = tmp_path/'mart.sqlite'
    first = collect(j, w)
    with j.connect() as db:
        db.execute("INSERT INTO events(job,event,detail,at) VALUES('job','cache_hit','render',1)")
    second = collect(j, w)
    assert second['event_cursor'] == first['event_cursor'] + 1 and second['inserted_events'] == 1
    assert report_warehouse(w)['daily'][0]['cache_hits'] == 1


@pytest.mark.parametrize('mutation', ['edit', 'delete', 'replace', 'decrease', 'missing'])
def test_history_rewrites_preserve_last_good_mart(tmp_path, mutation):
    j = Journal(tmp_path/'source')
    request(j).finish()
    w = tmp_path/'mart.sqlite'
    collect(j, w)
    previous = report_warehouse(w)
    with j.connect() as db:
        if mutation == 'edit':
            db.execute("UPDATE events SET detail='changed' WHERE seq=1")
        elif mutation == 'delete':
            db.execute('DELETE FROM events WHERE seq=1')
        elif mutation == 'replace':
            db.execute("UPDATE requests SET input_hash='changed'")
        elif mutation == 'decrease':
            db.execute('UPDATE submissions SET attempts=0')
        else:
            db.execute('DELETE FROM submissions')
    with pytest.raises(DataQualityError):
        collect(j, w)
    assert report_warehouse(w) == previous


def test_fact_and_watermark_rollback_together(tmp_path):
    j = Journal(tmp_path/'source')
    request(j).finish()
    w = tmp_path/'mart.sqlite'
    collect(j, w)
    previous = report_warehouse(w)
    request(j, 'second').finish()

    def crash(phase):
        assert phase == 'before_commit'
        raise RuntimeError('controlled ETL interruption')

    with pytest.raises(RuntimeError, match='ETL interruption'):
        collect(j, w, checkpoint=crash)
    assert report_warehouse(w) == previous
    retried = collect(j, w)
    assert retried['changed_requests'] == 1 and retried['inserted_events'] == 2
    assert report_warehouse(w)['daily'][0]['logical_requests'] == 2


def test_same_id_in_two_sources_and_pipeline_only_jobs_stay_separate(tmp_path):
    w = tmp_path/'mart.sqlite'
    for alias in ['alpha', 'beta']:
        j = Journal(tmp_path/alias)
        request(j).finish()
        j.create('cli', {'fixture': alias})
        collect(j, w, source_id=alias)
    rows = report_warehouse(w)['daily']
    assert sum(r['logical_requests'] for r in rows) == 4
    assert sum(r['api_attempts'] for r in rows) == 2
    assert len(read(w, 'SELECT DISTINCT job_key FROM fact_request')) == 4


def test_no_source_text_or_error_or_path_in_mart(tmp_path):
    marker = 'private_canary_72a0'
    j = Journal(tmp_path/'source')
    request(j, marker, brief={'text': marker}, provider=marker).finish(error=marker)
    j.event(marker, marker, marker)
    w = tmp_path/'mart.sqlite'
    collect(j, w)
    with sqlite3.connect(w) as db:
        dump = '\n'.join(db.iterdump())
    assert marker not in dump and str(j.root) not in dump
    assert report_warehouse(w)['daily'][0]['provider'] == 'unknown'


@pytest.mark.parametrize('case,expected', [('lease', 'interrupted'), ('cancel', 'cancelled'),
                                         ('deadline', 'timed_out'), ('deadline_first', 'timed_out')])
def test_expired_control_order_and_no_unconfirmed_latency(tmp_path, case, expected):
    j = Journal(tmp_path/'source')
    request(j)
    with j.connect() as db:
        db.execute('UPDATE submissions SET lease_until=0')
        if case in {'cancel', 'deadline_first'}:
            db.execute('UPDATE submissions SET cancel_requested=1,cancel_at=100')
        if case in {'deadline', 'deadline_first'}:
            db.execute('UPDATE submissions SET deadline=50')
    w = tmp_path/'mart.sqlite'
    collect(j, w)
    report = report_warehouse(w)
    assert report['daily'][0][expected] == 1
    assert report['last_terminal_api_attempt_latency']['measured_requests'] == 0


def test_sql_percentiles_have_explicit_nearest_rank_definition(tmp_path):
    j = Journal(tmp_path/'source')
    for i, duration in enumerate([1, 2, 3, 4, 100]):
        request(j, 'job'+str(i)).finish()
        with j.connect() as db:
            db.execute('UPDATE submissions SET queued=100,updated=? WHERE job=?', (100+duration, 'job'+str(i)))
    w = tmp_path/'mart.sqlite'
    collect(j, w)
    result = report_warehouse(w)['last_terminal_api_attempt_latency']
    assert result == {'measured_requests': 5, 'p50_seconds': 3.0, 'p95_seconds': 100.0}


def test_read_snapshot_and_source_rows_unchanged(tmp_path):
    j = Journal(tmp_path/'source')
    request(j).finish()
    with j.connect() as db:
        before = '\n'.join(db.iterdump())
    collect(j, tmp_path/'mart.sqlite')
    with j.connect() as db:
        assert '\n'.join(db.iterdump()) == before


def test_older_collector_cannot_overwrite_newer_snapshot(tmp_path):
    j = Journal(tmp_path/'source')
    request(j).finish()
    w = tmp_path/'mart.sqlite'
    collect(j, w, observed_at=200)
    with pytest.raises(DataQualityError, match='older extraction'):
        collect(j, w, observed_at=199)


def test_one_read_transaction_excludes_concurrent_later_commit(tmp_path, monkeypatch):
    from reelpilot import analytics

    j = Journal(tmp_path/'source')
    request(j).finish()
    real_connect = sqlite3.connect
    writes = []

    def connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        if kwargs.get('uri'):
            def on_statement(sql):
                if sql == 'SELECT id,input_hash,status,updated FROM jobs' and not writes:
                    with real_connect(j.db) as writer:
                        writer.execute("INSERT INTO events(job,event,detail,at) "
                                       "VALUES('job','cache_hit','render',1)")
                    writes.append(True)
            db.set_trace_callback(on_statement)
        return db

    monkeypatch.setattr(analytics.sqlite3, 'connect', connect)
    w = tmp_path/'mart.sqlite'
    collect(j, w)
    assert writes == [True]
    assert report_warehouse(w)['daily'][0]['cache_hits'] == 0
    assert collect(j, w)['inserted_events'] == 1
    assert report_warehouse(w)['daily'][0]['cache_hits'] == 1


def test_stage_counter_reversal_and_missing_stage_are_rejected(tmp_path):
    j = Journal(tmp_path/'source')
    j.create('cli', {})
    owner = j.claim('cli')
    j.stage('cli', owner, 'render', {}, lambda: {})
    j.stage('cli', owner, 'render', {'changed': True}, lambda: {})
    j.release('cli', owner)
    w = tmp_path/'mart.sqlite'
    collect(j, w)
    with j.connect() as db:
        db.execute('UPDATE stages SET attempts=1')
    with pytest.raises(DataQualityError, match='counter decreased'):
        collect(j, w)
    with j.connect() as db:
        db.execute('DELETE FROM stages')
    with pytest.raises(DataQualityError, match='stage history shrank'):
        collect(j, w)


def test_api_clock_reversal_is_visible_and_excluded_from_latency(tmp_path):
    j = Journal(tmp_path/'source')
    request(j).finish()
    with j.connect() as db:
        db.execute('UPDATE submissions SET queued=200,updated=199')
    w = tmp_path/'mart.sqlite'
    collect(j, w)
    report = report_warehouse(w)
    assert report['quality']['terminal_api_rows_with_clock_reversal'] == 1
    assert report['last_terminal_api_attempt_latency']['measured_requests'] == 0


def test_two_collectors_do_not_duplicate_events(tmp_path):
    j = Journal(tmp_path/'source')
    request(j).finish()
    w = tmp_path/'mart.sqlite'
    collect(j, w)
    request(j, 'new').finish()
    observed = time.time()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: collect(j, w, observed_at=observed), range(2)))
    assert sum(r['inserted_events'] for r in results) == 2
    assert report_warehouse(w)['daily'][0]['logical_requests'] == 2


@pytest.mark.parametrize('case', ['same', 'unsupported', 'missing', 'nonfinite'])
def test_bad_input_does_not_modify_source(tmp_path, case):
    j = Journal(tmp_path/'source')
    request(j).finish()
    w = tmp_path/'mart.sqlite'
    if case == 'same':
        with pytest.raises(ValueError, match='different files'):
            collect(j, j.db)
    elif case == 'missing':
        with pytest.raises(DataQualityError, match='does not exist'):
            ingest_journal(tmp_path/'absent.sqlite', w, source_id='valid')
        assert not (tmp_path/'absent.sqlite').exists()
    elif case == 'nonfinite':
        with j.connect() as db:
            db.execute("UPDATE events SET at=? WHERE seq=1", (float('inf'),))
        with pytest.raises(DataQualityError, match='non-finite'):
            collect(j, w)
    else:
        with sqlite3.connect(w) as db:
            db.execute('CREATE TABLE unrelated(id INTEGER)')
        with pytest.raises(DataQualityError, match='not an analytics'):
            collect(j, w)
