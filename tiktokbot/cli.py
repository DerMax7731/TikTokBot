"""Clip highlights from YouTube, live streams and TikTok and post them to TikTok on a schedule."""
from __future__ import annotations

import argparse
import logging
import shutil
import sys

from . import ai, transcript, vision
from .config import ConfigError, load_config
from . import web
from .accounts import Accounts, Runner
from .pipeline import Bot
from .tiktok_api import TikTokError


def _doctor(bot: Bot) -> int:
    cfg = bot.cfg
    checks = [
        ("ffmpeg installed", bool(shutil.which("ffmpeg") and shutil.which("ffprobe")), "install ffmpeg"),
        ("sources configured", bool(bot.all_sources()), "add a source in the dashboard or under sources"),
        ("TikTok app credentials", bool(cfg["tiktok"]["client_key"] and cfg["tiktok"]["client_secret"]),
         "set tiktok.client_key / client_secret"),
        ("TikTok account connected", bot.tiktok.token_path.exists(), "run `tiktokbot auth`"),
    ]
    optional = [
        ("AI clip picking and captions", ai.available(cfg["ai"]),
         "pip install 'tiktokbot[ai]' and set ANTHROPIC_API_KEY"),
        ("Whisper subtitles for non-YouTube sources", transcript.whisper_available(), "pip install 'tiktokbot[subtitles]'"),
        ("face-aware cropping (layout: smart)", vision.available(), "pip install 'tiktokbot[vision]'"),
        ("notifications", bool(cfg["notify"]["webhook_url"]), "set notify.webhook_url"),
    ]
    ok = True
    for name, passed, fix in checks:
        ok &= passed
        print(f"[{'ok' if passed else 'MISSING'}] {name}" + ("" if passed else f" - {fix}"))
    for name, passed, fix in optional:
        print(f"[{'ok' if passed else 'off'}] {name}" + ("" if passed else f" (optional) - {fix}"))
    if cfg["tiktok"]["privacy_level"] == "SELF_ONLY":
        print("[note] privacy_level is SELF_ONLY: posts are only visible to you")
    return 0 if ok else 1


def _status(bot: Bot) -> None:
    rows = bot.db.clips()
    for status in ("pending", "queued", "posted", "failed", "rejected"):
        n = sum(r["status"] == status for r in rows)
        if n or status in ("queued", "posted"):
            print(f"{status}: {n}")
    print("next posts: " + ", ".join(t.strftime("%a %H:%M") for t in bot.upcoming_slots()))
    for r in rows[-10:]:
        print(f"  #{r['id']} [{r['status']}] {r['caption'][:70]}" + (f" - {r['error']}" if r["error"] else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tiktokbot", description=__doc__)
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("auth", help="connect your TikTok account")
    p.add_argument("--code", help="redirect URL (or code) you were sent to after approving")
    p = sub.add_parser("harvest", help="download sources and create new clips")
    p.add_argument("--force", action="store_true", help="harvest even if the queue is full")
    p = sub.add_parser("post", help="post the next queued clip right now")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("run", help="run the scheduler (posts at the configured times)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--once", action="store_true", help="handle due slots and exit (for cron)")
    p.add_argument("--web", action="store_true", help="also serve the dashboard")
    p = sub.add_parser("web", help="open the dashboard to review clips in the browser")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    sub.add_parser("stats", help="fetch and show how posted clips performed")
    sub.add_parser("status", help="show the queue, recent posts and next post times")
    sub.add_parser("review", help="list clips waiting for approval")
    p = sub.add_parser("approve", help="approve clips for posting")
    p.add_argument("ids", nargs="+", help="clip ids, or 'all'")
    p = sub.add_parser("reject", help="discard clips")
    p.add_argument("ids", nargs="+", type=int)
    p = sub.add_parser("caption", help="change a clip's caption")
    p.add_argument("id", type=int)
    p.add_argument("text")
    sub.add_parser("doctor", help="check the setup")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        cfg = load_config(args.config)
        cfg["tiktok"].update(Accounts(cfg).app_settings())
        bot = Bot(cfg)
        if args.cmd == "auth":
            if not args.code:
                url = bot.tiktok.authorize_url()
                print(f"1. Open this URL and approve access:\n\n   {url}\n")
                print("2. You are redirected to your redirect_uri. Copy that full URL and run:")
                print('   tiktokbot auth --code "<redirected url>"')
            else:
                bot.tiktok.exchange_code(args.code)
                print("TikTok account connected.")
        elif args.cmd == "harvest":
            print(f"{bot.harvest(force=args.force)} clip(s) created")
        elif args.cmd == "post":
            return 0 if bot.post_next(dry_run=args.dry_run)[0] is not None else 1
        elif args.cmd == "run":
            if args.once:
                Runner(bot.cfg).step(dry_run=args.dry_run)
            else:
                if args.web:
                    web.serve_in_background(bot.cfg)
                Runner(bot.cfg).run_forever(dry_run=args.dry_run)
        elif args.cmd == "web":
            server = web.make_server(bot.cfg, args.host, args.port)
            print("dashboard on http://%s:%s (Ctrl+C to stop)" % server.server_address[:2])
            server.serve_forever()
        elif args.cmd == "stats":
            if not bot.cfg["stats"]["enabled"]:
                print("error: set stats.enabled: true and run `tiktokbot auth` again (adds the video.list permission)",
                      file=sys.stderr)
                return 2
            print(f"updated {bot.sync_stats()} clip(s)")
            rows = [r for r in bot.db.clips("posted") if r["views"] is not None]
            for r in sorted(rows, key=lambda r: -r["views"])[:10]:
                print(f"  {r['views']:>8} views  {r['likes']:>6} likes  #{r['id']} {r['caption'][:60]}")
            for url, avg in sorted(bot.db.source_performance().items(), key=lambda kv: -kv[1]):
                print(f"  avg {avg:>8.0f} views/clip  {url}")
        elif args.cmd == "status":
            _status(bot)
        elif args.cmd == "review":
            rows = bot.db.clips("pending")
            for r in rows:
                print(f"#{r['id']}  {r['path']}\n    hook: {r['hook'] or '-'}\n    caption: {r['caption']}")
            print(f"{len(rows)} clip(s) waiting. Approve with `tiktokbot approve <id ...|all>`.")
        elif args.cmd == "approve":
            ids = [r["id"] for r in bot.db.clips("pending")] if args.ids == ["all"] else [int(i) for i in args.ids]
            done = [i for i in ids if (row := bot.db.get(i)) and row["status"] == "pending"]
            for i in done:
                bot.db.set_status(i, "queued")
            print(f"approved {len(done)} clip(s)")
        elif args.cmd == "reject":
            for i in args.ids:
                row = bot.db.get(i)
                if row and row["status"] in ("pending", "queued"):
                    bot._drop(row, "rejected")
                    print(f"rejected #{i}")
                else:
                    print(f"#{i} is not pending or queued", file=sys.stderr)
        elif args.cmd == "caption":
            if not bot.db.get(args.id):
                print(f"error: no clip #{args.id}", file=sys.stderr)
                return 2
            bot.db.set_caption(args.id, args.text)
            print(f"caption of #{args.id} updated")
        elif args.cmd == "doctor":
            return _doctor(bot)
    except (ConfigError, TikTokError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
