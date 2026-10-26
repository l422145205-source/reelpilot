"""Fixed, argument-list FFmpeg tools; model output can never become shell code."""

from __future__ import annotations

import json
from pathlib import Path

import av
import imageio_ffmpeg

from .compiler import verify_plan
from .library import Library, digest
from .schema import EditPlan
from .processes import OwnedProcess
from . import processes


def probe(path: Path, *, decode=True):
    with av.open(str(path)) as container:
        video = next((s for s in container.streams if s.type == "video"), None)
        if video is None:
            raise ValueError("file has no video stream")
        duration = (
            float(container.duration / av.time_base)
            if container.duration
            else (float(video.duration * video.time_base) if video.duration else 0)
        )
        if duration <= 0:
            raise ValueError("media has no finite positive duration")
        if decode:
            try:
                next(container.decode(video))
            except StopIteration as exc:
                raise ValueError("video has no decodable frames") from exc
        return {
            "duration": duration,
            "width": video.width,
            "height": video.height,
            "fps": float(video.average_rate) if video.average_rate else 0,
            "audio": any(s.type == "audio" for s in container.streams),
        }


def frame_at(path: Path, seconds: float):
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        container.seek(int(max(0, seconds) / float(stream.time_base)), stream=stream, backward=True)
        latest = None
        for frame in container.decode(stream):
            latest = frame
            if frame.time is not None and frame.time >= seconds:
                break
        if latest is None:
            raise ValueError("failed to decode requested frame")
        return latest.to_image().convert("RGB")


def run_ffmpeg(args: list[str], timeout=180, *, check=None):
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args]
    result = OwnedProcess(command, isolate=not processes.INSIDE_WORKER)
    code = result.wait(check=check, timeout=timeout)
    if code:
        raise RuntimeError("FFmpeg failed: " + result.stderr.value()[-2000:])


def render(library: Library, plan: EditPlan, output: Path, *, check=None):
    verify_plan(library, plan)
    output.parent.mkdir(parents=True, exist_ok=True)
    clip_dir = output.parent / "clips"
    clip_dir.mkdir(exist_ok=True)
    checked = set()
    clip_paths = []
    for i, clip in enumerate(plan.clips):
        if check is not None:
            check()
        source = library.media_path(clip.asset_id, verify=clip.asset_id not in checked)
        checked.add(clip.asset_id)
        info = probe(source, decode=False)
        dest = clip_dir / f"{i:02d}.mp4"
        partial = dest.with_name(dest.stem + ".partial.mp4")
        duration = clip.end - clip.start
        args = ["-ss", f"{clip.start:.6f}", "-t", f"{duration:.6f}", "-i", str(source)]
        if not info["audio"]:
            args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        args += [
            "-map",
            "0:v:0",
            "-map",
            "0:a:0" if info["audio"] else "1:a:0",
            "-vf",
            "scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-t",
            f"{duration:.6f}",
            "-movflags",
            "+faststart",
            str(partial),
        ]
        run_ffmpeg(args, check=check)
        probe(partial)
        partial.replace(dest)
        clip_paths.append(dest)
    # Filenames are generated IDs and contain no user-supplied paths or quotes.
    concat = clip_dir / "concat.txt"
    concat.write_text("\n".join(f"file '{p.name}'" for p in clip_paths), encoding="utf-8")
    temporary = output.with_name(output.stem + ".partial.mp4")
    run_ffmpeg(
        [
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            str(concat),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(temporary),
        ],
        check=check,
    )
    info = probe(temporary)
    tolerance = max(0.20, 0.05 * len(plan.clips))
    if abs(info["duration"] - plan.duration) > tolerance:
        raise ValueError(f"rendered duration {info['duration']:.3f}s differs from plan {plan.duration:.3f}s")
    if (info["width"], info["height"]) != (640, 360):
        raise ValueError("render dimensions differ from compiled output contract")
    temporary.replace(output)
    return {
        **info,
        "expected_duration": plan.duration,
        "tolerance": tolerance,
        "sha256": digest(output),
        "clip_count": len(plan.clips),
    }
