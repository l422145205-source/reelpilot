from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from threading import RLock
from typing import Literal
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from .journal import Journal, BusyJob
from .library import Library
from .pipeline import run_job
from .retrieval import Retriever
from .schema import Brief, Identifier, StrictModel, QuerySpec, explicit_spec


class Search(StrictModel):
    query: str = Field(min_length=1, max_length=600)
    mode: Literal["lexical", "dense", "hybrid", "adaptive"] = "lexical"
    view: Literal["image", "text", "joint"] = "image"
    limit: int = Field(default=8, ge=1, le=40)
    asset_ids: list[Identifier] = Field(default_factory=list, max_length=20)


class JobRequest(StrictModel):
    brief: Brief
    mode: Literal["lexical", "dense", "hybrid", "adaptive"] = "lexical"
    provider: Literal["explicit", "transformers", "ollama"] = "explicit"
    job_id: Identifier | None = None


def create_app(collection: Path, jobs: Path, *, encoder_path: str | None = None,
               planner_path: str | None = None, ollama_model="qwen3:4b", device="cuda"):
    library = Library(collection)
    journal = Journal(jobs)
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="reelpilot")
    encoder_lock = RLock()
    encoder = None
    active = {}
    active_lock = RLock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        pool.shutdown(wait=True)
        if encoder is not None:
            encoder.close()

    app = FastAPI(title="ReelPilot", version="0.1.0", lifespan=lifespan)

    def retriever(mode):
        nonlocal encoder
        if mode != "lexical":
            if not encoder_path:
                raise ValueError("this server has no multimodal query model configured")
            with encoder_lock:
                if encoder is None:
                    from .encoders import QwenEncoder
                    encoder = QwenEncoder(encoder_path, device=device)
        return Retriever(library, encoder)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "collection_hash": library.hash, "assets": len(library.assets),
                "segments": len(library.segments), "dense_configured": bool(encoder_path),
                "planner_configured": bool(planner_path), "version": "0.1.0"}

    @app.get("/api/library")
    def listing():
        return {"assets": [a.model_dump(exclude={"path"}) for a in library.assets.values()],
                "segments": [s.model_dump(exclude={"frame"}) for s in library.segments.values()]}

    @app.post("/api/search")
    def search(body: Search):
        try:
            return {"mode": body.mode, "hits": [h.model_dump() for h in retriever(body.mode).search(
                body.query, mode=body.mode, limit=body.limit, asset_ids=body.asset_ids, view=body.view)]}
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    def execute(job_id, body):
        try:
            try:
                resolved = journal.get(job_id)["spec"]["spec"]
            except KeyError:
                resolved = None
            if resolved is not None:
                spec = QuerySpec.model_validate(resolved)
            elif body.provider == "transformers":
                if not planner_path:
                    raise ValueError("no local planning model configured")
                from .planner import transformers_spec
                spec = transformers_spec(body.brief, planner_path, device=device)
            elif body.provider == "ollama":
                from .planner import ollama_spec
                spec = ollama_spec(body.brief, ollama_model)
            else:
                spec = explicit_spec(body.brief)
            run_job(library, retriever(body.mode), journal, job_id, spec, mode=body.mode)
        except Exception as exc:
            # The job may fail before its pipeline journal is created (e.g. LLM JSON).
            with active_lock:
                active[job_id] = {"status": "failed", "error": str(exc)[:1500]}
            return
        with active_lock:
            active[job_id] = {"status": "complete"}

    @app.post("/api/jobs", status_code=202)
    def create_job(body: JobRequest):
        job_id = body.job_id or uuid.uuid4().hex
        try:
            journal.reserve_request(job_id, body.model_dump(exclude={"job_id"}))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        with active_lock:
            if active.get(job_id, {}).get("status") == "running" or journal.is_live(job_id):
                raise HTTPException(409, "job already has an active worker")
            active[job_id] = {"status": "running"}
        pool.submit(execute, job_id, body)
        return {"job_id": job_id, "status": "queued"}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        try:
            result = journal.get(job_id)
            if result["status"] == "complete":
                import json
                result["result"] = json.loads((journal.root/job_id/"result.json").read_text(encoding="utf-8"))
            return result
        except KeyError:
            with active_lock:
                state = active.get(job_id)
            if state:
                return {"job_id": job_id, **state, "events": []}
            raise HTTPException(404, "unknown job")

    @app.get("/api/jobs/{job_id}/output")
    def output(job_id: str):
        try:
            if journal.get(job_id)["status"] != "complete":
                raise HTTPException(409, "output is not ready")
        except KeyError:
            raise HTTPException(404, "unknown job")
        return FileResponse(journal.root/job_id/"output.mp4", media_type="video/mp4")

    @app.get("/api/assets/{asset_id}")
    def asset(asset_id: str):
        if asset_id not in library.assets:
            raise HTTPException(404, "unknown asset")
        return FileResponse(library.media_path(asset_id), media_type="video/mp4")

    @app.get("/api/frames/{segment_id}")
    def frame(segment_id: str):
        if segment_id not in library.segments:
            raise HTTPException(404, "unknown segment")
        return FileResponse(library.frame_path(segment_id), media_type="image/jpeg")

    static = Path(__file__).parent/"static"
    app.mount("/", StaticFiles(directory=static, html=True), name="ui")
    return app

