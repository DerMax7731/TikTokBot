"""TikTok Content Posting API client (official API: OAuth + direct post by file upload)."""
from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

log = logging.getLogger(__name__)

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
SCOPES = "user.info.basic,video.publish"
MIN_CHUNK = 5 * 1024 * 1024
CHUNK = 10 * 1024 * 1024


class TikTokError(RuntimeError):
    pass


class TikTokAuthError(TikTokError):
    """The account is not connected (or the connection expired): retrying will not help."""


def plan_chunks(size: int, chunk: int = CHUNK) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) following TikTok's rules: files below
    5 MB go up whole; otherwise the last chunk absorbs the remainder."""
    if size < MIN_CHUNK or size <= chunk:
        return size, 1
    return chunk, size // chunk


class TikTokClient:
    def __init__(self, cfg: dict, token_path: str | Path):
        self.client_key = cfg["client_key"]
        self.client_secret = cfg["client_secret"]
        self.redirect_uri = cfg["redirect_uri"]
        self.privacy_level = cfg.get("privacy_level", "SELF_ONLY")
        self.token_path = Path(token_path)
        self.session = requests.Session()

    # ---- OAuth -----------------------------------------------------------
    def authorize_url(self) -> str:
        if not (self.client_key and self.client_secret and self.redirect_uri):
            raise TikTokError("tiktok.client_key, client_secret and redirect_uri must be set")
        query = {
            "client_key": self.client_key,
            "scope": SCOPES,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "state": secrets.token_urlsafe(16),
        }
        return f"{AUTH_URL}?{urlencode(query)}"

    def exchange_code(self, code_or_url: str) -> dict:
        code = code_or_url.strip()
        if "code=" in code:
            code = parse_qs(urlparse(code).query)["code"][0]
        return self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri}
        )

    def _token_request(self, extra: dict) -> dict:
        data = {"client_key": self.client_key, "client_secret": self.client_secret, **extra}
        resp = self.session.post(f"{API}/oauth/token/", data=data, timeout=30)
        tok = resp.json()
        if "access_token" not in tok:
            raise TikTokError(f"token request failed: {tok.get('error_description') or tok}")
        tok["expires_at"] = time.time() + int(tok.get("expires_in", 0))
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(tok))
        self.token_path.chmod(0o600)
        return tok

    def access_token(self) -> str:
        if not self.token_path.exists():
            raise TikTokAuthError("not authorized yet - run `tiktokbot auth` first")
        tok = json.loads(self.token_path.read_text())
        if tok.get("expires_at", 0) - 300 < time.time():
            log.info("refreshing TikTok access token")
            try:
                tok = self._token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
            except TikTokError as exc:
                raise TikTokAuthError(f"{exc} - run `tiktokbot auth` again") from None
        return tok["access_token"]

    # ---- Posting ---------------------------------------------------------
    def _post(self, endpoint: str, body: dict | None = None) -> dict:
        resp = self.session.post(
            f"{API}{endpoint}",
            headers={
                "Authorization": f"Bearer {self.access_token()}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json=body or {},
            timeout=60,
        )
        try:
            payload = resp.json()
        except ValueError:
            raise TikTokError(f"{endpoint}: HTTP {resp.status_code}") from None
        err = payload.get("error") or {}
        if err.get("code") in ("access_token_invalid", "scope_not_authorized"):
            raise TikTokAuthError(f"{err.get('code')} - run `tiktokbot auth` again")
        if err.get("code", "ok") != "ok":
            raise TikTokError(f"{endpoint}: {err.get('code')} - {err.get('message')}")
        return payload.get("data") or {}

    def creator_info(self) -> dict:
        return self._post("/post/publish/creator_info/query/")

    def upload_video(self, path: str | Path, caption: str, wait: bool = True) -> str:
        path = Path(path)
        size = path.stat().st_size
        chunk_size, chunks = plan_chunks(size)

        privacy = self.privacy_level
        options = self.creator_info().get("privacy_level_options") or []
        if options and privacy not in options:
            log.warning("privacy level %s not available for this account, using %s", privacy, options[-1])
            privacy = options[-1]

        data = self._post(
            "/post/publish/video/init/",
            {
                "post_info": {"title": caption, "privacy_level": privacy},
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": chunks,
                },
            },
        )
        publish_id, upload_url = data["publish_id"], data["upload_url"]

        with path.open("rb") as fh:
            for i in range(chunks):
                first = i * chunk_size
                last = size - 1 if i == chunks - 1 else first + chunk_size - 1
                fh.seek(first)
                resp = self.session.put(
                    upload_url,
                    data=fh.read(last - first + 1),
                    headers={"Content-Type": "video/mp4", "Content-Range": f"bytes {first}-{last}/{size}"},
                    timeout=600,
                )
                if resp.status_code not in (200, 201, 206):
                    raise TikTokError(f"chunk {i + 1}/{chunks} upload failed: HTTP {resp.status_code}")

        if wait:
            self.wait_published(publish_id)
        return publish_id

    def wait_published(self, publish_id: str, timeout: int = 600) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self._post("/post/publish/status/fetch/", {"publish_id": publish_id})
            status = data.get("status")
            if status == "PUBLISH_COMPLETE":
                return status
            if status == "FAILED":
                raise TikTokError(f"publishing failed: {data.get('fail_reason')}")
            time.sleep(10)
        raise TikTokError(f"publish {publish_id} still processing after {timeout}s")
