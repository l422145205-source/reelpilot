"""Trusted supervisor used to test actual parent death, not just explicit close."""

import json
import os
from pathlib import Path
import sys
import time


def main():
    payload = json.loads(sys.stdin.buffer.readline())
    from reelpilot.admission import Admission, Ticket
    from reelpilot.journal import Journal
    from reelpilot.processes import OwnedProcess

    ticket = Ticket(
        Admission(Journal(Path(payload["jobs"])), **payload["lease"]), payload["job_id"], payload["owner"]
    )
    ticket.start()
    child = OwnedProcess(
        [sys.executable, "-B", str(Path(__file__).with_name("runtime_child.py")), "tool"], gated=True
    )
    payload["parent_pid"] = os.getpid()
    child.send((json.dumps(payload) + "\n").encode())
    while True:
        time.sleep(0.1)  # The test kills this process without running cleanup.


if __name__ == "__main__":
    main()
