"""Harvest (fetch -> score -> pick -> render clips) and the posting schedule."""
from __future__ import annotations

import json
from urllib.parse import urlparse
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import ai, clipper, highlights, notify, sources, transcript, vision
from .db import DB
from .tiktok_api import TikTokAuthError, TikTokClient

log = logging.getLogger(__name__)


def _tags(*groups) -> str:
    seen, out = set(), []
    for tag in (t for g in groups for t in (g or [])):
        tag = "".join(ch for ch in str(tag).lstrip("#") if ch.isalnum() or ch == "_")
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(f"#{tag}")
    return " ".join(out[:8])


def build_caption(cfg: dict, title: str, uploader: str, hashtags: list[str], part: int = 1, total: int = 1,
                  ai_caption: str | None = None, ai_hashtags: list[str] | None = None) -> str:
    ccfg = cfg["caption"]
    tags = _tags(hashtags, ai_hashtags)
    if ai_caption:
        text = f"{ai_caption.strip()} {tags}"
    else:
        text = ccfg["template"].format(title=title.strip(), hashtags=tags, uploader=uploader, part=part, total=total)
    if ccfg.get("credit") and uploader:
        text = f"{text} (via {uploader})"
    return " ".join(text.split())[: int(ccfg["max_length"])]


class Bot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.data = Path(cfg["paths"]["data_dir"])
        self.db = DB(self.data / "state.db")
        self.tiktok = TikTokClient(cfg["tiktok"], self.data / "tiktok_token.json")
        self.tz = ZoneInfo(cfg["schedule"]["timezone"])

    # ---- Harvest ---------------------------------------------------------
    def harvest(self, force: bool = False) -> int:
        """Turn unseen source videos into clips. Returns clips created."""
        ccfg = self.cfg["clips"]
        created = 0
        for src in self.ranked_sources():
            if not force and self.db.count("queued", "pending") >= ccfg["queue_target"]:
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
        log.info("harvest created %d clip(s): %d queued, %d awaiting approval",
                 created, self.db.count("queued"), self.db.count("pending"))
        return created

    @property
    def _sources_file(self) -> Path:
        return self.data / "sources.json"

    def dashboard_sources(self) -> list[dict]:
        try:
            items = json.loads(self._sources_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [s for s in items if isinstance(s, dict) and s.get("url")]

    def all_sources(self) -> list[dict]:
        seen, out = set(), []
        for src in list(self.cfg["sources"]) + self.dashboard_sources():
            if src["url"] not in seen:
                seen.add(src["url"])
                out.append(src)
        return out

    def add_source(self, url: str, match: list[str] | None = None, hashtags: list[str] | None = None) -> dict:
        url = url.strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or len(url) > 500:
            raise ValueError("enter a full link starting with https://")
        if any(s["url"] == url for s in self.all_sources()):
            raise ValueError("this link is already in the list")
        src: dict = {"url": url, "latest": 2}
        host = parsed.netloc.lower()
        if "twitch.tv" in host or "kick.com" in host or url.rstrip("/").endswith("/live"):
            src["record_seconds"] = 1200
        if match:
            src["match"] = [m for m in (k.strip().lower() for k in match) if m]
        if hashtags:
            src["hashtags"] = [h for h in (t.strip().lstrip("#") for t in hashtags) if h]
        items = self.dashboard_sources() + [src]
        self.data.mkdir(parents=True, exist_ok=True)
        self._sources_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
        return src

    def remove_source(self, url: str) -> bool:
        items = self.dashboard_sources()
        keep = [s for s in items if s["url"] != url]
        if len(keep) == len(items):
            return False
        self._sources_file.write_text(json.dumps(keep, indent=2), encoding="utf-8")
        return True

    def ranked_sources(self) -> list[dict]:
        """Configured sources, those whose clips earned the most views first.
        Sources without data yet rank as average, so new ones still get their turn."""
        sources_now = self.all_sources()
        perf = self.db.source_performance()
        if not perf:
            return sources_now
        mean = sum(perf.values()) / len(perf)
        return sorted(sources_now, key=lambda s: -perf.get(s["url"], mean))

    def _words(self, media, a: float, b: float) -> list:
        """Words spoken between a and b, timed in source seconds."""
        if media.words:
            return [w for w in media.words if a <= w.start < b]
        if transcript.whisper_available():
            try:
                rel = transcript.transcribe(media.analysis_path, a, b - a, self.cfg["whisper"]["model"])
                return [transcript.Word(w.start + a, w.end + a, w.text) for w in rel]
            except Exception as exc:
                log.warning("transcription failed: %s", exc)
        return []

    def _focus(self, seg, offset: float, length: float) -> float | None:
        if not vision.available():
            return None
        try:
            return vision.face_center_x(seg, offset, length)
        except Exception as exc:
            log.warning("face detection failed: %s", exc)
            return None

    def _process(self, entry: dict, src: dict) -> int:
        ccfg = self.cfg["clips"]
        media = sources.fetch(entry, src, self.data / "downloads", ccfg["long_video_minutes"])
        try:
            length = int(src.get("length_seconds", ccfg["length_seconds"]))
            want = int(src.get("per_source", ccfg["per_source"]))
            media.duration = duration = media.duration or highlights.probe_duration(media.analysis_path)
            energy = highlights.audio_energy(media.analysis_path)
            score = highlights.build_score(energy, media.heatmap)

            use_ai = ai.available(self.cfg["ai"])
            need_words = use_ai or ccfg["subtitles"]
            can_words = bool(media.words) or transcript.whisper_available()
            pool = want * int(ccfg["candidates_factor"]) if use_ai and can_words else want

            cands = []
            for i, (start, sc) in enumerate(highlights.pick_windows(score, duration, length, pool, int(ccfg["min_gap_seconds"]))):
                end = start + length
                around = self._words(media, max(start - 3, 0), min(end + 8, duration)) if need_words else []
                if around:
                    start, end = transcript.refine_bounds(around, start, end)
                else:
                    start = highlights.snap_to_quiet(energy, start)
                    end = start + length
                start = max(min(start, duration - 1), 0.0)
                clip_len = min(end, duration) - start
                words = transcript.slice_words(around, start, clip_len)
                cands.append({"index": i, "start": start, "length": clip_len, "score": sc,
                              "words": words, "transcript": transcript.text(words)})

            picks = None
            if use_ai and any(c["transcript"] for c in cands):
                picks = ai.pick_clips(self.cfg["ai"], media.title, media.uploader, cands, want)
            chosen = [(cands[p["index"]], p) for p in picks] if picks else [(c, None) for c in cands[:want]]

            safe_id = "".join(ch if ch.isalnum() else "_" for ch in media.source_id)
            status = "pending" if self.cfg["review"]["require_approval"] else "queued"
            for n, (cand, pick) in enumerate(chosen, 1):
                out = self.data / "clips" / f"{safe_id}_{n}.mp4"
                hook = pick["hook"].strip() if pick and ccfg["hook_overlay"] else None
                subs = cand["words"] if ccfg["subtitles"] else []
                ass = None
                if subs or hook:
                    ass = transcript.build_ass(subs, out.with_suffix(".ass"), hook, cand["length"], ccfg["font"])
                seg, offset = media.segment(cand["start"], cand["length"])
                smart = ccfg["layout"] == "smart"
                focus = self._focus(seg, offset, cand["length"]) if smart else None
                try:
                    clipper.render_clip(seg, out, offset, cand["length"], "blur" if smart else ccfg["layout"],
                                        ass, ccfg["normalize_audio"], focus)
                finally:
                    if ass:
                        ass.unlink(missing_ok=True)
                caption = build_caption(
                    self.cfg, media.title, media.uploader, src.get("hashtags", []), n, len(chosen),
                    pick["caption"] if pick else None, pick["hashtags"] if pick else None,
                )
                self.db.add_clip(str(out), caption, media.source_id, hook, cand["score"], status, src["url"])
                log.info("%s clip %s (start %.0fs%s)", status, out.name, cand["start"], ", AI-picked" if pick else "")
            return len(chosen)
        finally:
            media.cleanup()

    # ---- Posting ---------------------------------------------------------
    def _drop(self, clip, status: str, error: str | None = None) -> None:
        self.db.set_status(clip["id"], status, error)
        Path(clip["path"]).unlink(missing_ok=True)

    def post_next(self, dry_run: bool = False) -> tuple[int | None, bool]:
        """Post the next clip. Returns (clip id or None, whether the slot is settled).
        An unsettled slot is tried again on the next tick."""
        pcfg = self.cfg["posting"]
        if self.db.queued_count() == 0:
            self.harvest()
        while (clip := self.db.next_clip()) is not None:
            if not Path(clip["path"]).exists():
                self.db.mark_failed(clip["id"], "clip file missing")
                continue
            if dry_run:
                log.info("[dry-run] would post %s: %s", clip["path"], clip["caption"])
                return clip["id"], True
            try:
                publish_id = self.tiktok.upload_video(clip["path"], clip["caption"])
            except TikTokAuthError as exc:
                log.error("cannot post: %s", exc)
                notify.send(self.cfg["notify"], f"TikTokBot cannot post: {exc}")
                return None, True
            except Exception as exc:
                attempts = self.db.mark_retry(
                    clip["id"], str(exc), time.time() + 60 * float(pcfg["retry_minutes"]) * (clip["attempts"] + 1)
                )
                log.error("posting clip %s failed (attempt %d/%d): %s", clip["id"], attempts, pcfg["max_attempts"], exc)
                if attempts >= int(pcfg["max_attempts"]):
                    self._drop(clip, "failed", str(exc))
                    notify.send(self.cfg["notify"], f"TikTokBot gave up on clip #{clip['id']}: {exc}")
                return None, False
            self.db.mark_posted(clip["id"], publish_id)
            Path(clip["path"]).unlink(missing_ok=True)
            log.info("posted clip %s (publish_id %s)", clip["id"], publish_id)
            if self.tiktok.mode == "draft":
                notify.send(self.cfg["notify"], f"Clip #{clip['id']} is in your TikTok inbox. Caption: {clip['caption']}")
            else:
                notify.send(self.cfg["notify"], f"Posted clip #{clip['id']}: {clip['caption'][:120]}")
            return clip["id"], True

        if self.db.queued_count():  # clips exist but are waiting for their retry time
            return None, False
        pending = self.db.count("pending")
        reason = f"{pending} clip(s) are waiting for approval" if pending else "no new source material was found"
        log.warning("nothing to post: %s", reason)
        notify.send(self.cfg["notify"], f"TikTokBot had nothing to post: {reason}")
        return None, True

    def _slots(self, day):
        for hhmm in self.cfg["schedule"]["post_times"]:
            hour, minute = map(int, hhmm.split(":"))
            yield f"{day.isoformat()} {hhmm}", datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.tz)

    def due_slots(self, now: datetime | None = None) -> list[str]:
        """Post slots that are due now (within the grace period) and not handled yet."""
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        grace = timedelta(minutes=int(self.cfg["schedule"]["grace_minutes"]))
        return [
            slot
            for day in (now.date() - timedelta(days=1), now.date())
            for slot, at in self._slots(day)
            if at <= now <= at + grace and not self.db.slot_done(slot)
        ]

    def upcoming_slots(self, count: int = 3, now: datetime | None = None) -> list[datetime]:
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        out = [at for d in range(3) for _, at in self._slots(now.date() + timedelta(days=d)) if at > now]
        return out[:count]

    def tick(self, now: datetime | None = None, dry_run: bool = False) -> int:
        """Handle every due slot. Returns the number of posts made."""
        posted = 0
        for slot in self.due_slots(now):
            clip_id, settled = self.post_next(dry_run=dry_run)
            if settled:
                self.db.mark_slot(slot, clip_id)
            posted += clip_id is not None
        return posted

    # ---- Statistics ------------------------------------------------------
    def sync_stats(self, max_age_days: float = 30) -> int:
        """Fetch view/like counts for recent posts. Returns clips updated."""
        cutoff = time.time() - max_age_days * 86400
        recent = [c for c in self.db.clips("posted") if (c["posted_at"] or 0) >= cutoff and c["publish_id"]]
        for clip in recent:
            if not clip["video_id"]:
                try:
                    if video_id := self.tiktok.video_id(clip["publish_id"]):
                        self.db.set_video_id(clip["id"], video_id)
                except Exception as exc:
                    log.debug("no video id for clip %s: %s", clip["id"], exc)
        by_video = {c["video_id"]: c["id"] for c in self.db.clips("posted") if c["video_id"] and (c["posted_at"] or 0) >= cutoff}
        stats = self.tiktok.video_stats(list(by_video)) if by_video else {}
        for video_id, st in stats.items():
            self.db.set_stats(by_video[video_id], st.get("view_count", 0), st.get("like_count", 0),
                              st.get("comment_count", 0), st.get("share_count", 0))
        return len(stats)

    def cleanup(self, max_age_hours: float = 12) -> None:
        """Remove leftovers of interrupted downloads."""
        cutoff = time.time() - max_age_hours * 3600
        for path in (self.data / "downloads").glob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)

    def run_forever(self, dry_run: bool = False) -> None:
        log.info("scheduler running - posting daily at %s (%s)",
                 ", ".join(self.cfg["schedule"]["post_times"]), self.cfg["schedule"]["timezone"])
        last_harvest = last_stats = 0.0
        while True:
            try:
                if self.cfg["stats"]["enabled"] and time.time() - last_stats > 6 * 3600:
                    last_stats = time.time()
                    try:
                        log.info("statistics updated for %d clip(s)", self.sync_stats())
                    except Exception as exc:
                        log.warning("statistics update failed: %s", exc)
                # Keep the queue stocked ahead of time so posts go out on the minute.
                if (self.db.count("queued", "pending") < self.cfg["clips"]["queue_target"]
                        and time.time() - last_harvest > 3600):
                    last_harvest = time.time()
                    self.cleanup()
                    self.harvest()
                self.tick(dry_run=dry_run)
            except Exception:
                log.exception("scheduler tick failed")
            time.sleep(30)
