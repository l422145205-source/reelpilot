"""Reproduce small CPU-only cold-worker overhead; not a model or production benchmark."""

import argparse
import json
from pathlib import Path
import platform
import statistics
import time
import uuid

from fastapi.testclient import TestClient
from reelpilot.api import create_app
from reelpilot.demo import make_demo


def run(client, job):
    start = time.perf_counter()
    response = client.post(
        "/api/jobs", json={"job_id": job, "brief": {"text": "forest;robot", "budget_seconds": 8}}
    )
    assert response.status_code == 202, response.text
    while time.perf_counter() - start < 30:
        state = client.get("/api/jobs/" + job).json()
        if state["status"] in ("failed", "cancelled", "timed_out", "interrupted"):
            raise RuntimeError(state.get("error"))
        if state["status"] == "complete":
            break
        time.sleep(0.01)
    else:
        raise TimeoutError("benchmark job did not settle")
    complete = time.perf_counter() - start
    if client.get("/api/health").json()["execution_backend"] == "process":
        while not any(e["event"] == "worker_reaped" for e in state["events"]):
            if time.perf_counter() - start > 35:
                raise TimeoutError("worker not reaped")
            time.sleep(0.01)
            state = client.get("/api/jobs/" + job).json()
    return {
        "complete_seconds": complete,
        "worker_reaped_seconds": time.perf_counter() - start,
        "video_seconds": state["result"]["media"]["duration"],
        "render_commits": next(s["attempts"] for s in state["stages"] if s["stage"] == "render"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, default=Path("data/runtime-benchmark"))
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--out", type=Path, default=Path("reports/runtime_backend_benchmark.json"))
    args = parser.parse_args()
    if not 3 <= args.samples <= 20:
        parser.error("samples must be in [3,20]")
    library = make_demo(args.scratch / "collection")
    token = uuid.uuid4().hex[:8]
    rows = {}
    # Alternating order across samples limits a simple warm-machine/order confound.
    with TestClient(create_app(library.root, args.scratch / "thread-jobs", workers=1)) as thread:
        with TestClient(
            create_app(library.root, args.scratch / "process-jobs", workers=1, execution_backend="process")
        ) as process:
            clients = {"thread": thread, "process": process}
            warmups = {name: run(c, token + "-warmup-" + name) for name, c in clients.items()}
            for i in range(args.samples):
                for name in (["thread", "process"] if i % 2 == 0 else ["process", "thread"]):
                    rows.setdefault(name, []).append(run(clients[name], token + "-" + name + "-" + str(i)))
    medians = {
        name: statistics.median(r["complete_seconds"] for r in values) for name, values in rows.items()
    }
    report = {
        "schema_version": 1,
        "date": "2026-10-26",
        "environment": {
            "os": platform.system(),
            "python": platform.python_version(),
            "machine": platform.machine(),
        },
        "workload": {
            "source": "original synthetic video",
            "output_budget_seconds": 8,
            "goals": "forest;robot",
            "mode": "lexical",
            "provider": "explicit",
            "models_loaded": False,
            "workers_per_app": 1,
            "samples_per_backend": args.samples,
            "excluded_warmups_per_backend": 1,
            "measurement": "HTTP POST until committed complete; separate worker-reap observation",
        },
        "warmups": warmups,
        "samples": rows,
        "median_complete_seconds": medians,
        "process_minus_thread_median_seconds": medians["process"] - medians["thread"],
        "limitations": [
            "single local CPU workload",
            "no GPU model loading or inference",
            "not production throughput or SLA",
            "poll interval and filesystem cache affect timing",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "median_seconds": medians,
                "extra_process_seconds": report["process_minus_thread_median_seconds"],
            }
        )
    )


if __name__ == "__main__":
    main()
