"""Cutting clips with ffmpeg and reformatting them to 9:16."""
from __future__ import annotations

import subprocess
from pathlib import Path

VERTICAL = (
    "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:2[bg];"
    "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30[v]"
)


def cut_clip(src: str | Path, dst: str | Path, start: float, length: float, vertical: bool = True) -> Path:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{length:.2f}"]
    if vertical:
        cmd += ["-filter_complex", VERTICAL, "-map", "[v]", "-map", "0:a?"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst),
    ]
    subprocess.run(cmd, check=True)
    return dst
