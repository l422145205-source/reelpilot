"""Durable request admission before planning, with bounded expiring ownership.

The pipeline journal fences published artifacts separately. This queue provides
one receipt per original request, not exactly-once model/tool execution.
"""
from __future__ import annotations

import json
import time
import uuid
from threading import Event, Thread

from pydantic import TypeAdapter

from .journal import BusyJob, Journal
from .library import stable_hash
from .schema import Identifier


class QueueFull(RuntimeError):
    pass


class Admission:
    def __init__(self, journal: Journal, *, capacity=8, ttl=120, interval=10):
        if capacity < 1 or ttl <= 0 or not 0 < interval < ttl:
            raise ValueError("invalid admission capacity or lease timing")
        self.journal, self.capacity = journal, capacity
        self.ttl, self.interval = ttl, interval
        with journal.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS submissions (
                job TEXT PRIMARY KEY, status TEXT NOT NULL, phase TEXT NOT NULL,
                owner TEXT, lease_until REAL NOT NULL, attempts INTEGER NOT NULL,
                queued REAL NOT NULL, updated REAL NOT NULL, error TEXT)""")

    def reserve(self, job: str, body: dict):
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
            old = db.execute("SELECT status,lease_until FROM submissions WHERE job=?", (job,)).fetchone()
            if old and old[0] in ("queued", "running") and old[1] >= now:
                raise BusyJob("request already has an active worker or queue slot")
            count = db.execute("SELECT count(*) FROM submissions WHERE status IN ('queued','running') "
                               "AND lease_until>=?", (now,)).fetchone()[0]
            if count >= self.capacity:
                raise QueueFull("request capacity reached; retry later with the same job ID")
            db.execute("INSERT OR IGNORE INTO requests(job,input_hash,body) VALUES (?,?,?)",
                       (job, hashed, json.dumps(body, ensure_ascii=False)))
            db.execute("""INSERT INTO submissions VALUES (?,?,?,?,?,1,?,?,NULL)
                ON CONFLICT(job) DO UPDATE SET status='queued',phase='queued',
                owner=excluded.owner,lease_until=excluded.lease_until,
                attempts=submissions.attempts+1,queued=excluded.queued,
                updated=excluded.updated,error=NULL""",
                       (job, "queued", "queued", owner, now+self.ttl, now, now))
            db.execute("INSERT INTO events(job,event,detail,at) VALUES (?,?,?,?)",
                       (job, "admitted", "durable request receipt", now))
        return Ticket(self, job, owner)

    def get(self, job):
        with self.journal.connect() as db:
            row = db.execute("SELECT job,status,phase,attempts,queued,updated,error,lease_until "
                             "FROM submissions WHERE job=?", (job,)).fetchone()
        if row is None:
            raise KeyError(job)
        result = dict(row)
        if result["status"] in ("queued", "running") and result.pop("lease_until") < time.time():
            result["status"] = "interrupted"
            result["error"] = "worker lease expired; resubmit the identical original request to recover"
        else:
            result.pop("lease_until", None)
        return result


class Ticket:
    def __init__(self, admission, job, owner):
        self.admission, self.job, self.owner = admission, job, owner
        self._stop = Event()
        self._thread = None

    def guard(self):
        with self.admission.journal.connect() as db:
            row = db.execute("SELECT owner,lease_until,status FROM submissions WHERE job=?",
                             (self.job,)).fetchone()
        if not row or row[0] != self.owner or row[1] < time.time() or row[2] not in ("queued", "running"):
            raise BusyJob("request ownership expired or was replaced")

    def _update(self, status, phase, error=None):
        now = time.time()
        with self.admission.journal.connect() as db:
            changed = db.execute("UPDATE submissions SET status=?,phase=?,error=?,updated=?,lease_until=? "
                                 "WHERE job=? AND owner=? AND lease_until>=? "
                                 "AND status IN ('queued','running')",
                                 (status, phase, str(error)[:1500] if error else None, now,
                                  now+self.admission.ttl if status == "running" else 0,
                                  self.job, self.owner, now))
            if changed.rowcount != 1:
                raise BusyJob("stale request cannot update its successor")
        self.admission.journal.event(self.job, "request_"+phase)

    def phase(self, phase):
        if phase not in ("planning", "executing"):
            raise ValueError("unknown execution phase")
        self._update("running", phase)

    def finish(self, error=None):
        self._update("failed" if error else "complete", "failed" if error else "complete", error)

    def renew(self):
        now = time.time()
        with self.admission.journal.connect() as db:
            changed = db.execute("UPDATE submissions SET lease_until=?,updated=? WHERE job=? AND owner=? "
                                 "AND lease_until>=? AND status IN ('queued','running')",
                                 (now+self.admission.ttl, now, self.job, self.owner, now))
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
