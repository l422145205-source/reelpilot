from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event
import asyncio
from typing import Literal
import math
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from .journal import Journal, BusyJob
from .admission import Admission, QueueFull
from .library import Library
from .pipeline import run_job
from .retrieval import Retriever
from .schema import Identifier, StrictModel, JobRequest
from .resources import ModelResources, VectorCache
from .control import ResourceBusy, ServiceDraining
from . import __version__


class Search(StrictModel):
    query: str = Field(min_length=1, max_length=600)
    mode: Literal["lexical", "dense", "hybrid", "adaptive"] = "lexical"
    view: Literal["image", "text", "joint"] = "image"
    limit: int = Field(default=8, ge=1, le=40)
    asset_ids: list[Identifier] = Field(default_factory=list, max_length=20)


def create_app(
    collection: Path,
    jobs: Path,
    *,
    encoder_path: str | None = None,
    planner_path: str | None = None,
    ollama_model="qwen3:4b",
    device="cuda",
    workers=2,
    max_inflight=8,
    execution_backend="thread",
    request_timeout=180,
    retain_planner=False,
    model_waiters=16,
    query_cache_entries=256,
    query_cache_bytes=16 * 1024 * 1024,
):
    if execution_backend not in ("thread", "process"):
        raise ValueError("unknown execution backend")
    if execution_backend == "process" and retain_planner:
        raise ValueError("planner retention is available only in the thread backend")
    if (
        isinstance(request_timeout, bool)
        or not math.isfinite(request_timeout)
        or not 0 < request_timeout <= 3600
    ):
        raise ValueError("request timeout must be in (0,3600]")
    if workers < 1:
        raise ValueError("workers must be positive")
    library = Library(collection)
    closing = Event()

    def runtime_check():
        if closing.is_set():
            raise ServiceDraining("service shutdown; retry original request after restart")

    journal = Journal(jobs, runtime_check=runtime_check)
    admission = Admission(journal, capacity=max_inflight)
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="reelpilot")

    def load_encoder():
        from .encoders import QwenEncoder

        return QwenEncoder(encoder_path, device=device, check=runtime_check)

    def load_planner():
        from .planner import TransformersPlanner

        return TransformersPlanner(planner_path, device=device)

    def call_planner(brief, *, check):
        from .planner import transformers_spec

        return transformers_spec(brief, planner_path, device=device, check=check)

    fingerprint = None
    if encoder_path:
        from .encoders import embedding_identity

        _, fingerprint = embedding_identity(encoder_path)
    resources = ModelResources(
        encoder_factory=load_encoder if encoder_path else None,
        fingerprint=fingerprint,
        planner_factory=load_planner if planner_path else None,
        planner_call=call_planner if planner_path else None,
        cache_planner=retain_planner,
        stop=closing,
        max_waiters=model_waiters,
        cache=VectorCache(
            max_entries=query_cache_entries, max_bytes=query_cache_bytes, max_pending=model_waiters
        ),
    )

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            resources.begin_shutdown()
            # Run queued receipts through their guard so they become interrupted,
            # rather than abandoning Futures and losing their terminal acknowledgment.
            await asyncio.to_thread(pool.shutdown, wait=True)
            await asyncio.to_thread(resources.close)

    app = FastAPI(title="ReelPilot", version=__version__, lifespan=lifespan)
    app.state.resources = resources

    def retriever(mode, *, check=None):
        runtime_check()
        if mode == "lexical":
            return Retriever(library)
        if not encoder_path:
            raise ValueError("this server has no multimodal query model configured")
        return Retriever(library, resources.proxy(check))

    @app.get("/api/health")
    def health():
        return {
            "status": "draining" if closing.is_set() else "ok",
            "collection_hash": library.hash,
            "assets": len(library.assets),
            "segments": len(library.segments),
            "dense_configured": bool(encoder_path),
            "planner_configured": bool(planner_path),
            "version": __version__,
            "max_inflight": max_inflight,
            "workers": workers,
            "execution_backend": execution_backend,
            "model_resources": resources.stats(),
        }

    @app.get("/api/library")
    def listing():
        return {
            "assets": [a.model_dump(exclude={"path"}) for a in library.assets.values()],
            "segments": [s.model_dump(exclude={"frame"}) for s in library.segments.values()],
        }

    @app.post("/api/search")
    def search(body: Search):
        try:
            return {
                "mode": body.mode,
                "hits": [
                    h.model_dump()
                    for h in retriever(body.mode).search(
                        body.query, mode=body.mode, limit=body.limit, asset_ids=body.asset_ids, view=body.view
                    )
                ],
            }
        except ResourceBusy as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "1"}) from exc
        except ServiceDraining as exc:
            raise HTTPException(503, str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    def execute(ticket, body):
        try:
            if execution_backend == "process":
                from .supervisor import run_isolated

                run_isolated(
                    ticket,
                    body.model_dump(exclude={"job_id"}, exclude_none=True),
                    library,
                    model_lock=resources.gate,
                    encoder_path=encoder_path,
                    planner_path=planner_path,
                    ollama_model=ollama_model,
                    device=device,
                )
            else:
                from .execution import run_request

                run_request(
                    ticket,
                    body,
                    library,
                    retriever_factory=retriever,
                    resources=resources,
                    model_lock=resources.gate,
                    planner_path=planner_path,
                    ollama_model=ollama_model,
                    device=device,
                    pipeline=run_job,
                )
            ticket.finish()
        except Exception as exc:
            try:
                ticket.finish(error=exc)
            except BusyJob:
                pass  # A stale request cannot change its successor's state.
        finally:
            ticket.stop()

    @app.post("/api/jobs", status_code=202)
    def create_job(body: JobRequest):
        job_id = body.job_id or uuid.uuid4().hex
        try:
            ticket = admission.reserve(
                job_id,
                body.model_dump(exclude={"job_id"}, exclude_none=True),
                timeout_seconds=body.timeout_seconds or request_timeout,
            )
        except ServiceDraining as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except BusyJob as exc:
            raise HTTPException(409, str(exc)) from exc
        except QueueFull as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "5"}) from exc
        ticket.start()
        try:
            pool.submit(execute, ticket, body)
        except RuntimeError as exc:
            ticket.finish(error=exc)
            ticket.stop()
            raise HTTPException(503, "worker pool is unavailable") from exc
        return {"job_id": job_id, "status": "queued"}

    @app.post("/api/jobs/{job_id}/cancel", status_code=202)
    def cancel(job_id: Identifier):
        try:
            return admission.cancel(job_id)
        except KeyError:
            raise HTTPException(404, "unknown job")

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        try:
            receipt = admission.get(job_id)
        except KeyError:
            receipt = None
        try:
            result = journal.get(job_id)
            if result["status"] == "complete" and (receipt is None or receipt["status"] == "complete"):
                import json

                result["result"] = json.loads(
                    (journal.root / job_id / "result.json").read_text(encoding="utf-8")
                )
            if receipt is not None:
                result["pipeline_status"] = result["status"]
                result["submission"] = receipt
                if receipt["status"] != "complete":
                    result["status"], result["error"] = receipt["status"], receipt["error"]
            return result
        except KeyError:
            if receipt:
                return {"job_id": job_id, **receipt, "events": []}
            raise HTTPException(404, "unknown job")

    @app.get("/api/jobs/{job_id}/output")
    def output(job_id: str):
        try:
            if admission.get(job_id)["status"] != "complete":
                raise HTTPException(409, "request is not complete")
        except KeyError:
            pass  # CLI-created results have a pipeline journal but no API receipt.
        try:
            if journal.get(job_id)["status"] != "complete":
                raise HTTPException(409, "output is not ready")
        except KeyError:
            raise HTTPException(404, "unknown job")
        return FileResponse(journal.root / job_id / "output.mp4", media_type="video/mp4")

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

    static = Path(__file__).parent / "static"
    app.mount("/", StaticFiles(directory=static, html=True), name="ui")
    return app
