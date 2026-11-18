"""Configuration loading with defaults."""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path

import yaml

DEFAULTS = {
    "tiktok": {
        "client_key": "",
        "client_secret": "",
        "redirect_uri": "http://localhost:8080/callback",
        "privacy_level": "SELF_ONLY",
        "mode": "draft",
    },
    "schedule": {
        "timezone": "UTC",
        "post_times": ["09:00", "15:00", "20:00"],
        "grace_minutes": 180,
    },
    "clips": {
        "length_seconds": 35,
        "per_source": 3,
        "min_gap_seconds": 20,
        "layout": "stream",
        "subtitle_style": "normal",
        "subtitles": True,
        "hook_overlay": True,
        "normalize_audio": True,
        "candidates_factor": 3,
        "long_video_minutes": 20,
        "font": "DejaVu Sans",
        "queue_target": 6,
    },
    "ai": {"enabled": True, "model": "claude-opus-5-5", "language": ""},
    "whisper": {"model": "small"},
    "review": {"require_approval": False},
    "posting": {"max_attempts": 3, "retry_minutes": 10},
    "notify": {"webhook_url": ""},
    "stats": {"enabled": False},
    "web": {"host": "127.0.0.1", "port": 8080, "token": ""},
    "sources": [],
    "caption": {"template": "{title} {hashtags}", "credit": True, "max_length": 2200},
    "paths": {"data_dir": "data"},
}


class ConfigError(ValueError):
    pass


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path = "config.yaml") -> dict:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg = _merge(DEFAULTS, raw or {})

    tk = cfg["tiktok"]
    cfg["web"]["host"] = os.environ.get("TIKTOKBOT_WEB_HOST") or cfg["web"]["host"]
    cfg["web"]["token"] = os.environ.get("TIKTOKBOT_WEB_TOKEN") or cfg["web"]["token"]
    tk["client_key"] = os.environ.get("TIKTOK_CLIENT_KEY") or tk["client_key"]
    tk["client_secret"] = os.environ.get("TIKTOK_CLIENT_SECRET") or tk["client_secret"]

    times = cfg["schedule"]["post_times"]
    if not times:
        raise ConfigError("schedule.post_times must list at least one HH:MM time")
    for t in times:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(t)):
            raise ConfigError(f"invalid post time {t!r}, expected HH:MM")
    cfg["schedule"]["post_times"] = sorted(set(map(str, times)))

    if "vertical" in cfg["clips"]:  # pre-layout configs
        cfg["clips"]["layout"] = "blur" if cfg["clips"].pop("vertical") else "fit"
    if cfg["clips"]["layout"] not in ("stream", "smart", "blur", "crop", "fit"):
        raise ConfigError("clips.layout must be stream, smart, blur, crop or fit")
    if tk["mode"] not in ("direct", "draft"):
        raise ConfigError("tiktok.mode must be direct or draft")
    tk["stats"] = bool(cfg["stats"]["enabled"])

    sources = []
    for src in cfg["sources"] or []:
        if isinstance(src, str):
            src = {"url": src}
        if not src.get("url"):
            raise ConfigError("every source needs a url")
        sources.append(src)
    cfg["sources"] = sources

    data_dir = Path(cfg["paths"]["data_dir"])
    if not data_dir.is_absolute():
        data_dir = path.resolve().parent / data_dir
    cfg["paths"]["data_dir"] = str(data_dir)
    return cfg
