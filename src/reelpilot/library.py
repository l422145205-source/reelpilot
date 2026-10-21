"""A portable, hashed, time-aligned media index. No machine-specific paths."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .schema import Asset, Segment


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def stable_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def within(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute():
        raise ValueError("absolute paths are not accepted in a portable index")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("path escapes media collection")
    return path


class Library:
    def __init__(self, root: Path):
        self.root = root.resolve()
        raw = json.loads((self.root / "library.json").read_text(encoding="utf-8"))
        if raw.get("version") != 1:
            raise ValueError("unsupported library version")
        assets = [Asset.model_validate(a) for a in raw["assets"]]
        segments = [Segment.model_validate(s) for s in raw["segments"]]
        self.assets = {a.id: a for a in assets}
        self.segments = {s.id: s for s in segments}
        if len(self.assets) != len(assets) or len(self.segments) != len(segments):
            raise ValueError("duplicate asset or segment IDs")
        for a in assets:
            within(self.root, a.path)
        for s in segments:
            if s.asset_id not in self.assets or s.end > self.assets[s.asset_id].duration + 0.05:
                raise ValueError("segment is outside its source asset")
            if s.frame:
                within(self.root, s.frame)
        self.hash = stable_hash(raw)

    def media_path(self, asset_id: str, *, verify=False) -> Path:
        asset = self.assets[asset_id]
        path = within(self.root, asset.path)
        if not path.is_file():
            raise FileNotFoundError(f"media is missing: {asset.id}; run the documented download script")
        if verify and digest(path) != asset.sha256:
            raise ValueError(f"source media hash changed: {asset.id}")
        return path

    def frame_path(self, segment_id: str) -> Path:
        frame = self.segments[segment_id].frame
        if not frame:
            raise FileNotFoundError("segment has no frame")
        return within(self.root, frame)


def save_library(root: Path, assets: list[Asset], segments: list[Segment]):
    root.mkdir(parents=True, exist_ok=True)
    value = {"version": 1, "assets": [a.model_dump() for a in assets],
             "segments": [s.model_dump() for s in segments]}
    target = root / "library.json"
    temporary = target.with_suffix(".json.partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)

