"""Project-local public downloads. Large files and weights are never committed."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
HOST = os.environ.get("REELPILOT_MODEL_ENDPOINT", "https://huggingface.co").rstrip("/")
PINNED = {
    "Qwen/Qwen3-VL-Embedding-2B": "9f2f7e710d6d81056aa5c0a4f04764fec6bb7bda",
    "Qwen/Qwen3-0.6B": "c1899de289a04d12100db370d81485cdf75e47ca",
    "Systran/faster-whisper-base.en": "3d3d5dee26484f91867d81cb899cfcf72b96be6c",
}


def session():
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504])))
    return s


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def download(url, target, expected_size=None, expected_sha=None):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and (expected_size is None or target.stat().st_size == expected_size):
        if expected_sha is None or sha(target) == expected_sha:
            return {"file": str(target.relative_to(ROOT)), "bytes": target.stat().st_size, "sha256": sha(target)}
    part = target.with_name(target.name+".partial")
    for attempt in range(5):
        try:
            offset = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            with session().get(url, stream=True, headers=headers, timeout=(20, 60)) as response:
                response.raise_for_status()
                append = response.status_code == 206 and response.headers.get("Content-Range", "").startswith(f"bytes {offset}-")
                with part.open("ab" if append else "wb") as f:
                    for chunk in response.iter_content(1024*1024):
                        if chunk:
                            f.write(chunk)
            if expected_size is not None and part.stat().st_size != expected_size:
                raise ValueError(f"incomplete file {target.name}: {part.stat().st_size}/{expected_size}")
            if expected_sha is not None and sha(part) != expected_sha:
                part.unlink()
                raise ValueError(f"hash mismatch: {target.name}")
            part.replace(target)
            print("verified", target.name, target.stat().st_size, flush=True)
            return {"file": str(target.relative_to(ROOT)), "bytes": target.stat().st_size, "sha256": sha(target)}
        except Exception as exc:
            print("retry", target.name, attempt+1, type(exc).__name__, flush=True)
            if attempt == 4:
                raise
            time.sleep(1)


def model(repo, name):
    revision = PINNED[repo]
    tree = session().get(f"{HOST}/api/models/{repo}/tree/{revision}",
                         params={"recursive": "true"}, timeout=40)
    tree.raise_for_status()
    folder = ROOT / ".runtime/models" / name
    folder.mkdir(parents=True, exist_ok=True)
    selected = [r for r in tree.json() if r.get("type") == "file"
                and r["path"].endswith((".json", ".safetensors", ".txt", ".model", ".jinja", ".bin"))
                and "/" not in r["path"] and not r["path"].startswith("optimizer")]
    print("model", repo, revision, sum(r["size"] for r in selected), flush=True)
    files = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = [pool.submit(download, f"{HOST}/{repo}/resolve/{revision}/{r['path']}",
                             folder/r["path"], r["size"], r.get("lfs", {}).get("oid")) for r in selected]
        for task in as_completed(tasks):
            files.append(task.result())
    manifest = {"repo": repo, "revision": revision, "mirror": HOST,
                "canonical_url": f"https://huggingface.co/{repo}/tree/{revision}", "files": files}
    (folder/"download_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def movie():
    root = ROOT / "data/raw"
    records = [download("https://download.blender.org/demo/movies/ToS/tears_of_steel_720p.mov",
                        root/"tears_of_steel_720p.mov", 372178639,
                        "efa9062d9cdb7a338e40ad530dfdf234806743f29ae6a1a136b97ece4e588e8f")]
    records.append(download("https://download.blender.org/demo/movies/ToS/copyright.txt", root/"copyright.txt"))
    records.append(download("https://download.blender.org/durian/trailer/sintel_trailer-720p.mp4",
                            root/"sintel_trailer-720p.mp4", 7608204,
                            "cb0fe73fc0a7d543459996c0cdab730997e6eac1013d3ede18796f777cb7f273"))
    (root/"download_manifest.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    return records


def reference():
    response = session().get("https://api.github.com/repos/QwenLM/Qwen3-VL-Embedding/contents/src/models/qwen3_vl_embedding.py", timeout=30)
    response.raise_for_status()
    data = response.json()
    content = base64.b64decode(data["content"])
    target = ROOT/".runtime/qwen_reference.py"
    target.write_bytes(content)
    print("reference downloaded for inspection", len(content), data["sha"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("item", choices=["movie", "embedding", "planner", "asr", "reference"])
    item = parser.parse_args().item
    if item == "movie":
        movie()
    elif item == "reference":
        reference()
    else:
        model(*{"embedding": ("Qwen/Qwen3-VL-Embedding-2B", "qwen3-vl-embedding-2b"),
                "planner": ("Qwen/Qwen3-0.6B", "qwen3-0.6b"),
                "asr": ("Systran/faster-whisper-base.en", "whisper-base-en")}[item])
