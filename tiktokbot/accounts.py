"""Several TikTok accounts in one bot: each account keeps its own data folder."""
from __future__ import annotations

import copy
import json
import logging
import re
import secrets
import shutil
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .pipeline import Bot
from .tiktok_api import TikTokClient

log = logging.getLogger(__name__)
MAIN = "main"
TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if private:
        path.chmod(0o600)


def safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)[:80] or "account"


class Accounts:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.root = Path(cfg["paths"]["data_dir"])

    @property
    def app_file(self) -> Path:
        return self.root / "app.json"

    def app_settings(self) -> dict:
        tk = self.cfg["tiktok"]
        saved = _read(self.app_file)
        return {
            "client_key": tk["client_key"] or saved.get("client_key", ""),
            "client_secret": tk["client_secret"] or saved.get("client_secret", ""),
            "redirect_uri": saved.get("redirect_uri") or tk["redirect_uri"],
            "mode": saved.get("mode") if saved.get("mode") in ("direct", "draft") else tk["mode"],
        }

    def app_ready(self) -> bool:
        app = self.app_settings()
        return bool(app["client_key"] and app["client_secret"] and app["redirect_uri"])

    def save_mode(self, mode: str) -> None:
        if mode not in ("direct", "draft"):
            raise ValueError("publishing must be draft or direct")
        current = _read(self.app_file)
        current["mode"] = mode
        _write(self.app_file, current, private=True)

    def save_app(self, client_key: str, client_secret: str, redirect_uri: str) -> None:
        current = _read(self.app_file)
        current.update({k: v.strip() for k, v in
                        (("client_key", client_key), ("client_secret", client_secret), ("redirect_uri", redirect_uri)) if v.strip()})
        _write(self.app_file, current, private=True)

    def folder(self, account_id: str) -> Path:
        return self.root if account_id == MAIN else self.root / "accounts" / safe_id(account_id)

    def ids(self) -> list[str]:
        out = [MAIN]
        base = self.root / "accounts"
        if base.is_dir():
            out += sorted(p.name for p in base.iterdir() if p.is_dir() and (p / "account.json").exists())
        return out

    def settings(self, account_id: str) -> dict:
        return _read(self.folder(account_id) / "account.json")

    def save_settings(self, account_id: str, post_times: list[str] | None = None,
                      require_approval: bool | None = None, timezone: str | None = None) -> dict:
        data = self.settings(account_id)
        if timezone is not None:
            try:
                ZoneInfo(timezone.strip())
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("unknown time zone - use a name like Europe/Berlin or America/New_York") from None
            data["timezone"] = timezone.strip()
        if post_times is not None:
            times = sorted({t.strip() for t in post_times if t.strip()})
            if not times or not all(TIME_RE.fullmatch(t) for t in times):
                raise ValueError("post times must look like 09:00, 15:00, 20:00")
            data["post_times"] = times
        if require_approval is not None:
            data["require_approval"] = bool(require_approval)
        _write(self.folder(account_id) / "account.json", data)
        return data

    def cfg_for(self, account_id: str) -> dict:
        if account_id not in self.ids():
            raise KeyError(account_id)
        cfg = copy.deepcopy(self.cfg)
        cfg["tiktok"].update(self.app_settings())
        cfg["paths"]["data_dir"] = str(self.folder(account_id))
        if account_id != MAIN:
            cfg["sources"] = []
        extra = self.settings(account_id)
        if extra.get("post_times"):
            cfg["schedule"]["post_times"] = list(extra["post_times"])
        if extra.get("timezone"):
            cfg["schedule"]["timezone"] = extra["timezone"]
        if "require_approval" in extra:
            cfg["review"]["require_approval"] = bool(extra["require_approval"])
        return cfg

    def bot(self, account_id: str) -> Bot:
        return Bot(self.cfg_for(account_id))

    def describe(self, account_id: str) -> dict:
        info = self.settings(account_id)
        folder = self.folder(account_id)
        name = info.get("name") or ("Main account" if account_id == MAIN else account_id)
        token = _read(folder / "tiktok_token.json")
        need = "video.upload" if self.app_settings()["mode"] == "draft" else "video.publish"
        granted = str(token.get("scope", "")).replace(" ", "").split(",")
        return {"id": account_id, "name": name, "avatar": info.get("avatar", ""), "connected": bool(token),
                "needs_reconnect": bool(token) and bool(token.get("scope")) and need not in granted}

    def list(self) -> list[dict]:
        return [self.describe(i) for i in self.ids()]

    def start_connect(self) -> str:
        state = secrets.token_urlsafe(16)
        pending = self.root / "pending" / state
        client = TikTokClient(self.cfg_for(MAIN)["tiktok"], pending / "tiktok_token.json")
        url = client.authorize_url(state=state)
        _write(pending / "started.json", {"at": time.time()})
        self._prune_pending()
        return url

    def _prune_pending(self, max_age: float = 3600) -> None:
        base = self.root / "pending"
        for p in base.glob("*") if base.is_dir() else []:
            started = _read(p / "started.json").get("at", 0)
            if time.time() - started > max_age:
                shutil.rmtree(p, ignore_errors=True)

    def finish_connect(self, state: str, code: str) -> dict:
        pending = self.root / "pending" / safe_id(state)
        if not state or not (pending / "started.json").exists():
            raise ValueError("this login link expired - click Connect again")
        try:
            client = TikTokClient(self.cfg_for(MAIN)["tiktok"], pending / "tiktok_token.json")
            token = client.exchange_code(code)
            open_id = str(token.get("open_id") or "")
            profile = {}
            try:
                profile = client.user_info()
            except Exception as exc:
                log.warning("could not read TikTok profile: %s", exc)
            open_id = open_id or str(profile.get("open_id") or "") or secrets.token_hex(6)

            account_id = self._account_for(open_id)
            folder = self.folder(account_id)
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / "tiktok_token.json"
            shutil.copyfile(pending / "tiktok_token.json", target)
            target.chmod(0o600)
            info = self.settings(account_id)
            info.update({"open_id": open_id, "name": profile.get("display_name") or info.get("name") or open_id,
                         "avatar": profile.get("avatar_url", ""), "connected_at": time.time()})
            _write(folder / "account.json", info)
            return self.describe(account_id)
        finally:
            shutil.rmtree(pending, ignore_errors=True)

    def _account_for(self, open_id: str) -> str:
        main_token = _read(self.root / "tiktok_token.json")
        main_info = self.settings(MAIN)
        if not main_token or open_id in (main_token.get("open_id"), main_info.get("open_id")):
            return MAIN
        for account_id in self.ids()[1:]:
            if self.settings(account_id).get("open_id") == open_id:
                return account_id
        return safe_id(open_id)

    def disconnect(self, account_id: str) -> None:
        if account_id not in self.ids():
            raise KeyError(account_id)
        (self.folder(account_id) / "tiktok_token.json").unlink(missing_ok=True)


class Runner:
    def __init__(self, cfg: dict):
        self.accounts = Accounts(cfg)
        self.last_harvest: dict[str, float] = {}
        self.last_stats: dict[str, float] = {}

    def step(self, dry_run: bool = False) -> None:
        for info in self.accounts.list():
            if info["id"] != MAIN and not info["connected"]:
                continue
            try:
                bot = self.accounts.bot(info["id"])
                if bot.cfg["stats"]["enabled"] and time.time() - self.last_stats.get(info["id"], 0) > 6 * 3600:
                    self.last_stats[info["id"]] = time.time()
                    try:
                        bot.sync_stats()
                    except Exception as exc:
                        log.warning("[%s] statistics update failed: %s", info["name"], exc)
                if (bot.db.count("queued", "pending") < bot.cfg["clips"]["queue_target"]
                        and time.time() - self.last_harvest.get(info["id"], 0) > 3600):
                    self.last_harvest[info["id"]] = time.time()
                    bot.cleanup()
                    bot.harvest()
                bot.tick(dry_run=dry_run)
            except Exception:
                log.exception("[%s] scheduler step failed", info["name"])

    def run_forever(self, dry_run: bool = False) -> None:
        log.info("scheduler running for %d account(s)", len(self.accounts.ids()))
        while True:
            self.step(dry_run)
            time.sleep(30)
