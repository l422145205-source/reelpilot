"""Controlled resource experiment. Delays are injected, not measured LLM latency."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import platform
import statistics
from threading import Barrier, Lock
import time

import numpy as np

from reelpilot.resources import FairGate, ModelResources, VectorCache
from reelpilot.schema import Brief, explicit_spec


def burst(coalesce, requests=8):
    barrier = Barrier(requests)
    lock = Lock()
    calls = []

    class Encoder:
        fingerprint = "controlled-v1"

        def encode(self, *, text, check):
            check()
            with lock:
                calls.append(text)
            time.sleep(0.025)  # Explicit injected tool delay, not neural inference.
            return np.array([1.0, 2.0], dtype=np.float32)

        def close(self):
            pass

    encoder = Encoder()
    gate = FairGate()
    managed = ModelResources(encoder_factory=Encoder, fingerprint=Encoder.fingerprint)

    def request():
        barrier.wait(timeout=10)
        if coalesce:
            return managed.query("same exact query")
        with gate.lease():
            return encoder.encode(text="same exact query", check=lambda: None)

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=requests) as pool:
        result = list(pool.map(lambda _: request(), range(requests)))
    seconds = time.perf_counter() - start
    assert all(np.array_equal(v, [1, 2]) for v in result)
    assert len(calls) == (1 if coalesce else requests)
    stats = managed.stats()
    managed.close()
    return {
        "seconds": seconds,
        "compute_calls": len(calls),
        "requests": requests,
        "coalesced_waiters": stats["query_cache"]["coalesced"] if coalesce else 0,
    }


def planner_sequence(retain):
    loads = []
    closed = []

    class Planner:
        def __init__(self):
            time.sleep(0.02)
            loads.append(True)

        def spec(self, brief, *, check):
            check()
            time.sleep(0.005)
            return explicit_spec(brief)

        def close(self):
            closed.append(True)

    def ephemeral(brief, *, check):
        model = Planner()
        try:
            return model.spec(brief, check=check)
        finally:
            model.close()

    manager = ModelResources(planner_factory=Planner, planner_call=ephemeral, cache_planner=retain)
    start = time.perf_counter()
    plans = [manager.plan(Brief(text="forest", budget_seconds=n)) for n in [3, 4, 5, 6, 7]]
    seconds = time.perf_counter() - start
    assert [p.budget_seconds for p in plans] == [3, 4, 5, 6, 7]
    manager.close()
    assert len(loads) == len(closed) == (1 if retain else 5)
    return {
        "seconds": seconds,
        "model_constructs": len(loads),
        "model_closes": len(closed),
        "requests": 5,
        "per_request_constraints_preserved": True,
    }


def churn():
    cache = VectorCache(max_entries=32, max_bytes=8192)
    peak = 0
    for i in range(500):
        cache.get("controlled-v1", f"query-{i}", lambda: np.ones(64, dtype=np.float32))
        stats = cache.stats()
        peak = max(peak, stats["accounted_bytes"])
        assert stats["accounted_bytes"] <= 8192 and stats["entries"] <= 32
    return {"unique_queries": 500, "vector_dimensions": 64, "peak_accounted_bytes": peak, **cache.stats()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("reports/model_resource_experiment.json"))
    args = parser.parse_args()
    bursts = {
        name: [burst(flag) for _ in range(5)]
        for name, flag in [("serial_no_query_cache", False), ("bounded_singleflight", True)]
    }
    plans = {
        name: [planner_sequence(flag) for _ in range(3)]
        for name, flag in [("ephemeral", False), ("retained", True)]
    }
    report = {
        "schema_version": 1,
        "date": "2026-10-26",
        "os": platform.system(),
        "python": platform.python_version(),
        "workload": {
            "kind": "controlled CPU callbacks, no model weights",
            "encode_delay_seconds": 0.025,
            "planner_construct_delay_seconds": 0.02,
            "planner_spec_delay_seconds": 0.005,
            "same_key_burst_requests": 8,
            "burst_repetitions": 5,
            "planner_repetitions": 3,
        },
        "query_bursts": bursts,
        "planner_sequences": plans,
        "cache_churn": churn(),
        "median_burst_seconds": {k: statistics.median(x["seconds"] for x in v) for k, v in bursts.items()},
        "limitations": [
            "injected delay isolates scheduling/cache behavior",
            "not GPU inference performance",
            "not semantic quality or production throughput",
            "planner retention is opt-in and costs resident memory",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "median_burst_seconds": report["median_burst_seconds"],
                "cache_peak_bytes": report["cache_churn"]["peak_accounted_bytes"],
            }
        )
    )


if __name__ == "__main__":
    main()
