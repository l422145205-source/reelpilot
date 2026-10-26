"""CPU contracts plus real service-drain/restart evidence; generation probe is separate."""

import argparse
import json
from pathlib import Path

from validate_runtime import ROOT, run_suite, summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, default=ROOT / "data/resource-validation")
    parser.add_argument("--out", type=Path, default=ROOT / "reports/resource_validation.json")
    args = parser.parse_args()
    scratch, xml, raw = run_suite(args.scratch)
    report = summarize(
        scratch,
        xml,
        raw,
        args.out,
        summary_name="resource_depth_tests.txt",
        reproduce="python scripts/validate_resources.py",
    )
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in scratch.rglob("resource-evidence.json")]
    if {r["case"] for r in rows} != {"real_ffmpeg_drain_restart"}:
        raise RuntimeError("missing real shutdown/restart experiment")
    report["version"] = "0.4.0"
    report["resource_cases"] = rows
    report["scope"] = [
        "bounded namespace-aware LRU and single-flight cancellation ownership",
        "FIFO model admission with cancellation and bounded waiters",
        "lexical search bypasses GPU gate",
        "retained planner weights, no cached plan answers",
        "real FFmpeg shutdown acknowledgment and original-request recovery",
        "cooperative model doubles; tiny real Transformers generation validated separately",
    ]
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": report["tests"]["passed"],
                "real_process_experiments": len(report["cases"]) + len(rows),
            }
        )
    )


if __name__ == "__main__":
    main()
