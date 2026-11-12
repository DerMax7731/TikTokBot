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
        "redirect_uri": "",
        "privacy_level": "SELF_ONLY",
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
        "vertical": True,
        "queue_target": 6,
    },
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
    if not path.exists():
        raise ConfigError(f"{path} not found - copy config.example.yaml to {path} and edit it")
    cfg = _merge(DEFAULTS, yaml.safe_load(path.read_text()) or {})

    tk = cfg["tiktok"]
    tk["client_key"] = os.environ.get("TIKTOK_CLIENT_KEY") or tk["client_key"]
    tk["client_secret"] = os.environ.get("TIKTOK_CLIENT_SECRET") or tk["client_secret"]

    times = cfg["schedule"]["post_times"]
    if not times:
        raise ConfigError("schedule.post_times must list at least one HH:MM time")
    for t in times:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(t)):
            raise ConfigError(f"invalid post time {t!r}, expected HH:MM")
    cfg["schedule"]["post_times"] = sorted(set(map(str, times)))

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
