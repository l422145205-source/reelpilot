"""Trusted fault worker; never selected through an API request or model output."""

import json
import os
from pathlib import Path
import sys
import time


def marker(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())


def main():
    payload = json.loads(sys.stdin.buffer.readline(65537))
    from reelpilot import processes
    from reelpilot.worker import watch_parent
    from reelpilot.admission import Admission, Ticket
    from reelpilot.journal import Journal

    processes.INSIDE_WORKER = True
    watch_parent(payload["parent_pid"])
    journal = Journal(Path(payload["jobs"]))
    ticket = Ticket(Admission(journal, **payload["lease"]), payload["job_id"], payload["owner"])
    ticket.phase("executing")
    output = journal.root / ticket.job
    output.mkdir(exist_ok=True)
    mode = sys.argv[1]
    if mode == "tool":
        import imageio_ffmpeg

        tool = processes.OwnedProcess(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-nostdin",
                "-y",
                "-v",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=160x90:rate=25",
                "-t",
                "60",
                "-an",
                "-c:v",
                "libx264",
                "-tune",
                "zerolatency",
                "-flush_packets",
                "1",
                "-f",
                "mpegts",
                str(output / "live.ts"),
            ],
            isolate=False,
        )
        deadline = time.monotonic() + 15
        while not (output / "live.ts").exists() or (output / "live.ts").stat().st_size == 0:
            if tool.process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("real FFmpeg did not start")
            time.sleep(0.02)
        marker(output / "fault-marker.json", {"worker_pid": os.getpid(), "tool_pid": tool.process.pid})
        tool.wait(timeout=65)
        return
    from reelpilot.library import Library
    from reelpilot.pipeline import run_job
    from reelpilot.retrieval import Retriever
    from reelpilot.schema import JobRequest, explicit_spec

    library = Library(Path(payload["collection"]))
    body = JobRequest.model_validate(payload["request"])

    def checkpoint(name):
        if name == mode:
            marker(output / "fault-marker.json", {"worker_pid": os.getpid(), "checkpoint": name})
            while True:
                time.sleep(0.05)

    run_job(
        library,
        Retriever(library),
        journal,
        ticket.job,
        explicit_spec(body.brief),
        mode="lexical",
        request_owner=ticket.owner,
        checkpoint=checkpoint,
    )


if __name__ == "__main__":
    main()
