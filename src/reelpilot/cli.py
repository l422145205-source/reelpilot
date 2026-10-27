from pathlib import Path
import argparse
import json
import uuid


def main():
    parser = argparse.ArgumentParser(description="ReelPilot multimodal video editing agent")
    sub = parser.add_subparsers(dest="command", required=True)
    analytics = sub.add_parser("analytics", help="incremental journal facts and SQL operational metrics")
    analytics_sub = analytics.add_subparsers(dest="analytics_command", required=True)
    collect = analytics_sub.add_parser("ingest", help="read one journal without changing its data")
    collect.add_argument("--source", type=Path, required=True)
    collect.add_argument("--warehouse", type=Path, required=True)
    collect.add_argument("--source-id", required=True)
    summary = analytics_sub.add_parser("report", help="print aggregate metrics as JSON")
    summary.add_argument("--warehouse", type=Path, required=True)
    check = sub.add_parser("selfcheck", help="verify real media, recovery and repair; no models or downloads")
    check.add_argument("--workspace", type=Path, default=Path("data/selfcheck"))
    demo = sub.add_parser("demo", help="create original synthetic media, no model/download required")
    demo.add_argument("--collection", type=Path, default=Path("data/demo"))
    ingest = sub.add_parser("ingest")
    ingest.add_argument("video", type=Path)
    ingest.add_argument("--collection", type=Path, required=True)
    ingest.add_argument("--id", required=True)
    ingest.add_argument("--title", required=True)
    ingest.add_argument("--attribution", required=True)
    ingest.add_argument("--license", required=True)
    ingest.add_argument("--asr-model")
    embed = sub.add_parser("embed")
    embed.add_argument("--collection", type=Path, required=True)
    embed.add_argument("--model", required=True)
    embed.add_argument("--device", default="cuda")
    for command in ["edit", "serve"]:
        p = sub.add_parser(command)
        p.add_argument("--collection", type=Path, required=True)
        p.add_argument("--jobs", type=Path, default=Path("data/jobs"))
        p.add_argument("--embedding-model")
        p.add_argument("--planner-model")
        p.add_argument("--device", default="cuda")
        if command == "serve":
            p.add_argument("--port", type=int, default=8765)
            p.add_argument("--host", default="127.0.0.1")
            p.add_argument(
                "--execution-backend",
                choices=["thread", "process"],
                default="thread",
                help="process enables per-job termination but cold-loads models",
            )
            p.add_argument(
                "--request-timeout",
                type=float,
                default=180,
                help="attempt deadline in seconds, including queue wait (0,3600]",
            )

            p.add_argument(
                "--retain-planner", action="store_true", help="keep local planner resident; uses extra memory"
            )
            p.add_argument("--model-waiters", type=int, default=16)
            p.add_argument("--query-cache-entries", type=int, default=256)
            p.add_argument("--query-cache-mib", type=int, default=16)
        else:
            p.add_argument("brief")
            p.add_argument("--budget", type=float, default=18)
            p.add_argument("--mode", choices=["lexical", "dense", "hybrid", "adaptive"], default="lexical")
            p.add_argument("--provider", choices=["explicit", "transformers", "ollama"], default="explicit")
            p.add_argument("--job-id", default=None)
            p.add_argument("--interrupt-after", choices=["verify", "render"])
    args = parser.parse_args()
    if args.command == "analytics":
        from .analytics import ingest_journal, report_warehouse

        if args.analytics_command == "ingest":
            result = ingest_journal(args.source, args.warehouse, source_id=args.source_id)
        else:
            result = report_warehouse(args.warehouse)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "selfcheck":
        from .selfcheck import check_installation

        directory, report = check_installation(args.workspace)
        print(json.dumps({"directory": str(directory), "report": report}, ensure_ascii=False, indent=2))
    elif args.command == "demo":
        from .demo import make_demo

        library = make_demo(args.collection)
        print(
            json.dumps(
                {"collection": str(library.root), "hash": library.hash, "segments": len(library.segments)}
            )
        )
    elif args.command == "ingest":
        from .ingest import ingest_video, transcribe

        rows = None
        if args.asr_model:
            rows = transcribe(args.video, args.asr_model, args.collection / "asr" / (args.id + ".json"))
        library = ingest_video(
            args.video,
            args.collection,
            asset_id=args.id,
            title=args.title,
            attribution=args.attribution,
            license=args.license,
            transcript_rows=rows,
        )
        print("Indexed", len(library.segments), "segments", library.hash)
    elif args.command == "embed":
        from .encoders import QwenEncoder, build_vectors
        from .library import Library

        encoder = QwenEncoder(args.model, device=args.device)
        try:
            for view in ["image", "text", "joint"]:
                build_vectors(Library(args.collection), encoder, view=view)
        finally:
            encoder.close()
    elif args.command == "serve":
        import uvicorn
        from .api import create_app

        app = create_app(
            args.collection,
            args.jobs,
            encoder_path=args.embedding_model,
            planner_path=args.planner_model,
            device=args.device,
            execution_backend=args.execution_backend,
            request_timeout=args.request_timeout,
            retain_planner=args.retain_planner,
            model_waiters=args.model_waiters,
            query_cache_entries=args.query_cache_entries,
            query_cache_bytes=args.query_cache_mib * 1024 * 1024,
        )
        uvicorn.run(app, host=args.host, port=args.port)
    elif args.command == "edit":
        from .library import Library
        from .retrieval import Retriever
        from .journal import Journal
        from .pipeline import run_job
        from .schema import Brief, explicit_spec

        brief = Brief(text=args.brief, budget_seconds=args.budget)
        if args.provider == "transformers":
            if not args.planner_model:
                parser.error("--planner-model is required for transformers")
            from .planner import transformers_spec

            spec = transformers_spec(brief, args.planner_model, args.device)
        elif args.provider == "ollama":
            from .planner import ollama_spec

            spec = ollama_spec(brief, "qwen3:4b")
        else:
            spec = explicit_spec(brief)
        library = Library(args.collection)
        encoder = None
        if args.mode != "lexical":
            if not args.embedding_model:
                parser.error("--embedding-model is required for dense/hybrid")
            from .encoders import QwenEncoder

            encoder = QwenEncoder(args.embedding_model, args.device)
        try:
            result = run_job(
                library,
                Retriever(library, encoder),
                Journal(args.jobs),
                args.job_id or uuid.uuid4().hex,
                spec,
                mode=args.mode,
                interrupt_after=args.interrupt_after,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            if encoder:
                encoder.close()


if __name__ == "__main__":
    main()
