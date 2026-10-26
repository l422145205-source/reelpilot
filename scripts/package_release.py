"""Explicit public-source allowlist, path redaction, and byte-level release manifest."""
from pathlib import Path
import hashlib
import json
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".toml", ".yml", ".md", ".json", ".html", ".css", ".js", ".txt"}
TOP = ["README.md", "README.en.md", "CONTRIBUTING.md", "pyproject.toml", "LICENSE", "THIRD_PARTY.md", ".gitignore", "Dockerfile", ".dockerignore"]
REPORTS = ["corpus.json", "retrieval_pilot.json", "real_planner.json", "real_runs.json", "validation.json",
           "admission_validation.json", "career_hardening_tests.txt",
           "runtime_fault_validation.json", "runtime_backend_benchmark.json", "runtime_depth_tests.txt",
           "resource_validation.json", "resource_depth_tests.txt", "model_resource_experiment.json",
           "generation_control.json"]


def build():
    stage = ROOT/"dist/public"
    stage.mkdir(parents=True, exist_ok=True)
    paths = [ROOT/p for p in TOP]
    for folder in ["src", "tests", "scripts", "docs", ".github", "examples"]:
        paths.extend(p for p in (ROOT/folder).rglob("*") if p.is_file()
                     and "__pycache__" not in p.parts and p.suffix != ".pyc"
                     and not any(part.endswith('.egg-info') for part in p.parts))
    paths.extend(ROOT/"reports"/p for p in REPORTS)
    entries = []
    for source in sorted(set(paths)):
        relative = source.relative_to(ROOT).as_posix()
        payload = source.read_bytes()
        if source.suffix in TEXT_SUFFIXES or source.name in TOP:
            value = payload.decode("utf-8-sig")
            # Experiment outputs may contain machine-specific render paths.
            for root in [str(ROOT), str(ROOT).replace("\\", "/"), str(ROOT).replace("\\", "\\\\")]:
                value = value.replace(root, "${PROJECT_ROOT}")
            if re.search(r"(?i)(?:C:[\\/]+Users[\\/]+[^\\/\s]+|(?<![\w.])1[3-9]\d{9}(?![\w.])|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20})", value):
                raise ValueError("private path/contact/credential marker in "+relative)
            payload = value.encode("utf-8")
        if len(payload) > 2_000_000:
            raise ValueError("unexpected large public artifact: "+relative)
        target = stage/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        entries.append({"path": relative, "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "git_blob_sha": hashlib.sha1(f"blob {len(payload)}\0".encode()+payload).hexdigest()})
    manifest = {"project": "ReelPilot", "files": entries,
                "excluded": ["model weights", "full movies", "local indexes/jobs", "credentials", "private resumes"]}
    (ROOT/"dist/publication_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    archive = ROOT/"dist/reelpilot-source.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for row in entries:
            z.write(stage/row["path"], "reelpilot/"+row["path"])
    print(json.dumps({"files": len(entries), "public_bytes": sum(x["bytes"] for x in entries),
                      "zip_bytes": archive.stat().st_size}))


if __name__ == "__main__":
    build()
