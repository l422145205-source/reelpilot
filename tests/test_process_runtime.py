"""Real subprocess/FFmpeg faults. No global PID discovery or process-name killing."""

import json
import os
from pathlib import Path
import sys
import time

from fastapi.testclient import TestClient
import pytest

from reelpilot.admission import Admission
from reelpilot.api import create_app
from reelpilot.demo import make_demo
from reelpilot.journal import Journal
from reelpilot.control import BusyJob
from reelpilot.processes import OwnedProcess
from reelpilot.supervisor import run_isolated

HELPER = Path(__file__).with_name("runtime_child.py")
TERMINAL = {"complete", "failed", "timed_out", "cancelled", "interrupted"}


def source_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(HELPER.parents[1] / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return env


def wait_for(check, seconds=15):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("real process condition did not settle")


def marker(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def process_exited(pid):
    if os.name == "nt":
        import ctypes as c
        from ctypes import wintypes as w

        k = c.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        k.WaitForSingleObject.restype = w.DWORD
        k.CloseHandle.argtypes = [w.HANDLE]
        handle = k.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE, never terminate/query every process.
        if not handle:
            if c.get_last_error() == 87:
                return True  # PID no longer exists.
            raise c.WinError(c.get_last_error())
        try:
            return k.WaitForSingleObject(handle, 0) == 0
        finally:
            k.CloseHandle(handle)
    # An orphan zombie has exited even if PID 1 has not yet reaped its directory.
    stat = Path(f"/proc/{pid}/stat")
    try:
        if stat.exists() and stat.read_text().split(") ", 1)[1].startswith("Z"):
            return True
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def evidence(tmp_path, data):
    (tmp_path / "runtime-evidence.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


@pytest.fixture(scope="module")
def real_library(tmp_path_factory):
    return make_demo(tmp_path_factory.mktemp("runtime-media"))


def test_pipe_flood_is_drained_with_bounded_tail():
    size = 5 * 1024 * 1024
    child = OwnedProcess(
        [
            sys.executable,
            "-B",
            "-c",
            f'import sys;sys.stdout.buffer.write(b"x"*{size});sys.stderr.buffer.write(b"y"*{size})',
        ]
    )
    assert child.wait(timeout=15) == 0
    assert child.stdout.total == child.stderr.total == size
    assert child.stdout.size == child.stderr.size == 65536
    assert child.stdout.value() == "x" * 65536 and child.stderr.value() == "y" * 65536
    child.close()  # Idempotent: never signal a reused numeric process-group ID.


def test_real_process_backend_completes(real_library, tmp_path):
    start = time.monotonic()
    with TestClient(create_app(real_library.root, tmp_path / "jobs", execution_backend="process")) as c:
        assert (
            c.post(
                "/api/jobs",
                json={"job_id": "process-ok", "brief": {"text": "forest;robot", "budget_seconds": 8}},
            ).status_code
            == 202
        )
        state = wait_for(
            lambda: (s if (s := c.get("/api/jobs/process-ok").json())["status"] in TERMINAL else None)
        )
        assert state["status"] == "complete", state
        assert c.get("/api/jobs/process-ok/output").status_code == 200
        wait_for(
            lambda: any(e["event"] == "worker_reaped" for e in c.get("/api/jobs/process-ok").json()["events"])
        )
        assert "worker_started" in {e["event"] for e in state["events"]}
    evidence(
        tmp_path,
        {"case": "real_process_success", "seconds": time.monotonic() - start, "status": state["status"]},
    )


@pytest.mark.parametrize("point", ["render_committed", "result_replaced_before_commit"])
def test_hard_kill_recovers_verified_render_without_serving_uncommitted_result(real_library, tmp_path, point):
    journal = Journal(tmp_path / "jobs")
    queue = Admission(journal, ttl=1, interval=0.15)
    body = {"brief": {"text": "forest;robot", "budget_seconds": 8}, "mode": "lexical", "provider": "explicit"}
    old = queue.reserve("crash", body, timeout_seconds=30)
    old.start()
    child = OwnedProcess([sys.executable, "-B", str(HELPER), point], gated=True, env=source_env())
    try:
        child.send(
            (
                json.dumps(
                    {
                        "job_id": "crash",
                        "owner": old.owner,
                        "request": body,
                        "parent_pid": os.getpid(),
                        "jobs": str(journal.root),
                        "collection": str(real_library.root),
                        "lease": {"ttl": 1, "interval": 0.15},
                    }
                )
                + "\n"
            ).encode()
        )
        wait_for(lambda: marker(journal.root / "crash/fault-marker.json"))
        assert child.process.poll() is None
        old.stop()  # Simulate the API admission heartbeat dying with its server.
        killed = time.monotonic()
        child.close()  # OS hard kill, no Python exception handler/finally.
        assert process_exited(child.process.pid)
        assert (journal.root / "crash/result.json").exists() == (point == "result_replaced_before_commit")
        assert journal.get("crash")["status"] == "running"  # Uncommitted SQL rolled back.
        wait_for(lambda: queue.get("crash")["status"] == "interrupted", seconds=3)
        expired = time.monotonic()
        with TestClient(create_app(real_library.root, journal.root)) as c:
            assert c.get("/api/jobs/crash/output").status_code == 409
            assert "result" not in c.get("/api/jobs/crash").json()
        new = queue.reserve("crash", body, timeout_seconds=30)
        new.start()
        try:
            run_isolated(new, body, real_library)
            new.finish()
        finally:
            new.stop()
        assert journal.get("crash")["status"] == queue.get("crash")["status"] == "complete"
        render = next(s for s in journal.get("crash")["stages"] if s["stage"] == "render")
        assert render["attempts"] == 1
        with pytest.raises(BusyJob):
            old.guard()
        evidence(
            tmp_path,
            {
                "case": point,
                "fault": "OS hard kill",
                "expiry": "elapsed request lease",
                "lease_wait_seconds": expired - killed,
                "recovery_seconds": time.monotonic() - expired,
                "render_commits": render["attempts"],
                "uncommitted_result_served": False,
                "status": queue.get("crash")["status"],
            },
        )
    finally:
        old.stop()
        child.close()


@pytest.mark.parametrize("control", ["cancel", "timeout"])
def test_supervisor_reaps_running_ffmpeg_before_control_ack(real_library, tmp_path, monkeypatch, control):
    from reelpilot import supervisor

    actual = supervisor.OwnedProcess

    def instrumented(command, **kwargs):
        # Only the trusted test entry point changes. Gating, containment, request controls,
        # polling, reaping and API terminal acknowledgment are production implementations.
        return actual([sys.executable, "-B", str(HELPER), "tool"], **kwargs)

    monkeypatch.setattr(supervisor, "OwnedProcess", instrumented)
    jobs = tmp_path / "jobs"
    with TestClient(
        create_app(real_library.root, jobs, execution_backend="process", request_timeout=30)
    ) as c:
        assert (
            c.post(
                "/api/jobs", json={"job_id": "controlled", "brief": {"text": "forest", "budget_seconds": 6}}
            ).status_code
            == 202
        )
        pids = wait_for(lambda: marker(jobs / "controlled/fault-marker.json"))
        assert not process_exited(pids["tool_pid"])
        start = time.monotonic()
        if control == "cancel":
            assert c.post("/api/jobs/controlled/cancel").json()["requested"]
        else:
            # Advance only the deadline to now+.25; actual elapsed waiting triggers it.
            # Avoid tying test duration to FFmpeg's buffering/startup speed.
            with Journal(jobs).connect() as db:
                db.execute(
                    "UPDATE submissions SET deadline=? WHERE job=?", (time.time() + 0.25, "controlled")
                )
        state = wait_for(
            lambda: (s if (s := c.get("/api/jobs/controlled").json())["status"] in TERMINAL else None)
        )
        ack_seconds = time.monotonic() - start
        assert state["status"] == ("cancelled" if control == "cancel" else "timed_out"), state
        assert process_exited(pids["worker_pid"]) and process_exited(pids["tool_pid"])
        assert c.get("/api/jobs/controlled/output").status_code == 409
        if control == "cancel":
            assert (
                c.post(
                    "/api/jobs",
                    json={"job_id": "controlled", "brief": {"text": "forest", "budget_seconds": 6}},
                ).status_code
                == 409
            )
    evidence(
        tmp_path,
        {
            "case": control + "_running_ffmpeg",
            "instrumentation": "trusted long-running FFmpeg worker",
            "control_to_ack_seconds": ack_seconds,
            "worker_exited_before_ack": True,
            "ffmpeg_exited_before_ack": True,
            "status": state["status"],
        },
    )


def test_actual_supervisor_death_removes_owned_worker_and_ffmpeg(real_library, tmp_path):
    j = Journal(tmp_path / "jobs")
    q = Admission(j, ttl=2, interval=0.2)
    # Deliberately no enclosing job/session here: killing just the supervisor must
    # trigger its own containment/watchdog, rather than a test harness tree kill.
    parent = OwnedProcess(
        [
            sys.executable,
            "-B",
            str(HELPER.with_name("runtime_parent.py")),
            str(tmp_path / "parent-ready.txt"),
        ],
        gated=True,
        isolate=False,
        env=source_env(),
    )
    try:
        wait_for(lambda: (tmp_path / "parent-ready.txt").exists())
        ticket = q.reserve("parent-death", {"test": "parent death"}, timeout_seconds=30)
        parent.send(
            (
                json.dumps(
                    {
                        "job_id": ticket.job,
                        "owner": ticket.owner,
                        "jobs": str(j.root),
                        "lease": {"ttl": 2, "interval": 0.2},
                    }
                )
                + "\n"
            ).encode()
        )

        def checkpoint():
            if parent.process.poll() is not None:
                parent.close()
                raise AssertionError("probe supervisor exited: " + parent.stderr.value()[-1500:])
            return marker(j.root / ticket.job / "fault-marker.json")

        pids = wait_for(checkpoint)
        assert not process_exited(pids["worker_pid"]) and not process_exited(pids["tool_pid"])
        start = time.monotonic()
        parent.terminate_tree()  # Only our directly created supervisor PID.
        wait_for(lambda: process_exited(pids["worker_pid"]) and process_exited(pids["tool_pid"]), seconds=5)
        evidence(
            tmp_path,
            {
                "case": "supervisor_hard_kill",
                "fault": "kill direct supervisor only",
                "mechanism": "Windows Job Object" if os.name == "nt" else "cooperative POSIX parent watchdog",
                "descendant_exit_seconds": time.monotonic() - start,
                "worker_exited": True,
                "ffmpeg_exited": True,
            },
        )
    finally:
        parent.close()
