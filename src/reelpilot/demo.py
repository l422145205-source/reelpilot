"""An original synthetic fixture that exercises real encoding and rendering.

The labels are authored fixture metadata, not ASR or learned visual predictions.
"""
from pathlib import Path
import tempfile

import imageio_ffmpeg

from .library import digest, save_library, Library
from .media import run_ffmpeg
from .schema import Asset, Segment


def make_demo(root: Path) -> Library:
    from PIL import Image, ImageDraw
    root.mkdir(parents=True, exist_ok=True)
    (root/"media").mkdir(exist_ok=True)
    (root/"frames").mkdir(exist_ok=True)
    scenes = [
        ("forest", "#244c35", "A quiet green forest and a winding path"),
        ("robot", "#35425a", "A blue robot stands near a city building"),
        ("dragon", "#8c432a", "A red dragon flies over a mountain"),
        ("ocean", "#184f72", "An ocean at night with a lighthouse"),
    ]
    clips = []
    for i, (name, color, text) in enumerate(scenes):
        image = Image.new("RGB", (640, 360), color)
        draw = ImageDraw.Draw(image)
        draw.text((30, 35), "REELPILOT / SYNTHETIC FIXTURE", fill="white")
        draw.text((30, 100), text, fill="white")
        draw.text((30, 300), f"scene {i+1} / {i*4:02d}-{i*4+4:02d}s", fill="white")
        frame = root/f"frames/demo_{i:04d}.jpg"
        image.save(frame)
        clip = root/f"media/part_{i}.mp4"
        run_ffmpeg(["-loop", "1", "-i", str(frame), "-f", "lavfi", "-i",
                    f"sine=frequency={220+i*110}:sample_rate=48000", "-t", "4",
                    "-vf", "fps=25", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)])
        clips.append(clip)
    listing = root/"media/concat.txt"
    listing.write_text("\n".join(f"file '{p.name}'" for p in clips), encoding="utf-8")
    movie = root/"media/demo.mp4"
    run_ffmpeg(["-f", "concat", "-safe", "1", "-i", str(listing), "-c", "copy", str(movie)])
    from .media import probe
    asset = Asset(id="demo", path="media/demo.mp4", title="Original synthetic fixture",
        duration=probe(movie)["duration"], sha256=digest(movie), license="MIT",
        attribution="ReelPilot contributors; original generated test fixture")
    segments = [Segment(id=f"demo_{i:04d}", asset_id="demo", start=i*4, end=i*4+4,
        transcript=text, frame=f"frames/demo_{i:04d}.jpg", source="synthetic_fixture")
        for i, (_, _, text) in enumerate(scenes)]
    save_library(root, [asset], segments)
    return Library(root)

