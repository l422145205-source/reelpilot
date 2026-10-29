"""Attempt-bound, local-only operational records; no prompts, outputs or secrets.

Execution authorization stays in admission/journal. A late span may finish on its
original attempt even after supersession, without gaining publication authority.
"""
import hashlib
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from .control import BusyJob, DeadlineExceeded, TaskCancelled

SCHEMA = """
CREATE TABLE IF NOT EXISTS request_attempts (
 attempt_id TEXT PRIMARY KEY, job TEXT NOT NULL, ordinal INTEGER NOT NULL,
 owner_key TEXT NOT NULL, status TEXT NOT NULL, admitted_at REAL NOT NULL,
 started_at REAL, finished_at REAL, superseded_at REAL,
 UNIQUE(job,ordinal), UNIQUE(job,owner_key)
);
CREATE TABLE IF NOT EXISTS operation_spans (
 span_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, operation TEXT NOT NULL,
 started_at REAL NOT NULL, finished_at REAL, elapsed_seconds REAL, outcome TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS spans_attempt ON operation_spans(attempt_id);
"""
OPERATIONS = {'planning', 'retriever_setup', 'compile', 'verify', 'render', 'probe'}


def owner_key(owner):
    return hashlib.sha256(owner.encode()).hexdigest()


def admit_attempt(db, job, owner, ordinal, now):
    # This runs in the SAME transaction as the durable admission receipt.
    db.execute("UPDATE request_attempts SET status='superseded',superseded_at=? "
               "WHERE job=? AND status IN ('queued','running')", (now, job))
    db.execute('INSERT INTO request_attempts(attempt_id,job,ordinal,owner_key,status,admitted_at) '
               "VALUES(?,?,?,?,'queued',?)", (uuid.uuid4().hex, job, ordinal, owner_key(owner), now))


def attempt_for(db, job, owner):
    if owner is None:
        return None
    row = db.execute('SELECT attempt_id FROM request_attempts WHERE job=? AND owner_key=?',
                     (job, owner_key(owner))).fetchone()
    return row[0] if row else None  # Legacy attempts are never fabricated.


def update_attempt(db, job, owner, status, now):
    if owner is None:
        return
    if status == 'running':
        db.execute("UPDATE request_attempts SET status='running',started_at=COALESCE(started_at,?) "
                   "WHERE job=? AND owner_key=? AND status IN ('queued','running')",
                   (now, job, owner_key(owner)))
    else:
        db.execute("UPDATE request_attempts SET status=?,finished_at=? "
                   "WHERE job=? AND owner_key=? AND status IN ('queued','running')",
                   (status, now, job, owner_key(owner)))


class Span:
    def __init__(self):
        self.outcome = 'success'


@contextmanager
def operation_span(journal, attempt_id, operation):
    span = Span()
    if attempt_id is None:
        yield span
        return
    name = operation if operation in OPERATIONS else 'other'
    span_id = uuid.uuid4().hex
    with journal.connect() as db:
        db.execute('INSERT INTO operation_spans(span_id,attempt_id,operation,started_at,outcome) '
                   "VALUES(?,?,?,?,'open')", (span_id, attempt_id, name, time.time()))
    start = time.perf_counter()
    try:
        yield span
    except BaseException as error:
        span.outcome = ('fenced' if isinstance(error, BusyJob) else
                        'cancelled' if isinstance(error, TaskCancelled) else
                        'timed_out' if isinstance(error, DeadlineExceeded) else 'error')
        raise
    finally:
        # Duration uses only two readings in this same process. After OS kill this
        # block does not execute; the durable span remains open with unknown time.
        elapsed = time.perf_counter() - start
        with journal.connect() as db:
            db.execute('UPDATE operation_spans SET finished_at=?,elapsed_seconds=?,outcome=? '
                       'WHERE span_id=? AND outcome=\'open\'',
                       (time.time(), elapsed, span.outcome, span_id))


def trace_report(source: Path):
    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError('journal does not exist')
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        known = db.execute('SELECT COALESCE(sum(attempts),0) FROM submissions').fetchone()[0] \
            if 'submissions' in tables else 0
        if 'request_attempts' not in tables:
            return {'schema_version': 1, 'available': False, 'accepted_attempts': known,
                    'recorded_attempts': 0, 'unrecorded_legacy_attempts': known}
        counts = dict(db.execute("SELECT count(*) AS recorded_attempts,"
            "COALESCE(sum(status='superseded'),0) AS superseded_attempts,"
            "COALESCE(sum(status='complete'),0) AS complete_attempts,"
            "COALESCE(sum(status IN ('failed','timed_out','interrupted')),0) AS failed_or_interrupted_attempts,"
            "COALESCE(sum(status IN ('queued','running')),0) AS unconfirmed_attempts "
            'FROM request_attempts').fetchone())
        transitions = dict(db.execute("""
            WITH ordered AS (
             SELECT *,LAG(status) OVER(PARTITION BY job ORDER BY ordinal) AS previous_status,
              LAG(ordinal) OVER(PARTITION BY job ORDER BY ordinal) AS previous_ordinal
             FROM request_attempts
            ) SELECT
             COALESCE(sum(status='complete' AND previous_ordinal=ordinal-1 AND
                 previous_status IN ('failed','timed_out','interrupted')),0) AS completed_after_recorded_failure,
             COALESCE(sum(status='complete' AND previous_ordinal=ordinal-1 AND
                 previous_status='complete'),0) AS completed_after_recorded_success,
             COALESCE(sum(status='complete' AND previous_ordinal=ordinal-1 AND
                 previous_status='superseded'),0) AS completed_after_supersession,
             COALESCE(sum(ordinal>1 AND (previous_ordinal IS NULL OR previous_ordinal<>ordinal-1)),0)
                 AS attempts_with_unknown_predecessor
            FROM ordered
        """).fetchone())
        waits = dict(db.execute("""
            SELECT count(*) AS measured_attempts,
             avg(started_at-admitted_at) AS mean_seconds,
             min(started_at-admitted_at) AS min_seconds,
             max(started_at-admitted_at) AS max_seconds
            FROM request_attempts WHERE started_at>=admitted_at
        """).fetchone())
        clock = db.execute('SELECT count(*) FROM request_attempts WHERE '
                           'started_at<admitted_at OR finished_at<started_at').fetchone()[0]
        spans = [dict(r) for r in db.execute("""
            SELECT operation,outcome,count(*) AS observations,
             count(elapsed_seconds) AS measured_observations,avg(elapsed_seconds) AS mean_seconds,
             sum(elapsed_seconds) AS total_observed_seconds
            FROM operation_spans GROUP BY operation,outcome ORDER BY operation,outcome
        """)]
        if counts['recorded_attempts'] > known:
            raise ValueError('attempt ledger exceeds durable admission counts')
        return {'schema_version': 1, 'available': True, 'accepted_attempts': known, **counts,
                'unrecorded_legacy_attempts': known-counts['recorded_attempts'],
                'transitions': transitions, 'admission_to_first_phase_wall_wait': waits,
                'clock_reversal_attempts': clock, 'operations': spans,
                'scope': ['attempt transitions require consecutive recorded ordinals',
                          'superseded means ownership replaced, not confirmed physical worker exit',
                          'operation durations are local monotonic wall time, including waits',
                          'open spans have unknown duration and are excluded from duration aggregates',
                          'admission wait uses wall clock and includes process startup/model-gate wait',
                          'no token, billing, pure model compute, or semantic quality measurements']}


def trace_details(journal, job, *, after_attempt=0, after_span=0, limit=50):
    """Local API details; trace IDs are correlations, never ownership capabilities."""
    if any(isinstance(n,bool) or not isinstance(n,int) or n<0 for n in [after_attempt,after_span]) \
            or isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=50:
        raise ValueError('invalid trace pagination')
    with journal.connect() as db:
        db.execute('BEGIN')
        known = db.execute('SELECT 1 FROM requests WHERE job=? UNION ALL '
                           'SELECT 1 FROM jobs WHERE id=?', (job,job)).fetchone()
        if not known:
            raise KeyError(job)
        attempts = [dict(r) for r in db.execute(
            'SELECT attempt_id,ordinal,status,admitted_at,started_at,finished_at,superseded_at '
            'FROM request_attempts WHERE job=? AND ordinal>? ORDER BY ordinal LIMIT ?',
            (job,after_attempt,limit+1))]
        spans = [dict(r) for r in db.execute(
            'SELECT s.rowid AS sequence,s.span_id,s.attempt_id,s.operation,s.started_at,s.finished_at,'
            's.elapsed_seconds,s.outcome FROM operation_spans s '
            'JOIN request_attempts a ON a.attempt_id=s.attempt_id WHERE a.job=? AND s.rowid>? '
            'ORDER BY s.rowid LIMIT ?', (job,after_span,limit+1))]
    next_attempt=attempts[limit-1]['ordinal'] if len(attempts)>limit else None
    next_span=spans[limit-1]['sequence'] if len(spans)>limit else None
    return {'schema_version':1,'attempts':attempts[:limit],'operations':spans[:limit],
            'next_after_attempt':next_attempt,'next_after_span':next_span,
            'legacy_history_backfilled':False}
