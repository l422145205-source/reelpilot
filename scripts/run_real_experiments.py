"""Execute the actual language-model -> retrieval -> edit -> recovery pipeline."""
from pathlib import Path
import json
import sys
import time
import argparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))
if (ROOT/".runtime/site").exists():
    sys.path.insert(0, str(ROOT/".runtime/site"))

from reelpilot.encoders import QwenEncoder
from reelpilot.journal import Journal
from reelpilot.library import Library, digest
from reelpilot.pipeline import run_job
from reelpilot.planner import transformers_spec
from reelpilot.retrieval import Retriever
from reelpilot.schema import Brief, explicit_spec


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-prefix", default="run-"+str(time.time_ns()),
                        help="Fresh IDs by default; reuse a prefix only to inspect cached replay")
    prefix = parser.parse_args().run_prefix
    brief = Brief(text="制作一段科幻预告片，先展示城市，再展示机器人，最后展示人物对话。", budget_seconds=18)
    start = time.perf_counter()
    spec = transformers_spec(brief, ROOT/".runtime/models/qwen3-0.6b")
    planning_seconds = time.perf_counter()-start
    (ROOT/"reports/real_planner.json").write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    library = Library(ROOT/"data/index")
    encoder = QwenEncoder(ROOT/".runtime/models/qwen3-vl-embedding-2b")
    retriever = Retriever(library, encoder)
    journal = Journal(ROOT/"data/jobs")
    experiments = []
    for job_id, query_spec, interrupt in [
        (prefix+"-llm-trailer", spec, False),
        (prefix+"-animation", explicit_spec(Brief(text="snowy mountains; a small flying dragon; desert sand dunes",
              budget_seconds=12, asset_ids=["sintel"])), False),
        (prefix+"-recovery", explicit_spec(Brief(text="a human brain on a dish; a futuristic city skyline",
              budget_seconds=12, asset_ids=["tears"])), True),
    ]:
        start = time.perf_counter()
        interruption_observed = False
        if interrupt:
            try:
                run_job(library, retriever, journal, job_id, query_spec, mode="adaptive", interrupt_after="render")
            except RuntimeError as exc:
                assert "controlled interruption" in str(exc)
                interruption_observed = True
        result = run_job(library, retriever, journal, job_id, query_spec, mode="adaptive")
        record = {"job_id": job_id, "elapsed_seconds_excluding_model_load": time.perf_counter()-start,
            "controlled_failure": interruption_observed, "result": result,
            "journal": journal.get(job_id)}
        experiments.append(record)
        print(job_id, "complete", result["media"]["duration"], flush=True)
    encoder.close()
    report = {"collection_hash": library.hash, "planning_model": "Qwen/Qwen3-0.6B",
        "planning_seconds_including_model_load": planning_seconds,
        "embedding_model": "Qwen/Qwen3-VL-Embedding-2B",
        "scope": "Three real local media runs; controlled exceptions exercise restart replay, not power-loss testing.",
        "experiments": experiments}
    (ROOT/"reports/real_runs.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
