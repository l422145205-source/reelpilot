"""Gated entry point. No model/tool import happens before containment is assigned."""

import json
import os
import signal
import sys
from threading import Thread
import time


def watch_parent(expected):
    if os.name == "nt":
        return  # The parent's non-inherited Job handle is the kernel lifetime boundary.

    def watch():
        while True:
            if os.getppid() != expected:
                os.killpg(os.getpgrp(), signal.SIGKILL)
                return
            time.sleep(0.1)

    Thread(target=watch, name="reelpilot-parent-watch", daemon=True).start()


def main():
    # Parent sends this only after assigning the process to its private Job/session.
    line = sys.stdin.buffer.readline(65537)
    if not line or len(line) > 65536:
        return 2
    payload = json.loads(line)
    watch_parent(payload["parent_pid"])
    from contextlib import redirect_stdout

    receipt = {"job_id": payload["job_id"]}
    try:
        with redirect_stdout(sys.stderr):
            from pathlib import Path
            from . import processes
            from .admission import Admission, Ticket
            from .execution import run_request
            from .journal import Journal
            from .library import Library, stable_hash
            from .schema import JobRequest

            processes.INSIDE_WORKER = True
            journal = Journal(Path(payload["jobs"]))
            admission = Admission(journal, **payload["lease"])
            ticket = Ticket(admission, payload["job_id"], payload["owner"])
            ticket.guard()
            with journal.connect() as db:
                origin = db.execute("SELECT input_hash FROM requests WHERE job=?", (ticket.job,)).fetchone()
            if not origin or origin[0] != stable_hash(payload["request"]):
                raise ValueError("worker payload differs from durable original receipt")
            library = Library(Path(payload["collection"]))
            if library.hash != payload["collection_hash"]:
                raise ValueError("collection changed after admission")
            body = JobRequest.model_validate(payload["request"])
            run_request(ticket, body, library, **payload["models"])
        receipt["status"] = "complete"
        print(json.dumps(receipt), flush=True)
        return 0
    except Exception as exc:
        receipt.update(status="failed", error=type(exc).__name__ + ": " + str(exc)[:1500])
        print(json.dumps(receipt), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
