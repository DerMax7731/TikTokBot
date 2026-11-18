"""Fetching source media with yt-dlp (YouTube, Twitch/Kick/YouTube live, TikTok, ...).

Short videos are downloaded whole. For long videos only the audio is downloaded
for analysis, and afterwards just the chosen stretches of video are fetched.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func

from .transcript import Word, from_youtube_json3

log = logging.getLogger(__name__)
PAD = 2.0  # extra seconds fetched around a section so cuts land on clean frames


def _ydl(opts: dict | None = None) -> yt_dlp.YoutubeDL:
    base = {"quiet": True, "no_warnings": True, "noprogress": True}
    base.update(opts or {})
    return yt_dlp.YoutubeDL(base)


def _source_id(info: dict) -> str:
    return f"{info.get('extractor_key', 'src')}:{info.get('id')}"


@dataclass
class Fetched:
    source_id: str
    url: str
    title: str
    uploader: str
    duration: float
    analysis_path: Path              # file with the audio track of the whole source
    workdir: Path
    full_video: bool                 # analysis_path is the complete video
    heatmap: list | None = None
    words: list[Word] | None = None  # transcript of the whole source, if the site provides one
    _files: list[Path] = field(default_factory=list)

    def segment(self, start: float, length: float) -> tuple[Path, float]:
        """A video file containing [start, start+length) and the offset of `start` inside it."""
        if self.full_video:
            return self.analysis_path, start
        a = max(start - PAD, 0.0)
        b = min(start + length + PAD, self.duration)
        stem = f"seg_{abs(hash(self.source_id)) % 10**8}_{int(a)}"
        opts = {
            "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
            "merge_output_format": "mp4",
            "outtmpl": str(self.workdir / f"{stem}.%(ext)s"),
            "noplaylist": True,
            "download_ranges": download_range_func(None, [(a, b)]),
            "force_keyframes_at_cuts": True,
        }
        try:
            with _ydl(opts) as ydl:
                ydl.extract_info(self.url, download=True)
            path = self.workdir / f"{stem}.mp4"
            if not path.exists():
                raise RuntimeError("section download produced no file")
            self._files.append(path)
            return path, start - a
        except Exception as exc:
            log.warning("section download failed (%s) - downloading the full video instead", exc)
            self.analysis_path = _download_full(self.url, self.workdir)
            self._files.append(self.analysis_path)
            self.full_video = True
            return self.analysis_path, start

    def cleanup(self) -> None:
        for path in [self.analysis_path, *self._files]:
            path.unlink(missing_ok=True)


def matches(info: dict, keywords: list[str]) -> bool:
    if not keywords:
        return True
    text = " ".join(str(info.get(k) or "") for k in ("title", "fulltitle", "description")).lower()
    return any(k in text for k in keywords)


def list_candidates(src: dict) -> list[dict]:
    """Resolve a configured source into concrete entries (videos or a live stream)."""
    url = src["url"]
    keywords = [k.lower() for k in (src.get("match") or [])]
    scan = int(src.get("scan", 30)) if keywords else int(src.get("latest", 2)) * 3
    with _ydl({"extract_flat": "in_playlist", "playlistend": scan}) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        return []
    if info.get("_type") == "playlist":
        return [
            {
                "id": f"{e.get('ie_key') or info.get('extractor_key', 'src')}:{e.get('id')}",
                "url": e.get("url") or e.get("webpage_url"),
                "live": False,
            }
            for e in (info.get("entries") or []) if e and matches(e, keywords)
        ]
    if not matches(info, keywords):
        return []
    if info.get("is_live"):
        # A stream can be recorded again later: one recording per hour at most.
        return [{"id": f"{_source_id(info)}:{int(time.time() // 3600)}", "url": url, "live": True}]
    return [{"id": _source_id(info), "url": url, "live": False}]


def _download_full(url: str, workdir: Path) -> Path:
    opts = {
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(workdir / "%(extractor_key)s_%(id)s.%(ext)s"),
        "noplaylist": True,
    }
    with _ydl(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
    if not path.exists():
        raise RuntimeError(f"download produced no file for {url}")
    return path


def _download_audio(url: str, workdir: Path) -> Path:
    opts = {"format": "wa/ba/w", "outtmpl": str(workdir / "%(extractor_key)s_%(id)s_audio.%(ext)s"), "noplaylist": True}
    with _ydl(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info))


def _site_transcript(ydl: yt_dlp.YoutubeDL, info: dict) -> list[Word] | None:
    """Word-timed captions from the site (YouTube json3), manual ones preferred."""
    lang = (info.get("language") or "en").split("-")[0]
    for pool in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
        keys = sorted((k for k in pool if k.split("-")[0] == lang), key=lambda k: ("orig" not in k, len(k)))
        for key in keys:
            for fmt in pool.get(key) or []:
                if fmt.get("ext") != "json3" or not fmt.get("url"):
                    continue
                try:
                    words = from_youtube_json3(json.loads(ydl.urlopen(fmt["url"]).read()))
                except Exception as exc:
                    log.debug("caption download failed: %s", exc)
                    continue
                if words:
                    return words
    return None


def fetch(entry: dict, src: dict, workdir: Path, long_video_minutes: float = 20) -> Fetched:
    workdir.mkdir(parents=True, exist_ok=True)
    if entry["live"]:
        return _record_live(entry, src, workdir)
    with _ydl({"noplaylist": True}) as ydl:
        info = ydl.extract_info(entry["url"], download=False)
        words = _site_transcript(ydl, info)
    duration = float(info.get("duration") or 0)
    sectioned = duration > long_video_minutes * 60
    path = _download_audio(entry["url"], workdir) if sectioned else _download_full(entry["url"], workdir)
    return Fetched(
        source_id=entry["id"], url=entry["url"], title=info.get("title") or "",
        uploader=info.get("uploader") or "", duration=duration, analysis_path=path, workdir=workdir,
        full_video=not sectioned, heatmap=info.get("heatmap"), words=words,
    )


def _record_live(entry: dict, src: dict, workdir: Path) -> Fetched:
    seconds = int(src.get("record_seconds", 1200))
    with _ydl({"format": "b[height<=1080]/b"}) as ydl:
        info = ydl.extract_info(entry["url"], download=False)
    path = workdir / f"live_{info.get('id')}_{int(time.time())}.mp4"
    log.info("recording %ss of live stream %s", seconds, entry["url"])
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    headers = "".join(f"{k}: {v}\r\n" for k, v in (info.get("http_headers") or {}).items())
    if headers:
        cmd += ["-headers", headers]
    cmd += ["-i", info["url"], "-t", str(seconds), "-c", "copy", "-movflags", "+faststart", str(path)]
    subprocess.run(cmd, check=True, timeout=seconds + 300)
    return Fetched(
        source_id=entry["id"], url=entry["url"], title=info.get("description") or info.get("title") or "",
        uploader=info.get("uploader") or "", duration=0.0, analysis_path=path, workdir=workdir, full_video=True,
    )
