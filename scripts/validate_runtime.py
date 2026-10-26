"""Run the real fault suite and publish a path-free evidence summary."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def summarize(
    scratch,
    xml,
    log,
    out,
    *,
    summary_name="runtime_depth_tests.txt",
    reproduce="python scripts/validate_runtime.py",
):
    suite = ET.parse(xml).getroot()
    cases = list(suite.iter("testcase"))
    failures = sum(any(c.find(tag) is not None for tag in ["failure", "error", "skipped"]) for c in cases)
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in scratch.rglob("runtime-evidence.json")]
    required = {
        "real_process_success",
        "render_committed",
        "result_replaced_before_commit",
        "cancel_running_ffmpeg",
        "timeout_running_ffmpeg",
        "supervisor_hard_kill",
    }
    if failures or required != {r["case"] for r in rows}:
        raise RuntimeError("suite did not produce the complete passing evidence set")
    report = {
        "schema_version": 1,
        "date": "2026-10-26",
        "environment": {"os": platform.system(), "python": platform.python_version()},
        "tests": {
            "passed": len(cases),
            "failed_or_skipped": failures,
            "junit_sha256": hashlib.sha256(xml.read_bytes()).hexdigest(),
        },
        "cases": sorted(rows, key=lambda r: r["case"]),
        "pipe_capture": {
            "bytes_written_per_stream": 5 * 1024 * 1024,
            "bytes_retained_per_stream": 65536,
            "verified_by": "test_pipe_flood_is_drained_with_bounded_tail",
        },
        "coverage_notes": [
            "real media, SQLite and HTTP handlers; no models loaded",
            "crash recovery waits for actual request lease expiry; no manual lease SQL edit",
            "deadline during FFmpeg is set to now+0.25s after startup, then expires by elapsed time",
            "queued deadline expires by elapsed time without SQL edits",
            "slow FFmpeg entry point is a trusted test worker; production supervisor is unchanged",
            "unit tests also cover synthetic lease states and blocked model doubles",
            "process-crash tests do not establish disk-power-loss atomicity or production scale",
        ],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary = re.search(r"\d+ passed[^\r\n]*", log)
    out.with_name(summary_name).write_text(
        f"OS: {platform.system()}\nPython: {platform.python_version()}\n"
        + (summary.group(0) if summary else f"{len(cases)} passed")
        + f"\nJUnit SHA256: {report['tests']['junit_sha256']}\n"
        + f"Full raw JUnit/log and test media stay in local scratch. Reproduce: {reproduce}\n",
        encoding="utf-8",
    )
    return report


def run_suite(root):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    scratch = root / ("cases-" + token)
    xml = root / (token + ".xml")
    # A fresh UUID is mandatory: pytest recursively clears an existing --basetemp.
    if scratch.exists() or xml.exists():
        raise FileExistsError("scratch collision")
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            str(ROOT / "tests"),
            "-o",
            "addopts=",
            "-q",
            "-p",
            "no:cacheprovider",
            "--tb=short",
            "--basetemp",
            str(scratch),
            "--junitxml",
            str(xml),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    raw = result.stdout + result.stderr
    xml.with_suffix(".log").write_text(raw, encoding="utf-8")
    if result.returncode:
        print(raw[-6000:])
        raise SystemExit(result.returncode)
    return scratch, xml, raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, default=ROOT / "data/runtime-validation")
    parser.add_argument("--out", type=Path, default=ROOT / "reports/runtime_fault_validation.json")
    args = parser.parse_args()
    scratch, xml, raw = run_suite(args.scratch)
    report = summarize(scratch, xml, raw, args.out)
    print(json.dumps({"passed": report["tests"]["passed"], "real_fault_cases": len(report["cases"])}))


if __name__ == "__main__":
    main()
