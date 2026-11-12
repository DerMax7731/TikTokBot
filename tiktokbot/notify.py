"""Optional webhook notifications (Discord- and Slack-compatible)."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


def send(cfg: dict, message: str) -> None:
    url = (cfg or {}).get("webhook_url")
    if not url:
        return
    try:
        requests.post(url, json={"content": message, "text": message}, timeout=10)
    except requests.RequestException as exc:
        log.warning("notification failed: %s", exc)
