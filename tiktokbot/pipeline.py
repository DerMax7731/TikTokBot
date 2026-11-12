"""Harvest (download -> detect highlights -> cut clips) and the posting schedule."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import clipper, highlights, sources
from .db import DB
from .tiktok_api import TikTokClient

log = logging.getLogger(__name__)


def build_caption(cfg: dict, title: str, uploader: str, hashtags: list[str], part: int, total: int) -> str:
    ccfg = cfg["caption"]
    tags = " ".join(f"#{t.lstrip('#')}" for t in hashtags)
    text = ccfg["template"].format(title=title.strip(), hashtags=tags, uploader=uploader, part=part, total=total)
    if ccfg.get("credit") and uploader:
        text = f"{text} (via {uploader})"
    text = " ".join(text.split())
    return text[: int(ccfg["max_length"])]


class Bot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.data = Path(cfg["paths"]["data_dir"])
        self.db = DB(self.data / "state.db")
        self.tiktok = TikTokClient(cfg["tiktok"], self.data / "tiktok_token.json")
        self.tz = ZoneInfo(cfg["schedule"]["timezone"])

    # ---- Harvest ---------------------------------------------------------
    def harvest(self, force: bool = False) -> int:
        """Turn unseen source videos into queued clips. Returns clips created."""
        ccfg = self.cfg["clips"]
        created = 0
        for src in self.cfg["sources"]:
            if not force and self.db.queued_count() >= ccfg["queue_target"]:
                break
            try:
                entries = sources.list_candidates(src)
            except Exception as exc:  # one broken source must not stop the others
                log.error("cannot read source %s: %s", src["url"], exc)
                continue
            fresh = [e for e in entries if not self.db.is_seen(e["id"])][: int(src.get("latest", 2))]
            for entry in fresh:
                try:
                    created += self._process(entry, src)
                except Exception as exc:
                    log.error("failed to process %s: %s", entry["url"], exc)
                # Marked either way so a permanently broken video is not retried forever.
                self.db.mark_seen(entry["id"], entry["url"] or "")
        log.info("harvest created %d clip(s), %d queued", created, self.db.queued_count())
        return created

    def _process(self, entry: dict, src: dict) -> int:
        ccfg = self.cfg["clips"]
        media = sources.fetch(entry, src, self.data / "downloads")
        try:
            length = int(src.get("length_seconds", ccfg["length_seconds"]))
            duration = highlights.probe_duration(media.path)
            starts = highlights.pick_windows(
                highlights.audio_energy(media.path), duration, length,
                int(src.get("per_source", ccfg["per_source"])), int(ccfg["min_gap_seconds"]),
            )
            safe_id = "".join(c if c.isalnum() else "_" for c in media.source_id)
            for i, start in enumerate(starts, 1):
                out = self.data / "clips" / f"{safe_id}_{i}.mp4"
                clipper.cut_clip(media.path, out, start, min(length, duration), ccfg["vertical"])
                caption = build_caption(
                    self.cfg, media.title, media.uploader, src.get("hashtags", []), i, len(starts)
                )
                self.db.add_clip(str(out), caption, media.source_id)
                log.info("queued clip %s (start %.0fs)", out.name, start)
            return len(starts)
        finally:
            media.path.unlink(missing_ok=True)

    # ---- Posting ---------------------------------------------------------
    def post_next(self, dry_run: bool = False) -> int | None:
        """Post the next queued clip. Returns its id, or None if nothing was posted."""
        if self.db.queued_count() == 0:
            self.harvest()
        while (clip := self.db.next_clip()) is not None:
            if not Path(clip["path"]).exists():
                self.db.mark_failed(clip["id"], "clip file missing")
                continue
            if dry_run:
                log.info("[dry-run] would post %s: %s", clip["path"], clip["caption"])
                return clip["id"]
            try:
                publish_id = self.tiktok.upload_video(clip["path"], clip["caption"])
            except Exception as exc:
                log.error("posting clip %s failed: %s", clip["id"], exc)
                self.db.mark_failed(clip["id"], str(exc))
                return None
            self.db.mark_posted(clip["id"], publish_id)
            Path(clip["path"]).unlink(missing_ok=True)
            log.info("posted clip %s (publish_id %s)", clip["id"], publish_id)
            return clip["id"]
        log.warning("nothing to post: the queue is empty and no new source material was found")
        return None

    def due_slots(self, now: datetime | None = None) -> list[str]:
        """Post slots that are due now (within the grace period) and not handled yet."""
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        grace = timedelta(minutes=int(self.cfg["schedule"]["grace_minutes"]))
        due = []
        for day in (now.date() - timedelta(days=1), now.date()):
            for hhmm in self.cfg["schedule"]["post_times"]:
                hour, minute = map(int, hhmm.split(":"))
                at = datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.tz)
                slot = f"{day.isoformat()} {hhmm}"
                if at <= now <= at + grace and not self.db.slot_done(slot):
                    due.append(slot)
        return due

    def tick(self, now: datetime | None = None, dry_run: bool = False) -> int:
        """Handle every due slot once. Returns the number of posts made."""
        posted = 0
        for slot in self.due_slots(now):
            clip_id = self.post_next(dry_run=dry_run)
            self.db.mark_slot(slot, clip_id)
            posted += clip_id is not None
        return posted

    def run_forever(self, dry_run: bool = False) -> None:
        log.info("scheduler running - posting daily at %s (%s)",
                 ", ".join(self.cfg["schedule"]["post_times"]), self.cfg["schedule"]["timezone"])
        last_harvest = 0.0
        while True:
            try:
                # Keep the queue stocked ahead of time so posts go out on the minute.
                if (self.db.queued_count() < self.cfg["clips"]["queue_target"]
                        and time.time() - last_harvest > 3600):
                    last_harvest = time.time()
                    self.harvest()
                self.tick(dry_run=dry_run)
            except Exception:
                log.exception("scheduler tick failed")
            time.sleep(30)
