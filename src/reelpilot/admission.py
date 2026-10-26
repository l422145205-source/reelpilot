"""Durable request admission before planning, with bounded expiring ownership.

The pipeline journal fences published artifacts separately. This queue provides
one receipt per original request, not exactly-once model/tool execution.
"""

from __future__ import annotations

import json
import time
import uuid
import math
from threading import Event, Thread

from pydantic import TypeAdapter

from .journal import BusyJob, Journal
from .library import stable_hash
from .schema import Identifier
from .control import (
    TaskCancelled,
    DeadlineExceeded,
    ProcessCleanupError,
    guard_request,
    terminal_status,
    requested_error,
)


class QueueFull(RuntimeError):
    pass


class Admission:
    def __init__(self, journal: Journal, *, capacity=8, ttl=120, interval=10):
        if capacity < 1 or ttl <= 0 or not 0 < interval < ttl:
            raise ValueError("invalid admission capacity or lease timing")
        self.journal, self.capacity = journal, capacity
        self.ttl, self.interval = ttl, interval
        with journal.connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS submissions (
                job TEXT PRIMARY KEY, status TEXT NOT NULL, phase TEXT NOT NULL,
                owner TEXT, lease_until REAL NOT NULL, attempts INTEGER NOT NULL,
                queued REAL NOT NULL, updated REAL NOT NULL, error TEXT)"""
            )
            db.execute("BEGIN IMMEDIATE")
            columns = {r["name"] for r in db.execute("PRAGMA table_info(submissions)")}
            for name, kind in [
                ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
                ("cancel_at", "REAL"),
                ("deadline", "REAL"),
            ]:
                if name not in columns:
                    db.execute(f"ALTER TABLE submissions ADD COLUMN {name} {kind}")

    def reserve(self, job: str, body: dict, *, timeout_seconds=None):
        self.journal.runtime_check()
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0
        ):
            raise ValueError("timeout must be finite and positive")
        TypeAdapter(Identifier).validate_python(job)
        hashed, now, owner = stable_hash(body), time.time(), uuid.uuid4().hex
        with self.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT input_hash FROM requests WHERE job=?", (job,)).fetchone()
            if previous and previous[0] != hashed:
                raise ValueError("idempotency key belongs to a different original brief")
            pipeline = db.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()
            if pipeline and not previous:
                raise ValueError("job ID belongs to a pipeline without an API receipt; use a new ID")
            old = db.execute("SELECT * FROM submissions WHERE job=?", (job,)).fetchone()
            if old and (old["status"] == "cancelled" or isinstance(requested_error(old, now), TaskCancelled)):
                raise ValueError("cancelled job IDs are terminal; create a new job ID")
            if old and old["status"] in ("queued", "running") and old["lease_until"] >= now:
                raise BusyJob("request already has an active worker or queue slot")
            count = db.execute(
                "SELECT count(*) FROM submissions WHERE status IN ('queued','running') " "AND lease_until>=?",
                (now,),
            ).fetchone()[0]
            if count >= self.capacity:
                raise QueueFull("request capacity reached; retry later with the same job ID")
            db.execute(
                "INSERT OR IGNORE INTO requests(job,input_hash,body) VALUES (?,?,?)",
                (job, hashed, json.dumps(body, ensure_ascii=False)),
            )
            db.execute(
                """INSERT INTO submissions
                (job,status,phase,owner,lease_until,attempts,queued,updated,error,cancel_requested,cancel_at,deadline)
                VALUES (?,?,?,?,?,1,?,?,NULL,0,NULL,?)
                ON CONFLICT(job) DO UPDATE SET status='queued',phase='queued',
                owner=excluded.owner,lease_until=excluded.lease_until,
                attempts=submissions.attempts+1,queued=excluded.queued,
                updated=excluded.updated,error=NULL,cancel_requested=0,cancel_at=NULL,deadline=excluded.deadline""",
                (
                    job,
                    "queued",
                    "queued",
                    owner,
                    now + self.ttl,
                    now,
                    now,
                    now + timeout_seconds if timeout_seconds is not None else None,
                ),
            )
            db.execute(
                "INSERT INTO events(job,event,detail,at) VALUES (?,?,?,?)",
                (job, "admitted", "durable request receipt", now),
            )
        return Ticket(self, job, owner)

    def get(self, job):
        with self.journal.connect() as db:
            row = db.execute(
                "SELECT job,status,phase,attempts,queued,updated,error,lease_until,cancel_requested,cancel_at,deadline "
                "FROM submissions WHERE job=?",
                (job,),
            ).fetchone()
        if row is None:
            raise KeyError(job)
        result = dict(row)
        now = time.time()
        if result["status"] in ("queued", "running") and result["lease_until"] < now:
            control = requested_error(row, now)
            result["status"] = terminal_status(control) if control else "interrupted"
            result["phase"] = result["status"]
            result["error"] = (
                str(control)
                if control
                else "worker lease expired; resubmit the identical original request to recover"
            )
        elif result["status"] in ("queued", "running") and result["cancel_requested"]:
            result["phase"] = "cancelling"  # Capacity is retained until acknowledgment/reaping.
        result.pop("lease_until", None)
        result.pop("cancel_at", None)
        return result

    def cancel(self, job):
        now = time.time()
        with self.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM submissions WHERE job=?", (job,)).fetchone()
            if row is None:
                raise KeyError(job)
            if row["status"] not in ("queued", "running"):
                return {"job_id": job, "status": row["status"], "requested": False}
            control = requested_error(row, now)
            if isinstance(control, DeadlineExceeded):
                if row["lease_until"] < now:
                    db.execute(
                        "UPDATE submissions SET status='timed_out',phase='timed_out',owner=NULL,lease_until=0,"
                        "error=?,updated=? WHERE job=?",
                        (str(control), now, job),
                    )
                return {"job_id": job, "status": "timed_out", "requested": False}
            if row["lease_until"] < now:
                db.execute(
                    "UPDATE submissions SET status='cancelled',phase='cancelled',owner=NULL,lease_until=0,"
                    "cancel_requested=1,cancel_at=COALESCE(cancel_at,?),updated=? WHERE job=?",
                    (now, now, job),
                )
                return {"job_id": job, "status": "cancelled", "requested": True}
            db.execute(
                "UPDATE submissions SET cancel_requested=1,cancel_at=COALESCE(cancel_at,?),updated=? "
                "WHERE job=?",
                (now, now, job),
            )
            if not row["cancel_requested"]:
                db.execute(
                    "INSERT INTO events(job,event,detail,at) VALUES (?,?,?,?)",
                    (job, "cancel_requested", "client", now),
                )
            return {"job_id": job, "status": "cancelling", "requested": True}


class Ticket:
    def __init__(self, admission, job, owner):
        self.admission, self.job, self.owner = admission, job, owner
        self._stop = Event()
        self._thread = None

    def guard(self):
        self.admission.journal.runtime_check()
        with self.admission.journal.connect() as db:
            guard_request(db, self.job, self.owner)

    def completed_by_self(self):
        with self.admission.journal.connect() as db:
            row = db.execute("SELECT owner,status FROM submissions WHERE job=?", (self.job,)).fetchone()
        return bool(row and row["owner"] == self.owner and row["status"] == "complete")

    def _update(self, status, phase, error=None):
        now = time.time()
        with self.admission.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            guard_request(db, self.job, self.owner)
            changed = db.execute(
                "UPDATE submissions SET status=?,phase=?,error=?,updated=?,lease_until=? "
                "WHERE job=? AND owner=? AND lease_until>=? "
                "AND status IN ('queued','running')",
                (
                    status,
                    phase,
                    str(error)[:1500] if error else None,
                    now,
                    now + self.admission.ttl if status == "running" else 0,
                    self.job,
                    self.owner,
                    now,
                ),
            )
            if changed.rowcount != 1:
                raise BusyJob("stale request cannot update its successor")
        self.admission.journal.event(self.job, "request_" + phase)

    def phase(self, phase):
        if phase not in ("planning", "executing"):
            raise ValueError("unknown execution phase")
        self._update("running", phase)

    def finish(self, error=None):
        now = time.time()
        with self.admission.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT owner,status,lease_until FROM submissions WHERE job=?", (self.job,)
            ).fetchone()
            if row and row["owner"] == self.owner and row["status"] == "complete":
                # An already published completion is immutable. Keep later cleanup/
                # receipt failures observable without retroactively cancelling it.
                if error is not None:
                    db.execute(
                        "INSERT INTO events(job,event,detail,at) VALUES (?,?,?,?)",
                        (self.job, "postcommit_error", type(error).__name__ + ": " + str(error)[:1500], now),
                    )
                return
            try:
                guard_request(db, self.job, self.owner, now=now)
            except (TaskCancelled, DeadlineExceeded) as control_error:
                if not isinstance(error, ProcessCleanupError):
                    error = control_error
            status = terminal_status(error)
            db.execute(
                "UPDATE submissions SET status=?,phase=?,error=?,updated=?,lease_until=0 WHERE job=? AND owner=?",
                (status, status, str(error)[:1500] if error else None, now, self.job, self.owner),
            )
            db.execute(
                "INSERT INTO events(job,event,detail,at) VALUES (?,?,?,?)",
                (self.job, "request_" + status, "", now),
            )

    def renew(self):
        now = time.time()
        with self.admission.journal.connect() as db:
            changed = db.execute(
                "UPDATE submissions SET lease_until=?,updated=? WHERE job=? AND owner=? "
                "AND lease_until>=? AND status IN ('queued','running')",
                (now + self.admission.ttl, now, self.job, self.owner, now),
            )
            if changed.rowcount != 1:
                raise BusyJob("stale request cannot renew its lease")

    def start(self):
        if self._thread is not None:
            raise RuntimeError("heartbeat already started")

        def pulse():
            while not self._stop.wait(self.admission.interval):
                try:
                    self.renew()
                except BusyJob:
                    return

        self._thread = Thread(target=pulse, name="reelpilot-admission", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
