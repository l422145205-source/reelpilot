"""Model-free installation check with real media, replay and corruption repair."""

from pathlib import Path
import json
import uuid

from .demo import make_demo
from .journal import Journal
from .library import digest
from .pipeline import run_job
from .retrieval import Retriever
from .schema import Brief, explicit_spec


def require(condition, message):
    if not condition:
        raise RuntimeError("selfcheck failed: " + message)


def check_installation(workspace: Path):
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    run = workspace / ("selfcheck-" + uuid.uuid4().hex)
    run.mkdir(exist_ok=False)
    library = make_demo(run / "collection")
    journal = Journal(run / "jobs")
    retriever = Retriever(library)
    spec = explicit_spec(Brief(text="forest;robot", budget_seconds=8))

    def execute(**kwargs):
        return run_job(library, retriever, journal, "check", spec, mode="lexical", **kwargs)

    def attempts():
        return next(s["attempts"] for s in journal.get("check")["stages"] if s["stage"] == "render")

    try:
        execute(interrupt_after="render")
    except RuntimeError as exc:
        if str(exc) != "controlled interruption after render":
            raise
    else:
        raise RuntimeError("selfcheck failed: expected controlled interruption")
    require(journal.get("check")["status"] == "failed", "interruption was not recorded")
    require(attempts() == 1, "render did not commit before interruption")
    result = execute()
    require(result["status"] == "complete" and attempts() == 1, "resume did not reuse verified render")
    execute()
    require(attempts() == 1, "identical replay rendered again")
    output = run / "jobs/check/output.mp4"
    # This file was generated inside this fresh run. No existing workspace file
    # is selected for corruption, cleanup, replacement or recursive deletion.
    require(output.resolve().is_relative_to(run), "scratch output escaped run directory")
    output.write_bytes(b"intentionally corrupt selfcheck output")
    repaired = execute()
    require(attempts() == 2, "corrupt output did not trigger render repair")
    media = repaired["media"]
    require(media["width"] == 640 and media["height"] == 360, "unexpected output size")
    require(abs(media["duration"] - 8) < 0.25, "unexpected output duration")
    report = {
        "schema_version": 1,
        "kind": "real-media-cpu-selfcheck",
        "models_loaded": False,
        "retrieval_mode": "lexical",
        "planner": "explicit",
        "interruption": "controlled Python exception, not OS kill",
        "checks": {
            "media_generation": True,
            "interruption_receipt": True,
            "verified_render_resume": True,
            "identical_replay": True,
            "corruption_repair": True,
            "decoded_output": True,
        },
        "render_commits_before_corruption": 1,
        "render_commits_after_repair": attempts(),
        "output": {
            "path": "jobs/check/output.mp4",
            "duration": media["duration"],
            "width": media["width"],
            "height": media["height"],
            "sha256": digest(output),
        },
        "limits": [
            "installation and execution contracts only",
            "does not test model semantics",
            "real process-death experiments are in scripts/validate_resources.py",
        ],
    }
    (run / "selfcheck.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return run, report
