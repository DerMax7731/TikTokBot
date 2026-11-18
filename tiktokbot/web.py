"""Local dashboard: connect TikTok accounts, manage streamers, review clips, see the schedule and results."""
from __future__ import annotations

import hmac
import html
import json
import logging
import re
import threading
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .accounts import MAIN, Accounts
from .pipeline import Bot

log = logging.getLogger(__name__)
LOOPBACK = ("127.0.0.1", "localhost", "::1")
PAGE = (Path(__file__).parent / "dashboard.html").read_bytes()
_HARVEST_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(account_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _HARVEST_LOCKS.setdefault(account_id, threading.Lock())


def _sources(bot: Bot) -> list[dict]:
    mine = {s["url"] for s in bot.dashboard_sources()}
    return [{
        "url": s["url"], "match": s.get("match") or [], "hashtags": s.get("hashtags") or [],
        "live": bool(s.get("record_seconds")), "removable": s["url"] in mine,
    } for s in bot.all_sources()]


def _state(accounts: Accounts, account_id: str, bot: Bot) -> dict:
    clips = []
    for r in bot.db.clips():
        clips.append({
            "id": r["id"], "status": r["status"], "caption": r["caption"], "hook": r["hook"],
            "source": r["source_url"] or r["source_id"], "error": r["error"], "attempts": r["attempts"],
            "views": r["views"], "likes": r["likes"], "comments": r["comments"], "shares": r["shares"],
            "posted_at": r["posted_at"], "has_file": Path(r["path"]).exists(),
        })
    return {
        "app_ready": accounts.app_ready(),
        "setup_done": accounts.app_ready() or bot.tiktok.mode == "download",
        "redirect_uri": accounts.app_settings()["redirect_uri"],
        "needs_reconnect": accounts.describe(account_id)["needs_reconnect"],
        "accounts": accounts.list(),
        "account": account_id,
        "clips": clips,
        "next_posts": [t.isoformat() for t in bot.upcoming_slots(3)],
        "post_times": bot.cfg["schedule"]["post_times"],
        "timezone": bot.cfg["schedule"]["timezone"],
        "mode": bot.tiktok.mode,
        "connected": bot.tiktok.token_path.exists(),
        "require_approval": bool(bot.cfg["review"]["require_approval"]),
        "sources": _sources(bot),
        "harvesting": _lock(account_id).locked(),
    }


def _harvest_in_background(accounts: Accounts, account_id: str) -> bool:
    lock = _lock(account_id)
    if not lock.acquire(blocking=False):
        return False

    def work():
        try:
            accounts.bot(account_id).harvest(force=True)
        except Exception:
            log.exception("harvest from dashboard failed")
        finally:
            lock.release()

    threading.Thread(target=work, daemon=True, name=f"harvest-{account_id}").start()
    return True


def _words(value) -> list[str]:
    if isinstance(value, list):
        value = ",".join(map(str, value))
    return [w.strip() for w in str(value or "").split(",") if w.strip()][:20]


def _message_page(title: str, text: str, link: str = "/") -> bytes:
    return (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width, initial-scale=1'>"
        "<title>TikTokBot</title><style>body{font:16px/1.5 system-ui,sans-serif;max-width:520px;margin:15vh auto;"
        "padding:0 16px;background:#f5f4f1;color:#1c1b19}@media(prefers-color-scheme:dark){body{background:#131312;"
        "color:#ecebe7}a{color:#2dd4bf}}a{color:#0f766e}</style>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(text)}</p><p><a href='{html.escape(link)}'>Back to the dashboard</a></p>"
    ).encode()


def make_handler(cfg: dict):
    token = str(cfg["web"].get("token") or "")
    accounts = Accounts(cfg)

    def callback_path() -> str:
        return urlparse(accounts.app_settings()["redirect_uri"] or "").path or "/callback"

    class Handler(BaseHTTPRequestHandler):
        server_version = "TikTokBot"

        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

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

        def _redirect(self, location: str) -> None:
            self._send(302, b"", "text/plain", {"Location": location})

        def _authorized(self, query: dict) -> bool:
            if not token:
                return True
            cookie = SimpleCookie(self.headers.get("Cookie", "")).get("tt_token")
            given = (query.get("token") or [cookie.value if cookie else ""])[0]
            return hmac.compare_digest(given.encode(), token.encode())

        @staticmethod
        def _account(query: dict, body: dict | None = None) -> str:
            return str((body or {}).get("account") or (query.get("account") or [MAIN])[0])

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == callback_path():
                return self._callback(query)
            if not self._authorized(query):
                return self._send(401, b"Open this page with ?token=<web.token from config.yaml>", "text/plain")
            if url.path == "/":
                headers = {}
                if token and "token" in query:
                    headers["Set-Cookie"] = f"tt_token={token}; HttpOnly; SameSite=Lax; Path=/"
                return self._send(200, PAGE, "text/html; charset=utf-8", headers)
            if url.path == "/connect":
                if not accounts.app_ready():
                    return self._redirect("/")
                try:
                    return self._redirect(accounts.start_connect())
                except Exception as exc:
                    return self._send(500, _message_page("Could not start the TikTok login", str(exc)), "text/html; charset=utf-8")
            account_id = self._account(query)
            try:
                bot = accounts.bot(account_id)
            except KeyError:
                return self._json({"error": "unknown account"}, 404)
            if url.path == "/api/state":
                return self._json(_state(accounts, account_id, bot))
            if m := re.fullmatch(r"/media/(\d+)", url.path):
                return self._media(bot, int(m.group(1)), "download" in query)
            self._send(404, b"not found", "text/plain")

        do_HEAD = do_GET

        def _callback(self, query: dict) -> None:
            if query.get("error"):
                reason = (query.get("error_description") or query.get("error"))[0]
                return self._send(400, _message_page("TikTok login was cancelled", reason), "text/html; charset=utf-8")
            try:
                info = accounts.finish_connect((query.get("state") or [""])[0], (query.get("code") or [""])[0])
            except Exception as exc:
                log.warning("TikTok login failed: %s", exc)
                return self._send(400, _message_page("TikTok login failed", str(exc)), "text/html; charset=utf-8")
            log.info("TikTok account connected: %s", info["name"])
            self._redirect(f"/?account={quote(info['id'])}&connected=1")

        def _media(self, bot: Bot, clip_id: int, attachment: bool = False) -> None:
            row = bot.db.get(clip_id)
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
            if attachment:
                self.send_header("Content-Disposition", f'attachment; filename="tiktok-clip-{clip_id}.mp4"')
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
                    pass

        def do_POST(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if not self._authorized(query):
                return self._json({"error": "unauthorized"}, 401)
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

            if url.path == "/api/manual":
                accounts.save_mode("download")
                return self._json({"ok": True})
            if url.path == "/api/app":
                key, secret = str(body.get("client_key", "")), str(body.get("client_secret", ""))
                redirect = str(body.get("redirect_uri", "")).strip()
                if redirect and urlparse(redirect).scheme not in ("http", "https"):
                    return self._json({"error": "the redirect URI must start with http:// or https://"}, 400)
                accounts.save_app(key, secret, redirect)
                if not accounts.app_ready():
                    return self._json({"error": "client key, client secret and redirect URI are all needed"}, 400)
                return self._json({"ok": True})

            account_id = self._account(query, body)
            try:
                bot = accounts.bot(account_id)
            except KeyError:
                return self._json({"error": "unknown account"}, 404)

            if url.path == "/api/accounts/disconnect":
                accounts.disconnect(account_id)
                return self._json({"ok": True})
            if url.path == "/api/settings":
                try:
                    if "mode" in body:
                        accounts.save_mode(str(body["mode"]))
                    accounts.save_settings(
                        account_id,
                        _words(body["post_times"]) if "post_times" in body else None,
                        body.get("require_approval") if "require_approval" in body else None,
                        str(body["timezone"]) if body.get("timezone") else None,
                    )
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
                return self._json({"ok": True})
            if url.path == "/api/sources":
                try:
                    src = bot.add_source(str(body.get("url", "")), _words(body.get("match")), _words(body.get("hashtags")))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
                return self._json({"ok": True, "source": src})
            if url.path == "/api/sources/remove":
                if not bot.remove_source(str(body.get("url", ""))):
                    return self._json({"error": "only links added in the dashboard can be removed here"}, 404)
                return self._json({"ok": True})
            if url.path == "/api/harvest":
                if not _harvest_in_background(accounts, account_id):
                    return self._json({"error": "already checking for new clips"}, 409)
                return self._json({"ok": True})
            if url.path == "/api/approve-all":
                rows = bot.db.clips("pending")
                for row in rows:
                    bot.db.set_status(row["id"], "queued")
                return self._json({"ok": True, "approved": len(rows)})
            m = re.fullmatch(r"/api/clips/(\d+)/(approve|reject|caption|done)", url.path)
            row = bot.db.get(int(m.group(1))) if m else None
            if not row:
                return self._json({"error": "not found"}, 404)
            action = m.group(2)
            if action == "caption":
                text = " ".join(str(body.get("text", "")).split())
                if not text or row["status"] not in ("pending", "queued", "ready"):
                    return self._json({"error": "caption cannot be changed"}, 409)
                bot.db.set_caption(row["id"], text[: int(cfg["caption"]["max_length"])])
            elif action == "done":
                if not bot.mark_done(row["id"]):
                    return self._json({"error": "clip is not waiting to be published"}, 409)
            elif action == "approve":
                if row["status"] != "pending":
                    return self._json({"error": "clip is not waiting for approval"}, 409)
                bot.db.set_status(row["id"], "queued")
            else:
                if row["status"] not in ("pending", "queued", "ready"):
                    return self._json({"error": "clip can no longer be rejected"}, 409)
                bot._drop(row, "rejected")
            self._json({"ok": True})

    return Handler


def make_server(cfg: dict, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    host = host or cfg["web"]["host"]
    port = cfg["web"]["port"] if port is None else port
    if host not in LOOPBACK and not cfg["web"].get("token"):
        raise ValueError("set web.token in config.yaml (or the TIKTOKBOT_WEB_TOKEN variable) before exposing the dashboard beyond this machine")
    server = ThreadingHTTPServer((host, int(port)), make_handler(cfg))
    server.daemon_threads = True
    return server


def serve_in_background(cfg: dict) -> ThreadingHTTPServer:
    server = make_server(cfg)
    threading.Thread(target=server.serve_forever, daemon=True, name="dashboard").start()
    log.info("dashboard on http://%s:%s", *server.server_address[:2])
    return server
