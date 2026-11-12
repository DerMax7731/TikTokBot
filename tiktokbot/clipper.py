"""Rendering clips with ffmpeg: 9:16 layout, burned-in subtitles, loudness, fades."""
from __future__ import annotations

import subprocess
from pathlib import Path

LAYOUTS = {
    # Whole frame visible on a blurred, zoomed copy of itself.
    "blur": (
        "[0:v]split[a][b];"
        "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:2[bg];"
        "[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2"
    ),
    # Fill the screen, cropping the sides (good for centred gameplay or talking heads).
    "crop": "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
    # Letterbox on black.
    "fit": "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
}


def render_clip(src: str | Path, dst: str | Path, start: float, length: float, layout: str = "blur",
                ass: str | Path | None = None, normalize_audio: bool = True) -> Path:
    if layout not in LAYOUTS:
        raise ValueError(f"unknown layout {layout!r}, expected one of {sorted(LAYOUTS)}")
    dst = Path(dst).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    fade_out = max(length - 0.35, 0)
    graph = LAYOUTS[layout] + ",setsar=1,fps=30"
    if ass:
        # Referenced by bare file name (ffmpeg runs in its directory) to avoid filter escaping issues.
        graph += f",ass={Path(ass).name}"
    graph += f",fade=t=in:st=0:d=0.2,fade=t=out:st={fade_out:.2f}:d=0.35,format=yuv420p[v]"

    audio = f"afade=t=in:st=0:d=0.15,afade=t=out:st={fade_out:.2f}:d=0.35"
    if normalize_audio:
        audio = "loudnorm=I=-14:TP=-1.5:LRA=11," + audio
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", str(Path(src).resolve()),
        "-t", f"{length:.2f}", "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
        "-af", audio, "-ar", "44100",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-maxrate", "8M", "-bufsize", "16M",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst),
    ]
    subprocess.run(cmd, check=True, cwd=str(Path(ass).resolve().parent) if ass else None)
    return dst
