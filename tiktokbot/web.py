"""Local dashboard: watch clips, edit captions, approve or reject, see the schedule and results."""
from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .pipeline import Bot

log = logging.getLogger(__name__)
LOOPBACK = ("127.0.0.1", "localhost", "::1")
PAGE = (Path(__file__).parent / "dashboard.html").read_bytes()


def _state(bot: Bot) -> dict:
    clips = []
    for r in bot.db.clips():
        clips.append({
            "id": r["id"], "status": r["status"], "caption": r["caption"], "hook": r["hook"],
            "source": r["source_url"] or r["source_id"], "error": r["error"], "attempts": r["attempts"],
            "views": r["views"], "likes": r["likes"], "comments": r["comments"], "shares": r["shares"],
            "posted_at": r["posted_at"], "has_file": Path(r["path"]).exists(),
        })
    return {
        "clips": clips,
        "next_posts": [t.isoformat() for t in bot.upcoming_slots(3)],
        "timezone": bot.cfg["schedule"]["timezone"],
        "mode": bot.tiktok.mode,
        "connected": bot.tiktok.token_path.exists(),
        "require_approval": bool(bot.cfg["review"]["require_approval"]),
    }


def make_handler(cfg: dict):
    token = str(cfg["web"].get("token") or "")

    class Handler(BaseHTTPRequestHandler):
        server_version = "TikTokBot"

        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

        # -- helpers
        def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, data, status: int = 200) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def _authorized(self, query: dict) -> bool:
            if not token:
                return True
            cookie = SimpleCookie(self.headers.get("Cookie", "")).get("tt_token")
            given = (query.get("token") or [cookie.value if cookie else ""])[0]
            return hmac.compare_digest(given.encode(), token.encode())

        # -- routes
        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if not self._authorized(query):
                return self._send(401, b"Open this page with ?token=<web.token from config.yaml>", "text/plain")
            if url.path == "/":
                headers = {}
                if token and "token" in query:  # keep the token out of later URLs
                    headers["Set-Cookie"] = f"tt_token={token}; HttpOnly; SameSite=Strict; Path=/"
                return self._send(200, PAGE, "text/html; charset=utf-8", headers)
            if url.path == "/api/state":
                return self._json(_state(Bot(cfg)))
            if m := re.fullmatch(r"/media/(\d+)", url.path):
                return self._media(int(m.group(1)))
            self._send(404, b"not found", "text/plain")

        do_HEAD = do_GET

        def _media(self, clip_id: int) -> None:
            row = Bot(cfg).db.get(clip_id)
            path = Path(row["path"]) if row else None
            if not path or not path.is_file():
                return self._send(404, b"clip file not available", "text/plain")
            size = path.stat().st_size
            start, end = 0, size - 1
            if m := re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "")):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                elif m.group(2):
                    start = max(size - int(m.group(2)), 0)
                if start > end or start >= size:
                    return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            if self.command == "HEAD":
                return
            with path.open("rb") as fh:
                fh.seek(start)
                left = end - start + 1
                try:
                    while left > 0 and (chunk := fh.read(min(256 * 1024, left))):
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the browser stopped loading the video

        def do_POST(self):
            url = urlparse(self.path)
            if not self._authorized(parse_qs(url.query)):
                return self._json({"error": "unauthorized"}, 401)
            # Browsers cannot send JSON cross-site without a preflight, and the Origin
            # must be this server: blocks other websites from driving the dashboard.
            origin = self.headers.get("Origin")
            if (self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                    or (origin and urlparse(origin).netloc != self.headers.get("Host"))):
                return self._json({"error": "bad request"}, 400)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length <= 100_000 else None
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return self._json({"error": "bad request"}, 400)

            bot = Bot(cfg)
            if url.path == "/api/approve-all":
                rows = bot.db.clips("pending")
                for row in rows:
                    bot.db.set_status(row["id"], "queued")
                return self._json({"ok": True, "approved": len(rows)})
            m = re.fullmatch(r"/api/clips/(\d+)/(approve|reject|caption)", url.path)
            row = bot.db.get(int(m.group(1))) if m else None
            if not row:
                return self._json({"error": "not found"}, 404)
            action = m.group(2)
            if action == "caption":
                text = " ".join(str(body.get("text", "")).split())
                if not text or row["status"] not in ("pending", "queued"):
                    return self._json({"error": "caption cannot be changed"}, 409)
                bot.db.set_caption(row["id"], text[: int(cfg["caption"]["max_length"])])
            elif action == "approve":
                if row["status"] != "pending":
                    return self._json({"error": "clip is not waiting for approval"}, 409)
                bot.db.set_status(row["id"], "queued")
            else:
                if row["status"] not in ("pending", "queued"):
                    return self._json({"error": "clip can no longer be rejected"}, 409)
                bot._drop(row, "rejected")
            self._json({"ok": True})

    return Handler


def make_server(cfg: dict, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    host = host or cfg["web"]["host"]
    port = cfg["web"]["port"] if port is None else port
    if host not in LOOPBACK and not cfg["web"].get("token"):
        raise ValueError("set web.token in config.yaml before exposing the dashboard beyond this machine")
    server = ThreadingHTTPServer((host, int(port)), make_handler(cfg))
    server.daemon_threads = True
    return server


def serve_in_background(cfg: dict) -> ThreadingHTTPServer:
    server = make_server(cfg)
    threading.Thread(target=server.serve_forever, daemon=True, name="dashboard").start()
    log.info("dashboard on http://%s:%s", *server.server_address[:2])
    return server
