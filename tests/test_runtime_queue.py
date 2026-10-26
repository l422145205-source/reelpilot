"""Thread mode is cooperative: control intent fences results without killing Python."""

from threading import Event
import time

from fastapi.testclient import TestClient

from reelpilot import api, planner
from reelpilot.admission import Admission
from reelpilot.api import create_app
from reelpilot.journal import Journal
from reelpilot.schema import explicit_spec


def wait_for(client, job, status, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get("/api/jobs/" + job).json()
        if state["status"] == status:
            return state
        time.sleep(0.01)
    raise AssertionError(state)


def test_cancelled_queued_request_keeps_capacity_until_ack(library, tmp_path, monkeypatch):
    entered, release = Event(), Event()
    calls = []

    def blocked_pipeline(*args, **kwargs):
        calls.append(args[3])
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(api, "run_job", blocked_pipeline)
    request = {"brief": {"text": "forest", "budget_seconds": 6}}
    with TestClient(create_app(library.root, tmp_path / "jobs", workers=1, max_inflight=2)) as c:
        try:
            assert c.post("/api/jobs", json={**request, "job_id": "blocked"}).status_code == 202
            assert entered.wait(3)
            assert c.post("/api/jobs", json={**request, "job_id": "queued"}).status_code == 202
            assert c.post("/api/jobs/queued/cancel").json()["requested"]
            assert c.get("/api/jobs/queued").json()["phase"] == "cancelling"
            assert c.post("/api/jobs", json={**request, "job_id": "overflow"}).status_code == 429
        finally:
            release.set()
        wait_for(c, "queued", "cancelled")
        assert calls == ["blocked"]
        assert c.post("/api/jobs", json={**request, "job_id": "next"}).status_code == 202
        wait_for(c, "next", "complete")


def test_blocking_model_cannot_enter_pipeline_after_cancel(library, tmp_path, monkeypatch):
    entered, release = Event(), Event()
    pipeline_calls = []

    def blocked_model(brief, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return explicit_spec(brief)

    monkeypatch.setattr(planner, "transformers_spec", blocked_model)
    monkeypatch.setattr(api, "run_job", lambda *a, **k: pipeline_calls.append(True))
    jobs = tmp_path / "jobs"
    with TestClient(create_app(library.root, jobs, planner_path="test-double-no-model")) as c:
        try:
            c.post(
                "/api/jobs",
                json={"job_id": "planning", "provider": "transformers", "brief": {"text": "forest"}},
            )
            assert entered.wait(3)
            c.post("/api/jobs/planning/cancel")
            state = c.get("/api/jobs/planning").json()
            assert state["status"] == "running" and state["phase"] == "cancelling"
            assert c.get("/api/jobs/planning/output").status_code == 409
        finally:
            release.set()
        wait_for(c, "planning", "cancelled")
    assert pipeline_calls == []
    assert Admission(Journal(jobs)).get("planning")["status"] == "cancelled"


def test_attempt_deadline_counts_queue_wait(library, tmp_path, monkeypatch):
    entered, release = Event(), Event()
    calls = []

    def blocked_pipeline(*args, **kwargs):
        calls.append(args[3])
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(api, "run_job", blocked_pipeline)
    with TestClient(create_app(library.root, tmp_path / "jobs", workers=1)) as c:
        try:
            c.post("/api/jobs", json={"job_id": "first", "brief": {"text": "forest"}})
            assert entered.wait(3)
            c.post(
                "/api/jobs", json={"job_id": "short", "timeout_seconds": 0.05, "brief": {"text": "forest"}}
            )
            time.sleep(0.1)  # Real elapsed queue deadline; no SQL clock manipulation.
        finally:
            release.set()
        wait_for(c, "short", "timed_out")
        assert calls == ["first"]
