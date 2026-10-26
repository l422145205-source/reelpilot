"""Optional per-job process backend. Models are cold-loaded in the child."""

from pathlib import Path
import json
import os
import sys
import time

from .execution import checked_lock
from .processes import OwnedProcess


def run_isolated(
    ticket,
    request,
    library,
    *,
    model_lock=None,
    encoder_path=None,
    planner_path=None,
    ollama_model="qwen3:4b",
    device="cuda"
):
    journal = ticket.admission.journal
    uses_models = request["provider"] != "explicit" or request["mode"] != "lexical"
    with checked_lock(model_lock if uses_models else None, ticket.guard):
        env = dict(os.environ)
        source = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = source + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        child = OwnedProcess([sys.executable, "-B", "-m", "reelpilot.worker"], gated=True, env=env)
        try:
            journal.event(
                ticket.job, "worker_started", json.dumps({"pid": child.process.pid, "backend": "process"})
            )
            payload = {
                "job_id": ticket.job,
                "owner": ticket.owner,
                "request": request,
                "parent_pid": os.getpid(),
                "collection": str(library.root),
                "collection_hash": library.hash,
                "jobs": str(journal.root),
                "lease": {"ttl": ticket.admission.ttl, "interval": ticket.admission.interval},
                "models": {
                    "encoder_path": str(encoder_path) if encoder_path else None,
                    "planner_path": str(planner_path) if planner_path else None,
                    "ollama_model": ollama_model,
                    "device": device,
                },
            }
            child.send((json.dumps(payload) + "\n").encode())
            completed_at = None

            def check():
                nonlocal completed_at
                if ticket.worker_state() == "complete":
                    completed_at = completed_at or time.monotonic()
                    if time.monotonic() - completed_at > 5:
                        raise TimeoutError("completed worker cleanup exceeded grace")

            code = child.wait(check=check, timeout=3605)
            if child.stdout.total > 65536:
                raise RuntimeError("worker receipt exceeded protocol limit")
            try:
                receipt = json.loads(child.stdout.value())
            except (json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError("worker returned an invalid receipt") from exc
            if code or receipt.get("job_id") != ticket.job or receipt.get("status") != "complete":
                raise RuntimeError(receipt.get("error", "worker exited without completion"))
            if not ticket.completed_by_self():
                raise RuntimeError("worker has no matching committed completion")
        except BaseException as exc:
            child.close()
            journal.abandon_request(ticket.job, ticket.owner, exc)
            raise
        finally:
            child.close()
            journal.event(
                ticket.job,
                "worker_reaped",
                json.dumps(
                    {
                        "returncode": child.process.returncode,
                        "stderr_bytes": child.stderr.total,
                        "retained_stderr_bytes": child.stderr.size,
                    }
                ),
            )
