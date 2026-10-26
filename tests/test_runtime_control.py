from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
import sqlite3
import time

from fastapi.testclient import TestClient
import pytest

from reelpilot.admission import Admission
from reelpilot.api import create_app
from reelpilot.control import BusyJob, TaskCancelled, DeadlineExceeded, ProcessCleanupError
from reelpilot.journal import Journal


def linked(tmp_path, name="job", timeout=30):
    journal = Journal(tmp_path)
    admission = Admission(journal)
    ticket = admission.reserve(name, {"original": name}, timeout_seconds=timeout)
    journal.create(name, {"resolved": name})
    owner = journal.claim(name, request_owner=ticket.owner)
    return journal, admission, ticket, owner


def test_migrate_v02_schema_preserves_original_receipt(tmp_path):
    with sqlite3.connect(tmp_path / "journal.sqlite") as db:
        db.executescript(
            """CREATE TABLE jobs(id TEXT PRIMARY KEY,input_hash TEXT NOT NULL,spec TEXT NOT NULL,
          status TEXT NOT NULL,owner TEXT,lease_until REAL NOT NULL DEFAULT 0,error TEXT,updated REAL NOT NULL);
          CREATE TABLE submissions(job TEXT PRIMARY KEY,status TEXT NOT NULL,phase TEXT NOT NULL,
          owner TEXT,lease_until REAL NOT NULL,attempts INTEGER NOT NULL,queued REAL NOT NULL,updated REAL NOT NULL,error TEXT);
          INSERT INTO submissions VALUES('old','failed','failed',NULL,0,1,1,1,'original failure');"""
        )
    admission = Admission(Journal(tmp_path))
    assert admission.get("old")["error"] == "original failure"
    assert admission.get("old")["cancel_requested"] == 0
    with admission.journal.connect() as db:
        assert "request_owner" in {r["name"] for r in db.execute("PRAGMA table_info(jobs)")}


def test_cancel_fences_stage_artifact_and_final_publication(tmp_path):
    j, q, t, owner = linked(tmp_path)
    partial = j.root / "partial"
    partial.write_text("not committed")
    assert q.cancel("job")["requested"]
    for action in [
        t.guard,
        lambda: j.renew("job", owner),
        lambda: j.publish("job", owner, partial, j.root / "output"),
        lambda: j.finalize("job", owner, partial, j.root / "result.json"),
    ]:
        with pytest.raises(TaskCancelled):
            action()
    assert not (j.root / "output").exists() and partial.exists()
    with pytest.raises(TaskCancelled):
        j.stage("job", owner, "cancel_during_operation", {}, lambda: {})
    t.finish()
    assert q.get("job")["status"] == "cancelled"
    with pytest.raises(ValueError, match="terminal"):
        q.reserve("job", {"original": "job"})


def test_cancel_during_operation_prevents_stage_commit(tmp_path):
    j, q, t, owner = linked(tmp_path)

    def operation():
        q.cancel("job")
        return {"value": "computed but not committed"}

    with pytest.raises(TaskCancelled):
        j.stage("job", owner, "work", {}, operation)
    assert j.get("job")["stages"] == []
    t.finish()


def test_cancel_tombstone_survives_server_death_before_ack(tmp_path):
    j, q, t, owner = linked(tmp_path)
    q.cancel("job")
    # This is a deterministic lease-state unit test; real elapsed expiry is tested separately.
    with j.connect() as db:
        db.execute("UPDATE submissions SET lease_until=0 WHERE job='job'")
    reopened = Admission(Journal(tmp_path))
    assert reopened.get("job")["status"] == "cancelled"
    with pytest.raises(ValueError, match="terminal"):
        reopened.reserve("job", {"original": "job"})
    with pytest.raises(BusyJob):
        j.guard("job", owner)


def test_expired_deadline_can_retry_with_new_attempt_budget(tmp_path):
    j, q, t, owner = linked(tmp_path)
    with j.connect() as db:
        db.execute("UPDATE submissions SET deadline=0 WHERE job='job'")
    with pytest.raises(DeadlineExceeded):
        t.guard()
    t.finish()
    assert q.get("job")["status"] == "timed_out"
    replacement = q.reserve("job", {"original": "job"}, timeout_seconds=60)
    successor = j.claim("job", request_owner=replacement.owner)
    assert successor != owner
    with pytest.raises(BusyJob):
        j.guard("job", owner)
    j.release("job", successor, error="test cleanup")
    replacement.finish(error="test cleanup")


@pytest.mark.parametrize("first", ["cancel", "deadline"])
def test_first_control_event_wins(tmp_path, first):
    j, q, t, owner = linked(tmp_path)
    now = time.time()
    with j.connect() as db:
        db.execute(
            "UPDATE submissions SET deadline=?,cancel_requested=1,cancel_at=? WHERE job=?",
            (now - 1, now - 2 if first == "cancel" else now, "job"),
        )
    t.finish()
    assert q.get("job")["status"] == ("cancelled" if first == "cancel" else "timed_out")


def test_cancel_and_complete_linearize_on_same_transaction(tmp_path):
    j, q, t, owner = linked(tmp_path)
    partial = j.root / "partial"
    partial.write_text("{}")
    inside, release = Event(), Event()

    def checkpoint(name):
        inside.set()
        assert release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        final = pool.submit(j.finalize, "job", owner, partial, j.root / "result.json", checkpoint=checkpoint)
        assert inside.wait(3)
        cancel = pool.submit(q.cancel, "job")
        time.sleep(0.05)
        assert not cancel.done()  # Rename alone is not an acknowledged completion.
        release.set()
        final.result(timeout=5)
        assert cancel.result(timeout=5) == {"job_id": "job", "status": "complete", "requested": False}
    assert q.get("job")["status"] == j.get("job")["status"] == "complete"


def test_competing_cancel_and_complete_never_publish_after_cancel(tmp_path):
    for i in range(20):
        name = f"race-{i}"
        j, q, t, owner = linked(tmp_path, name)
        partial = j.root / f"partial-{i}"
        partial.write_text("{}")
        target = j.root / f"result-{i}.json"
        barrier = Barrier(2)

        def complete():
            barrier.wait(timeout=5)
            try:
                j.finalize(name, owner, partial, target)
                return True
            except TaskCancelled:
                return False

        def cancel():
            barrier.wait(timeout=5)
            return q.cancel(name)

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(complete)
            b = pool.submit(cancel)
            published, answer = a.result(timeout=5), b.result(timeout=5)
        if answer["requested"]:
            assert not published and not target.exists()
            t.finish()
            assert q.get(name)["status"] == "cancelled"
        else:
            assert published and target.exists() and q.get(name)["status"] == "complete"


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_api_rejects_invalid_deadline(library, tmp_path, timeout):
    # JSON itself forbids NaN/Infinity. Exercise the schema without a lossy client encoder.
    from pydantic import ValidationError
    from reelpilot.schema import JobRequest

    with pytest.raises(ValidationError):
        JobRequest(brief={"text": "forest"}, timeout_seconds=timeout)


def test_unknown_cancel_and_completed_cancel_are_unambiguous(library, tmp_path):
    jobs = tmp_path / "jobs"
    with TestClient(create_app(library.root, jobs)) as client:
        assert client.post("/api/jobs/missing/cancel").status_code == 404
        q = Admission(Journal(jobs))
        t = q.reserve("done", {"test": True})
        t.finish()
        assert client.post("/api/jobs/done/cancel").json()["requested"] is False


def test_unconfirmed_process_cleanup_is_failed_instead_of_cancel_ack(tmp_path):
    j, q, t, owner = linked(tmp_path)
    q.cancel("job")
    t.finish(error=ProcessCleanupError("termination not confirmed"))
    assert q.get("job")["status"] == "failed"
    assert "termination not confirmed" in q.get("job")["error"]
    with pytest.raises(ValueError, match="terminal"):
        q.reserve("job", {"original": "job"})


def test_cleanup_failure_is_not_silently_erased_on_second_close():
    from reelpilot.processes import OwnedProcess

    class FailingJob:
        calls = 0

        def kill_and_wait(self):
            self.calls += 1
            raise OSError("injected OS confirmation failure")

    child = OwnedProcess.__new__(OwnedProcess)
    job = child.job = FailingJob()
    child._terminated = False
    child._termination_error = None
    for _ in range(2):
        with pytest.raises(ProcessCleanupError):
            child.terminate_tree()
    assert job.calls == 1  # No retry against a potentially reused process/group ID.


def test_postcommit_cleanup_error_is_observable_without_rewriting_completion(tmp_path):
    j, q, t, owner = linked(tmp_path)
    partial = j.root / "final"
    partial.write_text("{}")
    j.finalize("job", owner, partial, j.root / "result.json")
    t.finish(error=ProcessCleanupError("late cleanup failure"))
    assert q.get("job")["status"] == j.get("job")["status"] == "complete"
    event = next(e for e in j.get("job")["events"] if e["event"] == "postcommit_error")
    assert "ProcessCleanupError" in event["detail"]
