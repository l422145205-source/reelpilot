"""SQLite stage journal with expiring ownership leases and content-verified replay.

Subprocesses may execute more than once after a crash. Atomic publication and
content checks make replay safe; this is not an exactly-once execution claim.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
import uuid
from threading import Event, Thread

from .library import digest, stable_hash, within
from .schema import Identifier
from pydantic import TypeAdapter


class BusyJob(RuntimeError):
    pass


class Journal:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "journal.sqlite"
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, spec TEXT NOT NULL,
              status TEXT NOT NULL, owner TEXT, lease_until REAL NOT NULL DEFAULT 0,
              error TEXT, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS stages (
              job TEXT NOT NULL, stage TEXT NOT NULL, input_hash TEXT NOT NULL,
              result TEXT NOT NULL, attempts INTEGER NOT NULL,
              PRIMARY KEY(job, stage));
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, event TEXT,
              detail TEXT, at REAL);
            CREATE TABLE IF NOT EXISTS requests (
              job TEXT PRIMARY KEY, input_hash TEXT NOT NULL, body TEXT NOT NULL);
            """)

    def connect(self):
        db = sqlite3.connect(self.db, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def event(self, job, name, detail=""):
        with self.connect() as db:
            db.execute("INSERT INTO events(job,event,detail,at) VALUES (?,?,?,?)",
                       (job, name, str(detail)[:2000], time.time()))

    def create(self, job: str, spec: dict):
        TypeAdapter(Identifier).validate_python(job)
        hashed = stable_hash(spec)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO jobs(id,input_hash,spec,status,updated) VALUES (?,?,?,?,?)",
                       (job, hashed, json.dumps(spec, ensure_ascii=False), "queued", time.time()))
            existing = db.execute("SELECT input_hash FROM jobs WHERE id=?", (job,)).fetchone()
            if existing[0] != hashed:
                raise ValueError("idempotency key already belongs to a different request")

    def reserve_request(self, job: str, body: dict):
        TypeAdapter(Identifier).validate_python(job)
        hashed = stable_hash(body)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO requests(job,input_hash,body) VALUES (?,?,?)",
                       (job, hashed, json.dumps(body, ensure_ascii=False)))
            if db.execute("SELECT input_hash FROM requests WHERE job=?", (job,)).fetchone()[0] != hashed:
                raise ValueError("idempotency key belongs to a different original brief")

    def is_live(self, job: str):
        with self.connect() as db:
            row = db.execute("SELECT owner,lease_until FROM jobs WHERE id=?", (job,)).fetchone()
            return bool(row and row[0] and row[1] >= time.time())

    def claim(self, job: str, ttl=120) -> str:
        owner = uuid.uuid4().hex
        now = time.time()
        with self.connect() as db:
            updated = db.execute("UPDATE jobs SET owner=?,lease_until=?,status='running',error=NULL,updated=? "
                                 "WHERE id=? AND (owner IS NULL OR lease_until < ?)",
                                 (owner, now+ttl, now, job, now))
            if updated.rowcount != 1:
                raise BusyJob("job has another live worker")
        self.event(job, "claimed")
        return owner

    def renew(self, job, owner, ttl=120):
        now = time.time()
        with self.connect() as db:
            cursor = db.execute("UPDATE jobs SET lease_until=?,updated=? WHERE id=? AND owner=? AND lease_until>=?",
                                (now+ttl, now, job, owner, now))
            if cursor.rowcount != 1:
                raise BusyJob("stale worker cannot renew a lease")

    def start_heartbeat(self, job, owner, *, ttl=120, interval=10):
        stop = Event()
        def pulse():
            while not stop.wait(interval):
                try:
                    self.renew(job, owner, ttl)
                except BusyJob:
                    self.event(job, "lease_lost")
                    return
        thread = Thread(target=pulse, name="reelpilot-lease", daemon=True)
        thread.start()
        def finish():
            stop.set()
            thread.join(timeout=2)
        return finish

    def guard(self, job, owner):
        with self.connect() as db:
            row = db.execute("SELECT owner,lease_until FROM jobs WHERE id=?", (job,)).fetchone()
            if not row or row[0] != owner or row[1] < time.time():
                raise BusyJob("worker lease expired or was replaced")

    def release(self, job, owner, *, error=None):
        with self.connect() as db:
            cursor = db.execute("UPDATE jobs SET owner=NULL,lease_until=0,status=?,error=?,updated=? "
                                 "WHERE id=? AND owner=?", ("failed" if error else "complete",
                                 str(error)[:1500] if error else None, time.time(), job, owner))
            if cursor.rowcount != 1:
                raise BusyJob("worker lost job ownership")

    def stage(self, job, owner, name, inputs, operation):
        self.guard(job, owner)
        hashed = stable_hash(inputs)
        with self.connect() as db:
            row = db.execute("SELECT * FROM stages WHERE job=? AND stage=?", (job, name)).fetchone()
        if row and row["input_hash"] == hashed:
            cached = json.loads(row["result"])
            valid = all(within(self.root, p).is_file() and digest(within(self.root, p)) == sha
                        for p, sha in cached.get("artifacts", {}).items())
            if valid:
                self.event(job, "cache_hit", name)
                return cached
        self.event(job, "stage_started", name)
        result = operation()
        self.guard(job, owner)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner,lease_until FROM jobs WHERE id=?", (job,)).fetchone()
            if not row or row[0] != owner or row[1] < time.time():
                raise BusyJob("worker lost ownership before committing its stage")
            db.execute("INSERT INTO stages(job,stage,input_hash,result,attempts) VALUES (?,?,?,?,1) "
                       "ON CONFLICT(job,stage) DO UPDATE SET input_hash=excluded.input_hash, "
                       "result=excluded.result,attempts=stages.attempts+1",
                       (job, name, hashed, json.dumps(result, ensure_ascii=False)))
        self.event(job, "stage_completed", name)
        return result

    def publish(self, job, owner, temporary: Path, destination: Path):
        if not temporary.resolve().is_relative_to(self.root) or not destination.resolve().is_relative_to(self.root):
            raise ValueError("artifact publication escapes job storage")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner,lease_until FROM jobs WHERE id=?", (job,)).fetchone()
            if not row or row[0] != owner or row[1] < time.time():
                raise BusyJob("stale worker cannot publish an artifact")
            temporary.replace(destination)

    def get(self, job):
        with self.connect() as db:
            row = db.execute("SELECT id,status,error,spec,updated FROM jobs WHERE id=?", (job,)).fetchone()
            if row is None:
                raise KeyError(job)
            out = dict(row)
            out["spec"] = json.loads(out["spec"])
            out["events"] = [dict(r) for r in db.execute(
                "SELECT seq,event,detail,at FROM events WHERE job=? ORDER BY seq", (job,))]
            out["stages"] = [dict(r) for r in db.execute(
                "SELECT stage,attempts FROM stages WHERE job=?", (job,))]
            return out

