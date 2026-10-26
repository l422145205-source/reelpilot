from concurrent.futures import ThreadPoolExecutor
from threading import Event
import json
import time

from fastapi.testclient import TestClient
import numpy as np

from reelpilot import api, encoders, planner
from reelpilot.admission import Admission
from reelpilot.api import create_app
from reelpilot.control import TaskCancelled
from reelpilot.journal import Journal
from reelpilot.schema import explicit_spec
from test_model_resources import until


def terminal(client, job):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        s = client.get("/api/jobs/" + job).json()
        if s["status"] in ("complete", "failed", "cancelled", "timed_out", "interrupted"):
            return s
        time.sleep(0.01)
    raise AssertionError(s)


def dense_index(library, fingerprint):
    ids = list(library.segments)
    np.savez_compressed(
        library.root / "vectors_image.npz", ids=np.array(ids), vectors=np.array([[1, 0]] * len(ids))
    )
    (library.root / "vectors_image.json").write_text(
        json.dumps({"collection_hash": library.hash, "fingerprint": fingerprint}), encoding="utf-8"
    )


def test_lexical_search_is_not_blocked_by_active_planner(library, tmp_path, monkeypatch):
    entered, release = Event(), Event()

    def blocked(brief, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return explicit_spec(brief)

    monkeypatch.setattr(planner, "transformers_spec", blocked)
    monkeypatch.setattr(api, "run_job", lambda *a, **k: None)
    with TestClient(create_app(library.root, tmp_path / "jobs", planner_path="controlled-planner")) as c:
        c.post(
            "/api/jobs", json={"job_id": "planner", "provider": "transformers", "brief": {"text": "forest"}}
        )
        assert entered.wait(3)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                search = pool.submit(c.post, "/api/search", json={"query": "forest", "mode": "lexical"})
                response = search.result(timeout=2)
                assert response.status_code == 200 and response.json()["hits"]
        finally:
            release.set()
        assert terminal(c, "planner")["status"] == "complete"


def test_cancelled_dense_waiter_never_loads_model(library, tmp_path, monkeypatch):
    _, fp = encoders.embedding_identity("controlled-model")
    dense_index(library, fp)
    loads = []
    monkeypatch.setattr(encoders, "QwenEncoder", lambda *a, **k: loads.append(True))
    app = create_app(library.root, tmp_path / "jobs", encoder_path="controlled-model")
    with TestClient(app) as c:
        with app.state.resources.gate.lease():
            c.post("/api/jobs", json={"job_id": "waiting", "mode": "dense", "brief": {"text": "forest"}})
            until(lambda: app.state.resources.stats()["gate"]["waiting"] == 1)
            c.post("/api/jobs/waiting/cancel")
            assert terminal(c, "waiting")["status"] == "cancelled"
            assert loads == []
        assert app.state.resources.cache.stats()["pending"] == 0


def test_search_backpressure_and_shutdown_have_distinct_http_status(library, tmp_path, monkeypatch):
    _, fp = encoders.embedding_identity("controlled-model")
    dense_index(library, fp)

    class Encoder:
        fingerprint = fp

        def encode(self, *, text, check):
            check()
            return [1, 0]

        def close(self):
            pass

    monkeypatch.setattr(encoders, "QwenEncoder", lambda *a, **k: Encoder())
    app = create_app(library.root, tmp_path / "jobs", encoder_path="controlled-model", model_waiters=1)
    with TestClient(app) as c:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with app.state.resources.gate.lease():
                first = pool.submit(c.post, "/api/search", json={"query": "forest", "mode": "dense"})
                until(lambda: app.state.resources.stats()["gate"]["waiting"] == 1)
                rejected = c.post("/api/search", json={"query": "robot", "mode": "dense"})
                assert rejected.status_code == 429 and rejected.headers["retry-after"] == "1"
            assert first.result(timeout=3).status_code == 200
        app.state.resources.begin_shutdown()
        assert c.post("/api/search", json={"query": "forest", "mode": "dense"}).status_code == 503
        assert c.post("/api/jobs", json={"brief": {"text": "forest"}}).status_code == 503
        assert c.get("/api/health").json()["status"] == "draining"


def test_shutdown_persists_active_and_queued_as_resumable_interrupted(library, tmp_path, monkeypatch):
    entered, release = Event(), Event()
    calls = []

    def blocked(brief, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return explicit_spec(brief)

    monkeypatch.setattr(planner, "transformers_spec", blocked)
    monkeypatch.setattr(api, "run_job", lambda *a, **k: calls.append(True))
    jobs = tmp_path / "jobs"
    app = create_app(library.root, jobs, planner_path="controlled-planner", workers=1)
    with TestClient(app) as c:
        body = {"provider": "transformers", "brief": {"text": "forest"}}
        c.post("/api/jobs", json={**body, "job_id": "active"})
        assert entered.wait(3)
        c.post("/api/jobs", json={**body, "job_id": "queued"})
        app.state.resources.begin_shutdown()
        release.set()
        assert terminal(c, "active")["status"] == terminal(c, "queued")["status"] == "interrupted"
    assert calls == []
    reopened = Admission(Journal(jobs))
    assert reopened.get("active")["status"] == reopened.get("queued")["status"] == "interrupted"
    # Original normalized request is retained; changing the provider is not a recovery.
    with reopened.journal.connect() as db:
        original = json.loads(db.execute("SELECT body FROM requests WHERE job='active'").fetchone()[0])
    retry = reopened.reserve("active", original, timeout_seconds=30)
    assert reopened.get("active")["attempts"] == 2
    retry.finish(error="controlled test cleanup")


def test_runtime_shutdown_guard_fences_artifact_publication(tmp_path):
    from reelpilot.control import ServiceDraining

    stop = Event()

    def guard():
        if stop.is_set():
            raise ServiceDraining("closing")

    j = Journal(tmp_path, runtime_check=guard)
    q = Admission(j)
    ticket = q.reserve("job", {"q": 1})
    j.create("job", {"resolved": 1})
    owner = j.claim("job", request_owner=ticket.owner)
    partial = j.root / "partial"
    partial.write_text("{}")
    stop.set()
    import pytest

    with pytest.raises(ServiceDraining):
        j.finalize("job", owner, partial, j.root / "result.json")
    assert not (j.root / "result.json").exists()
    ticket.finish(error=ServiceDraining("closing"))
    assert q.get("job")["status"] == "interrupted"
