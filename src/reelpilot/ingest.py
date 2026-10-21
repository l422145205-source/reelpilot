"""Real video -> ASR intervals + timestamped frame windows -> portable collection."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

from .library import digest, save_library, Library
from .media import frame_at, probe
from .schema import Asset, Segment


def transcribe(path: Path, model_path: str, out: Path):
    import subprocess
    import numpy as np
    import imageio_ffmpeg
    from faster_whisper import WhisperModel
    # Decode through the same bounded FFmpeg tool used by the editor. Passing a
    # waveform also avoids PyAV-version-specific metadata_errors API differences.
    decoded = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-nostdin",
        "-i", str(path), "-f", "f32le", "-ar", "16000", "-ac", "1", "pipe:1"],
        capture_output=True, timeout=180, check=True)
    waveform = np.frombuffer(decoded.stdout, dtype=np.float32)
    model = WhisperModel(model_path, device="cpu", compute_type="int8", cpu_threads=6)
    segments, info = model.transcribe(waveform, beam_size=5, word_timestamps=True,
                                      vad_filter=False, condition_on_previous_text=False)
    rows = [{"start": s.start, "end": s.end, "text": s.text,
             "words": [{"start": w.start, "end": w.end, "text": w.word} for w in s.words or []]}
             for s in segments if s.no_speech_prob < 0.8 and s.avg_logprob > -1.2]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"language": info.language, "model": model_path, "segments": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def ingest_video(path: Path, root: Path, *, asset_id: str, title: str, attribution: str,
                 license: str, window_seconds=6.0, transcript_rows=None, copy_media=True):
    root.mkdir(parents=True, exist_ok=True)
    if not 1 <= window_seconds <= 30:
        raise ValueError("window size must be between 1 and 30 seconds")
    # IDs are validated before being used as filesystem components.
    info = probe(path)
    asset = Asset(id=asset_id, path=f"media/{asset_id}{path.suffix.lower()}", duration=info["duration"],
                  sha256=digest(path), title=title, attribution=attribution, license=license)
    dest = root / asset.path
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() or digest(dest) != asset.sha256:
        if copy_media:
            shutil.copyfile(path, dest)
        else:
            # Copying is the portable default. This parameter never makes an external path public.
            raise ValueError("external media references are not supported")
    frames = root / "frames"
    frames.mkdir(exist_ok=True)
    segments = []
    start = 0.0
    rows = transcript_rows or []
    while start < info["duration"]-0.5:
        end = min(info["duration"], start+window_seconds)
        number = len(segments)
        image = frame_at(path, (start+end)/2)
        image.thumbnail((640, 360))
        from PIL import ImageStat
        stat = ImageStat.Stat(image.convert("L"))
        usable = stat.stddev[0] >= 2 and stat.mean[0] >= 2
        relative = f"frames/{asset_id}_{number:04d}.jpg"
        image.save(root/relative, quality=85)
        words = []
        for row in rows:
            if row.get("words"):
                words.extend(w["text"] for w in row["words"]
                             if start <= (w["start"]+w["end"])/2 < end)
            elif row["start"] < end and row["end"] > start:
                words.append(row["text"])
        transcript = " ".join(words).strip()
        segments.append(Segment(id=f"{asset_id}_{number:04d}", asset_id=asset_id, start=start,
            end=end, transcript=transcript, frame=relative, visual_usable=usable,
            source="asr" if rows else "visual_only"))
        start = end
    if (root/"library.json").exists():
        previous = Library(root)
        assets = [a for a in previous.assets.values() if a.id != asset_id] + [asset]
        segments = [s for s in previous.segments.values() if s.asset_id != asset_id] + segments
    else:
        assets = [asset]
    save_library(root, assets, segments)
    # Old dense vectors must not silently survive a changed collection.
    for name in ["vectors.npz", "vectors.json", "vectors_image.npz", "vectors_image.json",
                 "vectors_text.npz", "vectors_text.json"]:
        if (root/name).exists():
            (root/name).unlink()
    return Library(root)

