"""Real supervised FFmpeg shutdown and restart, not a mocked process exit."""

import json
import sys
import time

from fastapi.testclient import TestClient

from reelpilot.api import create_app
from reelpilot.demo import make_demo
from test_process_runtime import HELPER, marker, process_exited, wait_for


def test_service_drain_stops_real_tool_and_restart_recovers_original_request(tmp_path, monkeypatch):
    from reelpilot import supervisor

    actual = supervisor.OwnedProcess
    library = make_demo(tmp_path / "media")
    jobs = tmp_path / "jobs"
    body = {"job_id": "shutdown", "brief": {"text": "forest;robot", "budget_seconds": 8}}

    def long_worker(command, **kwargs):
        return actual([sys.executable, "-B", str(HELPER), "tool"], **kwargs)

    monkeypatch.setattr(supervisor, "OwnedProcess", long_worker)
    app = create_app(library.root, jobs, execution_backend="process")
    with TestClient(app) as c:
        assert c.post("/api/jobs", json=body).status_code == 202
        pids = wait_for(lambda: marker(jobs / "shutdown/fault-marker.json"))
        start = time.monotonic()
        app.state.resources.begin_shutdown()
        state = wait_for(
            lambda: (s if (s := c.get("/api/jobs/shutdown").json())["status"] == "interrupted" else None)
        )
        elapsed = time.monotonic() - start
        assert process_exited(pids["worker_pid"]) and process_exited(pids["tool_pid"])
        assert c.get("/api/jobs/shutdown/output").status_code == 409
    monkeypatch.setattr(supervisor, "OwnedProcess", actual)
    with TestClient(create_app(library.root, jobs, execution_backend="process")) as c:
        assert c.get("/api/jobs/shutdown").json()["status"] == "interrupted"
        assert c.post("/api/jobs", json=body).status_code == 202
        recovered = wait_for(
            lambda: (
                s if (s := c.get("/api/jobs/shutdown").json())["status"] in ("complete", "failed") else None
            )
        )
        assert recovered["status"] == "complete", recovered
        assert recovered["submission"]["attempts"] == 2
        assert c.get("/api/jobs/shutdown/output").status_code == 200
    (tmp_path / "resource-evidence.json").write_text(
        json.dumps(
            {
                "case": "real_ffmpeg_drain_restart",
                "shutdown_to_interrupted_seconds": elapsed,
                "worker_exited": True,
                "ffmpeg_exited": True,
                "restart_status": recovered["status"],
                "request_attempts": 2,
                "instrumentation": "trusted long-running real FFmpeg worker; production supervisor and request controls",
            }
        ),
        encoding="utf-8",
    )
