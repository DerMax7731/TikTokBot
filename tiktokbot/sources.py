"""Fetching source media with yt-dlp (YouTube, Twitch/Kick/YouTube live, TikTok, ...)."""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

log = logging.getLogger(__name__)


@dataclass
class Media:
    source_id: str
    path: Path
    title: str
    uploader: str
    url: str


def _ydl(opts: dict | None = None) -> yt_dlp.YoutubeDL:
    base = {"quiet": True, "no_warnings": True, "noprogress": True}
    base.update(opts or {})
    return yt_dlp.YoutubeDL(base)


def _source_id(info: dict) -> str:
    return f"{info.get('extractor_key', 'src')}:{info.get('id')}"


def list_candidates(src: dict) -> list[dict]:
    """Resolve a configured source into concrete entries (videos or a live stream)."""
    url = src["url"]
    with _ydl({"extract_flat": "in_playlist", "playlistend": int(src.get("latest", 2)) * 3}) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        return []
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        out = []
        for e in entries:
            out.append(
                {
                    "id": f"{e.get('ie_key') or info.get('extractor_key', 'src')}:{e.get('id')}",
                    "url": e.get("url") or e.get("webpage_url"),
                    "live": False,
                }
            )
        return out
    if info.get("is_live"):
        # A stream can be recorded again later: one recording per hour at most.
        return [{"id": f"{_source_id(info)}:{int(time.time() // 3600)}", "url": url, "live": True}]
    return [{"id": _source_id(info), "url": url, "live": False}]


def fetch(entry: dict, src: dict, workdir: Path) -> Media:
    workdir.mkdir(parents=True, exist_ok=True)
    if entry["live"]:
        return _record_live(entry, src, workdir)
    opts = {
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(workdir / "%(extractor_key)s_%(id)s.%(ext)s"),
        "noplaylist": True,
    }
    with _ydl(opts) as ydl:
        info = ydl.extract_info(entry["url"], download=True)
        path = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
    if not path.exists():
        raise RuntimeError(f"download produced no file for {entry['url']}")
    return Media(entry["id"], path, info.get("title") or "", info.get("uploader") or "", entry["url"])


def _record_live(entry: dict, src: dict, workdir: Path) -> Media:
    seconds = int(src.get("record_seconds", 1200))
    with _ydl({"format": "b[height<=1080]/b"}) as ydl:
        info = ydl.extract_info(entry["url"], download=False)
    path = workdir / f"live_{info.get('id')}_{int(time.time())}.mp4"
    log.info("recording %ss of live stream %s", seconds, entry["url"])
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for key, value in (info.get("http_headers") or {}).items():
        cmd += ["-headers", f"{key}: {value}\r\n"]
    cmd += ["-i", info["url"], "-t", str(seconds), "-c", "copy", "-movflags", "+faststart", str(path)]
    subprocess.run(cmd, check=True, timeout=seconds + 300)
    return Media(entry["id"], path, info.get("title") or "", info.get("uploader") or "", entry["url"])
