"""Read-only journal extraction and transactional, privacy-minimized SQLite marts.

Events use a sequence watermark; mutable request/stage snapshots use hash UPSERT.
An O(N) prefix audit detects event edits/deletions, so extraction is not claimed
to be log-based CDC or sublinear. No brief, transcript, error or path is stored.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
from importlib.resources import files
import json
import math
from pathlib import Path
import re
import sqlite3
import time

STATUSES = {'queued', 'running', 'complete', 'failed', 'cancelled', 'timed_out', 'interrupted'}
STAGES = {'compile', 'verify', 'render', 'probe'}
EVENTS = {'admitted', 'cache_hit', 'stage_started', 'stage_completed', 'failed',
          'cancel_requested', 'postcommit_error', 'request_planning', 'request_executing',
          'request_complete', 'request_failed', 'request_cancelled', 'request_timed_out',
          'request_interrupted'}
TERMINAL = STATUSES - {'queued', 'running'}


class DataQualityError(ValueError):
    """Source history/schema is unsafe to reconcile; keep the previous checkpoint."""


def hashed(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def number(value, *, optional=False):
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DataQualityError('non-finite or nonnumeric timestamp')
    return float(value)


def positive_integer(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DataQualityError('invalid sequence or attempt counter')
    return value


def _read_source(path):
    # Never instantiate Journal here: its constructor creates/migrates source tables.
    if not path.is_file():
        raise DataQualityError('source database does not exist')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {
            'jobs': {'id', 'input_hash', 'status', 'updated'},
            'requests': {'job', 'input_hash', 'body'},
            'stages': {'job', 'stage', 'input_hash', 'attempts'},
            'events': {'seq', 'job', 'event', 'detail', 'at'},
        }
        if 'submissions' in tables:
            required['submissions'] = {'job', 'status', 'attempts', 'queued', 'updated', 'lease_until',
                                       'cancel_requested', 'cancel_at', 'deadline'}
        for table, columns in required.items():
            if not columns.issubset({r['name'] for r in db.execute(f'PRAGMA table_info({table})')}):
                raise DataQualityError('unsupported journal schema: ' + table)
        # All four grains come from one committed SQLite read snapshot.
        jobs = [dict(r) for r in db.execute('SELECT id,input_hash,status,updated FROM jobs')]
        receipts = {r['job']: dict(r) for r in db.execute('SELECT job,input_hash,body FROM requests')}
        submissions = ([dict(r) for r in db.execute('SELECT job,status,attempts,queued,updated,'
                       'lease_until,cancel_requested,cancel_at,deadline FROM submissions')]
                       if 'submissions' in tables else [])
        stages = [dict(r) for r in db.execute('SELECT job,stage,input_hash,attempts FROM stages')]
        events = [dict(r) for r in db.execute('SELECT seq,job,event,detail,at FROM events ORDER BY seq')]
    for e in events:
        positive_integer(e['seq'])
        number(e['at'])
    for r in jobs:
        if r['status'] not in STATUSES:
            raise DataQualityError('unknown pipeline status')
        number(r['updated'])
    for r in submissions:
        if r['job'] not in receipts or r['status'] not in STATUSES:
            raise DataQualityError('receipt missing or unknown admission status')
        positive_integer(r['attempts'])
        for key in ['queued', 'updated', 'lease_until']:
            number(r[key])
        for key in ['cancel_at', 'deadline']:
            number(r[key], optional=True)
        if r['cancel_requested'] not in (0, 1) or (r['cancel_requested'] and r['cancel_at'] is None):
            raise DataQualityError('invalid cancellation control')
    for r in stages:
        positive_integer(r['attempts'])
        if r['job'] not in {j['id'] for j in jobs}:
            raise DataQualityError('stage refers to missing pipeline job')
    return jobs, receipts, submissions, stages, events


def _effective(r, observed_at):
    if r['status'] not in {'queued', 'running'} or r['lease_until'] >= observed_at:
        return r['status']
    if r['cancel_requested'] and (r['deadline'] is None or r['cancel_at'] <= r['deadline']):
        return 'cancelled'
    if r['deadline'] is not None and observed_at >= r['deadline']:
        return 'timed_out'
    return 'cancelled' if r['cancel_requested'] else 'interrupted'


def _prefix(events, cursor):
    digest = hashlib.sha256()
    for row in events:
        if row['seq'] > cursor:
            break
        digest.update(bytes.fromhex(hashed(row)))
    return digest.hexdigest()


def _connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def _setup(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables and 'warehouse_meta' not in tables:
        raise DataQualityError('destination is not an analytics warehouse')
    if 'warehouse_meta' in tables:
        version = db.execute('SELECT version FROM warehouse_meta').fetchall()
        if len(version) != 1 or version[0][0] != 1:
            raise DataQualityError('unsupported analytics warehouse version')
    db.executescript(files('reelpilot').joinpath('analytics.sql').read_text(encoding='utf-8'))
    if not db.execute('SELECT 1 FROM warehouse_meta').fetchone():
        db.execute('INSERT OR IGNORE INTO warehouse_meta VALUES(1)')
        db.commit()


def ingest_journal(source: Path, warehouse: Path, *, source_id: str, observed_at=None, checkpoint=None):
    """Append events and reconcile current facts atomically for one explicit source.

    source_id identifies a journal's lifetime, not its path. Reusing an ID after
    replacing/truncating that journal is rejected. checkpoint is a fault-test hook.
    """
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', source_id):
        raise ValueError('source_id must be a short nonpersonal identifier')
    source, warehouse = Path(source).resolve(), Path(warehouse).resolve()
    if source == warehouse or (source.exists() and warehouse.exists() and source.samefile(warehouse)):
        raise ValueError('source and warehouse must be different files')
    observed_at = number(time.time() if observed_at is None else observed_at)
    jobs, receipts, submissions, stages, events = _read_source(source)
    def key(job):
        return hashed([source_id, job])
    first, admitted = {}, {}
    for e in events:
        first.setdefault(e['job'], e['at'])
        if e['event'] == 'admitted':
            admitted[e['job']] = admitted.get(e['job'], 0) + 1
    api = {r['job']: r for r in submissions}
    facts = []
    all_jobs = {r['id']: r for r in jobs}
    for job in sorted(set(all_jobs) | set(api)):
        r = api.get(job, all_jobs.get(job))
        kind = 'api' if job in api else 'pipeline'
        status = _effective(r, observed_at) if kind == 'api' else r['status']
        body = {}
        if job in receipts:
            try:
                body = json.loads(receipts[job]['body'])
            except (ValueError, TypeError):
                raise DataQualityError('invalid receipt JSON') from None
            if not isinstance(body, dict):
                raise DataQualityError('receipt body is not an object')
        provider = body.get('provider', 'unknown')
        mode = body.get('mode', 'unknown')
        provider = provider if provider in {'explicit', 'transformers', 'ollama'} else 'unknown'
        mode = mode if mode in {'lexical', 'dense', 'hybrid', 'adaptive'} else 'unknown'
        identity = receipts[job]['input_hash'] if kind == 'api' else r['input_hash']
        elapsed = None
        if kind == 'api':
            if admitted.get(job, 0) != r['attempts']:
                raise DataQualityError('admission events disagree with request attempts')
            if r['status'] in TERMINAL and r['updated'] >= r['queued']:
                elapsed = r['updated'] - r['queued']
        values = [source_id, key(job), hashed(identity), kind, r['status'], status,
                  r.get('attempts'), provider, mode, r.get('queued'), r['updated'], first.get(job), elapsed]
        facts.append(values + [hashed(values)])
    stage_facts = []
    for r in stages:
        values = [source_id, key(r['job']), hashed(r['stage']),
                  r['stage'] if r['stage'] in STAGES else 'other', r['attempts']]
        stage_facts.append(values + [hashed(values)])
    warehouse.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(warehouse)) as db:
        _setup(db)
        with db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM sources WHERE source_id=?', (source_id,)).fetchone()
            if previous and observed_at < previous['snapshot_at']:
                raise DataQualityError('an older extraction cannot overwrite a newer snapshot')
            cursor = previous['event_cursor'] if previous else 0
            maximum = events[-1]['seq'] if events else 0
            if previous and (maximum < cursor or _prefix(events, cursor) != previous['prefix_hash']):
                raise DataQualityError('source event history changed; use a new source_id for a new journal')
            old = {r['job_key']: r for r in db.execute('SELECT job_key,identity_hash,attempts,updated_at FROM fact_request '
                                                      'WHERE source_id=?', (source_id,))}
            if set(old) - {r[1] for r in facts}:
                raise DataQualityError('source request history shrank')
            for row in facts:
                prior = old.get(row[1])
                if prior and row[10] < prior['updated_at']:
                    raise DataQualityError('source request update time moved backwards')
                if prior and (prior['identity_hash'] != row[2] or
                              (prior['attempts'] is not None and
                               (row[6] is None or row[6] < prior['attempts']))):
                    raise DataQualityError('source identity changed or attempt counter decreased')
            old_stages = {(r['job_key'], r['stage_key']): r['committed_executions'] for r in db.execute(
                'SELECT job_key,stage_key,committed_executions FROM fact_stage WHERE source_id=?', (source_id,))}
            if set(old_stages) - {(r[1], r[2]) for r in stage_facts}:
                raise DataQualityError('source stage history shrank')
            if any(r[4] < old_stages.get((r[1], r[2]), 0) for r in stage_facts):
                raise DataQualityError('source stage commit counter decreased')
            db.execute('INSERT INTO sources VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET '
                       'event_cursor=excluded.event_cursor,prefix_hash=excluded.prefix_hash,'
                       'snapshot_at=excluded.snapshot_at',
                       (source_id, maximum, _prefix(events, maximum), observed_at))
            before = db.total_changes
            db.executemany('INSERT INTO fact_request VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                           'ON CONFLICT(source_id,job_key) DO UPDATE SET '
                           'identity_hash=excluded.identity_hash,kind=excluded.kind,stored_status=excluded.stored_status,'
                           'effective_status=excluded.effective_status,attempts=excluded.attempts,'
                           'provider=excluded.provider,mode=excluded.mode,queued_at=excluded.queued_at,'
                           'updated_at=excluded.updated_at,first_observed_at=excluded.first_observed_at,'
                           'last_attempt_seconds=excluded.last_attempt_seconds,row_hash=excluded.row_hash '
                           'WHERE fact_request.row_hash<>excluded.row_hash', facts)
            changed_requests = db.total_changes - before
            before = db.total_changes
            db.executemany('INSERT INTO fact_stage VALUES(?,?,?,?,?,?) '
                           'ON CONFLICT(source_id,job_key,stage_key) DO UPDATE SET '
                           'committed_executions=excluded.committed_executions,row_hash=excluded.row_hash '
                           'WHERE fact_stage.row_hash<>excluded.row_hash', stage_facts)
            changed_stages = db.total_changes - before
            new_events = [e for e in events if e['seq'] > cursor]
            db.executemany('INSERT INTO fact_event VALUES(?,?,?,?,?,?)',
                [(source_id, e['seq'], key(e['job']) if e['job'] is not None else None,
                  e['event'] if e['event'] in EVENTS else 'other',
                  e['detail'] if e['event'] in {'cache_hit', 'stage_started', 'stage_completed'}
                  and e['detail'] in STAGES else None, e['at']) for e in new_events])
            if checkpoint:
                checkpoint('before_commit')
    return {'source_id': source_id, 'event_cursor': maximum, 'inserted_events': len(new_events),
            'changed_requests': changed_requests, 'changed_stages': changed_stages}


def report_warehouse(warehouse: Path):
    path = Path(warehouse).resolve()
    if not path.is_file():
        raise ValueError('warehouse does not exist')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        return {
            'schema_version': 1,
            'sources': [dict(r) for r in db.execute('SELECT source_id,snapshot_at,event_cursor '
                                                   'FROM sources ORDER BY source_id')],
            'daily': [dict(r) for r in db.execute('SELECT * FROM daily_metrics '
                                                 'ORDER BY cohort_day_utc,kind,provider,mode')],
            'last_terminal_api_attempt_latency': dict(db.execute('SELECT * FROM attempt_latency').fetchone()),
            'event_rows': db.execute('SELECT count(*) FROM fact_event').fetchone()[0],
            'quality': {
                'unmapped_events': db.execute("SELECT count(*) FROM fact_event WHERE event='other'").fetchone()[0],
                'events_without_current_request': db.execute('SELECT count(*) FROM fact_event e '
                    'LEFT JOIN fact_request r USING(source_id,job_key) WHERE r.job_key IS NULL').fetchone()[0],
                'terminal_api_rows_with_clock_reversal': db.execute("SELECT count(*) FROM fact_request "
                    "WHERE kind='api' AND stored_status NOT IN ('queued','running') "
                    "AND updated_at<queued_at").fetchone()[0],
            },
            'scope': ['current logical-request snapshot, not historical as-of reconstruction',
                      'stage commits are durable successes, not all model/tool invocations',
                      'latency is last confirmed terminal API attempt wall time, including queue wait',
                      'expired live leases are inferred terminal states at extraction snapshot time',
                      'no token/cost/GPU/queue-only timing is available in these journals'],
        }
