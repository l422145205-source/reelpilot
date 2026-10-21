"""Build the real two-title pilot corpus, optionally including real model embeddings."""
from pathlib import Path
import argparse
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))
if (ROOT/".runtime/site").exists():
    sys.path.insert(0, str(ROOT/".runtime/site"))
from reelpilot.ingest import ingest_video, transcribe
from reelpilot.library import digest, Library


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--embed", action="store_true")
    args = parser.parse_args()
    collection = ROOT/"data/index"
    started = time.perf_counter()
    for asset_id, filename, title, attribution in [
        ("tears", "tears_of_steel_720p.mov", "Tears of Steel",
         "(CC) Blender Foundation | mango.blender.org"),
        ("sintel", "sintel_trailer-720p.mp4", "Sintel trailer",
         "(CC) Blender Foundation | durian.blender.org"),
    ]:
        movie = ROOT/"data/raw"/filename
        asr_file = ROOT/"data/raw"/(asset_id+"_asr.json")
        if asr_file.exists():
            rows = json.loads(asr_file.read_text(encoding="utf-8"))["segments"]
        else:
            rows = transcribe(movie, str(ROOT/".runtime/models/whisper-base-en"), asr_file)
        library = ingest_video(movie, collection, asset_id=asset_id, title=title,
            attribution=attribution, license="CC-BY-3.0", transcript_rows=rows)
        print("ingested", title, len(rows), "ASR segments", flush=True)
    result = {"assets": [a.model_dump(exclude={"path"}) for a in library.assets.values()],
        "segments": len(library.segments), "collection_hash": library.hash,
        "duration_seconds": sum(a.duration for a in library.assets.values()),
        "frame_policy": "one decoded midpoint frame per six-second window",
        "asr": "faster-whisper base.en, CPU int8, word timestamp assignment",
        "ingest_seconds": time.perf_counter()-started}
    if args.embed:
        from reelpilot.encoders import QwenEncoder, build_vectors
        encoder = QwenEncoder(ROOT/".runtime/models/qwen3-vl-embedding-2b")
        begin = time.perf_counter()
        for view in ["image", "text", "joint"]:
            build_vectors(library, encoder, view=view)
        result["embedding_seconds"] = time.perf_counter()-begin
        result["encoder_fingerprint"] = encoder.fingerprint
        result["checkpoint_loading"] = encoder.loading_info
        encoder.close()
    (ROOT/"reports/corpus.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
