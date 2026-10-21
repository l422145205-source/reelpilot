"""Reproducible, explicitly weak-label retrieval ablations and real media replay."""
from pathlib import Path
import json
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))
if (ROOT/".runtime/site").exists():
    sys.path.insert(0, str(ROOT/".runtime/site"))
import numpy as np
from reelpilot.encoders import QwenEncoder, build_vectors
from reelpilot.library import Library, digest
from reelpilot.retrieval import Retriever


def main():
    data = json.loads((ROOT/"examples/pilot_queries.json").read_text(encoding="utf-8"))
    library = Library(ROOT/"data/index")
    encoder = QwenEncoder(ROOT/".runtime/models/qwen3-vl-embedding-2b")
    # No query or relevance labels are passed into index construction.
    for view in ["image", "text"]:
        if not (library.root/("vectors_"+view+".npz")).exists():
            build_vectors(library, encoder, view=view)
    queries = [(intent, lang, intent[lang]) for intent in data["intents"] for lang in ["en", "zh"]]
    encode_ms = []
    for _, _, query in queries:
        start = time.perf_counter()
        encoder(query)
        encode_ms.append((time.perf_counter()-start)*1000)
    details = []
    metrics = {}
    for name, mode, view in [("bm25", "lexical", "joint"), ("dense_image", "dense", "image"),
                             ("dense_text", "dense", "text"), ("dense_joint", "dense", "joint"),
                             ("rrf_joint", "hybrid", "joint")]:
        retriever = Retriever(library, encoder, view=view)
        scores, recalled, times = [], [], []
        for intent, lang, query in queries:
            start = time.perf_counter()
            hits = retriever.search(query, mode=mode, limit=10)
            times.append((time.perf_counter()-start)*1000)
            ranked = [h.segment.id for h in hits]
            relevant = set(intent["relevant"])
            rank = next((i for i, key in enumerate(ranked, 1) if key in relevant), None)
            scores.append(1/rank if rank else 0)
            recalled.append(len(set(ranked[:5]) & relevant)/len(relevant))
            details.append({"system": name, "intent": intent["id"], "language": lang,
                "query": query, "ranked": ranked, "first_relevant_rank": rank})
        metrics[name] = {"MRR@10": float(np.mean(scores)), "set_recall@5": float(np.mean(recalled)),
                         "ranking_ms_p50_cached_query": float(np.median(times))}
    import torch, transformers
    report = {"scope": data["metric_scope"], "label_provenance": data["label_provenance"],
        "query_file_sha256": digest(ROOT/"examples/pilot_queries.json"),
        "collection_hash": library.hash, "scene_intents": len(data["intents"]),
        "bilingual_query_forms": len(queries), "segments": len(library.segments), "assets": len(library.assets),
        "encoder_fingerprint": encoder.fingerprint, "model_revision": encoder.manifest["revision"],
        "query_encode_ms_p50": float(np.median(encode_ms)), "query_encode_ms_p95": float(np.percentile(encode_ms, 95)),
        "metrics": metrics, "details": details,
        "environment": {"python": platform.python_version(), "torch": torch.__version__,
                        "transformers": transformers.__version__, "gpu": torch.cuda.get_device_name(0)},
        "interpretation": "Development pilot only. Paired languages are correlated. No training, independent human gold, statistical significance claim, production metric or full-video grounding result."}
    encoder.close()
    (ROOT/"reports/retrieval_pilot.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ["scene_intents", "bilingual_query_forms", "query_encode_ms_p50", "metrics"]}, indent=2))


if __name__ == "__main__":
    main()
