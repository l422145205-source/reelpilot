"""Attempt identity, partial traces and privacy; controlled timing is not model quality."""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from reelpilot.admission import Admission, Ticket
from reelpilot.control import BusyJob
from reelpilot.journal import Journal
from reelpilot.tracing import trace_report


def rows(journal, table):
    with journal.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM '+table)]


def test_failure_recovery_is_distinct_from_successful_replay(tmp_path):
    j = Journal(tmp_path)
    q = Admission(j)
    first = q.reserve('job', {})
    first.phase('planning')
    with first.span('planning'):
        pass
    first.finish(ValueError('private error text'))
    q.reserve('job', {}).finish()
    q.reserve('job', {}).finish()
    r = trace_report(j.db)
    assert r['recorded_attempts'] == r['accepted_attempts'] == 3
    assert r['transitions']['completed_after_recorded_failure'] == 1
    assert r['transitions']['completed_after_recorded_success'] == 1
    assert len({a['attempt_id'] for a in rows(j, 'request_attempts')}) == 3


def test_old_ticket_reconstruction_uses_immutable_owner_binding(tmp_path):
    j = Journal(tmp_path)
    q = Admission(j)
    old = q.reserve('job', {})
    old.finish(ValueError('fixture'))
    new = q.reserve('job', {})
    restored = Ticket(q, 'job', old.owner)
    assert restored.attempt_id == old.attempt_id != new.attempt_id
    with pytest.raises(BusyJob):
        restored.phase('planning')
    assert rows(j, 'request_attempts')[1]['status'] == 'queued'
    new.finish()


def test_late_old_span_does_not_attach_to_successor_or_commit_stage(tmp_path):
    j = Journal(tmp_path)
    q = Admission(j)
    old = q.reserve('job', {})
    j.create('job', {})
    owner = j.claim('job', request_owner=old.owner)
    entered, release = Event(), Event()

    def operation():
        entered.set()
        assert release.wait(5)
        return {'fixture': True}

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(j.stage, 'job', owner, 'render', {}, operation)
        try:
            assert entered.wait(3)
            with j.connect() as db:
                db.execute('UPDATE submissions SET lease_until=0')
            new = q.reserve('job', {})
        finally:
            release.set()
        with pytest.raises(BusyJob):
            future.result(timeout=3)
    assert rows(j, 'stages') == []
    span = rows(j, 'operation_spans')[0]
    assert span['attempt_id'] == old.attempt_id and span['outcome'] == 'fenced'
    assert rows(j, 'request_attempts')[0]['status'] == 'superseded'
    assert rows(j, 'request_attempts')[1]['attempt_id'] == new.attempt_id
    new.finish()


def test_cache_hit_and_committed_operation_are_not_combined(tmp_path):
    j = Journal(tmp_path)
    t = Admission(j).reserve('job', {})
    j.create('job', {})
    owner = j.claim('job', request_owner=t.owner)
    j.stage('job', owner, 'render', {}, lambda: {'fixture': True})
    j.stage('job', owner, 'render', {}, lambda: pytest.fail('must reuse'))
    assert [s['outcome'] for s in rows(j, 'operation_spans')] == ['success', 'cache_hit']
    assert rows(j, 'stages')[0]['attempts'] == 1
    t.finish()


def test_incomplete_trace_is_unknown_instead_of_zero_duration(tmp_path):
    j = Journal(tmp_path)
    t = Admission(j).reserve('job', {})
    with j.connect() as db:
        db.execute('INSERT INTO operation_spans VALUES(?,?,?, ?,NULL,NULL,?)',
                   ('fixture-span', t.attempt_id, 'render', time.time(), 'open'))
    r = trace_report(j.db)
    span = r['operations'][0]
    assert span['observations'] == 1 and span['measured_observations'] == 0
    assert span['mean_seconds'] is span['total_observed_seconds'] is None
    t.finish()


def test_legacy_history_is_not_backfilled_from_event_order(tmp_path):
    j = Journal(tmp_path)
    q = Admission(j)
    q.reserve('job', {}).finish(ValueError('legacy fixture'))
    with j.connect() as db:
        db.execute('DROP TABLE request_attempts')
        db.execute('DROP TABLE operation_spans')
    legacy = trace_report(j.db)
    assert not legacy['available'] and legacy['unrecorded_legacy_attempts'] == 1
    reopened = Admission(Journal(tmp_path))
    reopened.reserve('job', {}).finish()
    r = trace_report(j.db)
    assert r['recorded_attempts'] == 1 and r['unrecorded_legacy_attempts'] == 1
    assert r['transitions']['attempts_with_unknown_predecessor'] == 1
    assert r['transitions']['completed_after_recorded_failure'] == 0


def test_cancelled_request_does_not_get_a_successor_attempt(tmp_path):
    j = Journal(tmp_path)
    q = Admission(j)
    t = q.reserve('job', {})
    q.cancel('job')
    t.finish()
    with pytest.raises(ValueError):
        q.reserve('job', {})
    assert len(rows(j, 'request_attempts')) == 1
    assert rows(j, 'request_attempts')[0]['status'] == 'cancelled'


def test_monotonic_span_survives_wall_clock_reversal(tmp_path, monkeypatch):
    from reelpilot import tracing
    j = Journal(tmp_path)
    t = Admission(j).reserve('job', {})
    with monkeypatch.context() as patch:
        patch.setattr(tracing.time, 'time', iter([100., 99.]).__next__)
        patch.setattr(tracing.time, 'perf_counter', iter([5., 5.125]).__next__)
        with t.span('planning'):
            pass
    assert rows(j, 'operation_spans')[0]['elapsed_seconds'] == .125
    assert rows(j, 'operation_spans')[0]['finished_at'] < rows(j, 'operation_spans')[0]['started_at']
    t.finish()


def test_trace_report_and_span_records_do_not_store_source_content(tmp_path):
    marker = 'private-canary-trace-7ed4'
    j = Journal(tmp_path)
    t = Admission(j).reserve(marker, {'text': marker})
    with pytest.raises(ValueError), t.span(marker):
        raise ValueError(marker)
    t.finish(ValueError(marker))
    payload = json.dumps(rows(j, 'operation_spans'))
    public = json.dumps(trace_report(j.db))
    assert marker not in payload+public and t.owner not in payload+public
    assert str(tmp_path) not in public and t.attempt_id not in public
    assert rows(j, 'operation_spans')[0]['operation'] == 'other'


def test_finalization_and_attempt_completion_rollback_together(tmp_path):
    j = Journal(tmp_path)
    t = Admission(j).reserve('job', {})
    j.create('job', {})
    owner = j.claim('job', request_owner=t.owner)
    temporary, final = tmp_path/'pending.json', tmp_path/'result.json'
    temporary.write_text('{}')

    def stop(_):
        raise RuntimeError('controlled finalization interruption')

    with pytest.raises(RuntimeError):
        j.finalize('job', owner, temporary, final, checkpoint=stop)
    assert rows(j, 'request_attempts')[0]['finished_at'] is None
    assert rows(j, 'submissions')[0]['status'] == 'queued'
    temporary.write_text('{}')
    j.finalize('job', owner, temporary, final)
    assert rows(j, 'request_attempts')[0]['status'] == 'complete'
    t.finish(ValueError('cleanup fixture'))
    assert rows(j, 'request_attempts')[0]['status'] == 'complete'


def test_os_kill_leaves_durable_open_span_and_new_attempt(tmp_path):
    from test_process_runtime import source_env, wait_for

    from reelpilot.processes import OwnedProcess
    j = Journal(tmp_path/'jobs')
    q = Admission(j, ttl=1, interval=.15)
    old = q.reserve('killed-span', {})
    old.start()
    marker = tmp_path/'started.txt'
    code = '''import json,sys,time
from pathlib import Path
from reelpilot.admission import Admission,Ticket
from reelpilot.journal import Journal
p=json.loads(sys.stdin.buffer.readline())
j=Journal(Path(p['jobs']))
t=Ticket(Admission(j,ttl=1,interval=.15),p['job'],p['owner'])
t.phase('planning')
with t.span('planning'):
 Path(p['marker']).write_text('span started')
 time.sleep(60)
'''
    child = OwnedProcess([sys.executable, '-B', '-c', code], gated=True, env=source_env())
    try:
        child.send((json.dumps({'jobs':str(j.root),'job':old.job,'owner':old.owner,'marker':str(marker)})+'\n').encode())
        wait_for(marker.exists)
        child.close()
        old.stop()
        wait_for(lambda:q.get(old.job)['status']=='interrupted', seconds=3)
        new = Admission(j).reserve(old.job, {})
        new.finish()
        r = trace_report(j.db)
        assert r['recorded_attempts'] == 2 and r['superseded_attempts'] == 1
        assert r['operations'][0]['outcome'] == 'open'
        assert r['operations'][0]['mean_seconds'] is None
        assert r['transitions']['completed_after_recorded_failure'] == 0
        assert r['transitions']['completed_after_supersession'] == 1
        (tmp_path/'trace-evidence.json').write_text(json.dumps({
            'case':'os_kill_during_controlled_span','models_loaded':False,
            'worker_killed':True,'open_duration_unknown':True,'metrics':r}),encoding='utf-8')
    finally:
        child.close()
        old.stop()


def test_admission_and_attempt_creation_are_one_transaction(tmp_path, monkeypatch):
    from reelpilot import admission
    j = Journal(tmp_path)
    def fail(*args):
        raise RuntimeError('controlled trace admission failure')
    with monkeypatch.context() as patch:
        patch.setattr(admission, 'admit_attempt', fail)
        with pytest.raises(RuntimeError, match='trace admission failure'):
            Admission(j).reserve('job', {})
    assert rows(j,'requests') == rows(j,'submissions') == rows(j,'request_attempts') == []
    assert rows(j,'events') == []
    Admission(j).reserve('job',{}).finish()
    assert trace_report(j.db)['recorded_attempts'] == 1


def test_detail_api_contains_no_owner_capabilities_or_request_text(library,tmp_path):
    from fastapi.testclient import TestClient
    from test_admission import wait_terminal

    from reelpilot.api import create_app
    body={'job_id':'planning-error','provider':'transformers',
          'brief':{'text':'private-content-canary-2814','budget_seconds':6}}
    with TestClient(create_app(library.root,tmp_path/'jobs')) as client:
        assert client.post('/api/jobs',json=body).status_code == 202
        assert wait_terminal(client,'planning-error')['status'] == 'failed'
        response=client.get('/api/jobs/planning-error/attempts')
        assert response.status_code == 200
        data=response.json()
        assert len(data['attempts']) == 1
        assert data['attempts'][0]['status'] == 'failed'
        assert data['operations'][0]['outcome'] == 'error'
        assert 'owner' not in response.text and 'private-content-canary' not in response.text
        assert client.get('/api/jobs/missing/attempts').status_code == 404


def test_real_planning_recovery_replay_and_phase_spans(tmp_path):
    from reelpilot.demo import make_demo
    from reelpilot.execution import run_request
    from reelpilot.pipeline import run_job
    from reelpilot.schema import JobRequest
    library = make_demo(tmp_path/'media')
    j = Journal(tmp_path/'jobs')
    q = Admission(j)
    body = JobRequest.model_validate({'brief':{'text':'forest;robot','budget_seconds':8}})
    payload = body.model_dump(exclude={'job_id'},exclude_none=True)

    def execute(job, interrupt=False):
        t = q.reserve(job,payload)
        def pipeline(*args,**kwargs):
            return run_job(*args,**kwargs,interrupt_after='render' if interrupt else None)
        try:
            result = run_request(t,body,library,pipeline=pipeline)
        except Exception as error:
            t.finish(error)
            raise
        t.finish()
        return result

    with pytest.raises(RuntimeError,match='controlled interruption after render'):
        execute('recover',True)
    recovered = execute('recover')
    execute('replay')
    execute('replay')
    r = trace_report(j.db)
    assert r['accepted_attempts'] == r['recorded_attempts'] == 4
    assert r['transitions']['completed_after_recorded_failure'] == 1
    assert r['transitions']['completed_after_recorded_success'] == 1
    assert r['unrecorded_legacy_attempts'] == 0
    for op in ['planning','retriever_setup','compile','verify','render','probe']:
        assert any(s['operation']==op and s['measured_observations']>0 for s in r['operations'])
    with j.connect() as db:
        assert db.execute("SELECT sum(attempts) FROM stages WHERE stage='render'").fetchone()[0] == 2
    (tmp_path/'trace-evidence.json').write_text(json.dumps({
        'case':'real_recovery_and_successful_replay','render_commits':2,
        'media':{'width':recovered['media']['width'],'height':recovered['media']['height'],
                 'duration':recovered['media']['duration']},'metrics':r}),encoding='utf-8')


def test_independent_attempt_and_span_cursors_are_bounded_and_stable(tmp_path):
    from reelpilot.tracing import trace_details
    j=Journal(tmp_path)
    q=Admission(j)
    for _ in range(3):
        t=q.reserve('job',{})
        with t.span('planning'):
            pass
        t.finish()
    first=trace_details(j,'job',limit=2)
    assert len(first['attempts'])==len(first['operations'])==2
    assert first['next_after_attempt']==2 and first['next_after_span']==2
    second=trace_details(j,'job',after_attempt=2,after_span=2,limit=2)
    assert len(second['attempts'])==len(second['operations'])==1
    assert second['next_after_attempt'] is second['next_after_span'] is None
    assert second['attempts'][0]['ordinal']==3 and second['operations'][0]['sequence']==3
    with pytest.raises(ValueError):
        trace_details(j,'job',limit=51)
