"""Persist compile/verify/render/probe stages and safely replay completed stages."""
from __future__ import annotations

import json
from pathlib import Path

from .compiler import compile_plan, verify_plan
from .journal import Journal
from .library import Library, digest
from .media import probe, render
from .schema import EditPlan, QuerySpec


def run_job(library: Library, retriever, journal: Journal, job_id: str, spec: QuerySpec,
            *, mode="hybrid", interrupt_after: str | None = None):
    request = {"collection_hash": library.hash, "spec": spec.model_dump(), "retrieval_mode": mode,
               "compiler_version": 3, "routing_policy": "goal_channel_blank_filter_v1",
               "index_fingerprints": {k: v[1].get("fingerprint") for k, v in retriever.views.items()},
               "render_contract": "h264_aac_640x360_25fps_v1"}
    journal.create(job_id, request)
    owner = journal.claim(job_id)
    stop_heartbeat = journal.start_heartbeat(job_id, owner)
    directory = journal.root/job_id
    directory.mkdir(exist_ok=True)

    def artifact(path):
        return {str(path.relative_to(journal.root)): digest(path)}

    def compile_stage():
        plan = compile_plan(library, retriever, spec, mode=mode)
        path = directory/"plan.json"
        partial = path.with_name(f"plan.{owner}.partial.json")
        partial.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        journal.publish(job_id, owner, partial, path)
        return {"plan": plan.model_dump(), "artifacts": artifact(path)}

    try:
        compiled = journal.stage(job_id, owner, "compile", request, compile_stage)
        plan = EditPlan.model_validate(compiled["plan"])
        validation = journal.stage(job_id, owner, "verify", compiled["plan"],
                                   lambda: verify_plan(library, plan))
        if interrupt_after == "verify":
            raise RuntimeError("controlled interruption after verify")

        def render_stage():
            output = directory/"output.mp4"
            attempt = directory/f"attempt-{owner}"/"output.mp4"
            metadata = render(library, plan, attempt)
            journal.publish(job_id, owner, attempt, output)
            return {"metadata": metadata, "artifacts": artifact(output)}

        rendered = journal.stage(job_id, owner, "render", compiled["plan"], render_stage)
        if interrupt_after == "render":
            raise RuntimeError("controlled interruption after render")
        media = journal.stage(job_id, owner, "probe", rendered,
                              lambda: probe(directory/"output.mp4"))
        result = {"job_id": job_id, "plan": compiled["plan"], "validation": validation,
                  "media": media, "render": rendered["metadata"], "status": "complete"}
        (directory/"result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        journal.release(job_id, owner)
        return result
    except Exception as exc:
        journal.event(job_id, "failed", f"{type(exc).__name__}: {exc}")
        try:
            journal.release(job_id, owner, error=exc)
        except Exception:
            # A replaced worker cannot change its successor's state.
            pass
        raise
    finally:
        stop_heartbeat()
