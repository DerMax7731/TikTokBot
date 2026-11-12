"""Command line interface."""
from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, load_config
from .pipeline import Bot
from .tiktok_api import TikTokError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tiktokbot", description=__doc__)
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("auth", help="connect your TikTok account")
    p.add_argument("--code", help="redirect URL (or code) you were sent to after approving")
    p = sub.add_parser("harvest", help="download sources and queue new clips")
    p.add_argument("--force", action="store_true", help="harvest even if the queue is full")
    p = sub.add_parser("post", help="post the next queued clip right now")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("run", help="run the scheduler (posts at the configured times)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--once", action="store_true", help="handle due slots and exit (for cron)")
    sub.add_parser("status", help="show the queue and recent posts")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        bot = Bot(load_config(args.config))
        if args.cmd == "auth":
            if not args.code:
                print("1. Open this URL and approve access:\n")
                print("   " + bot.tiktok.authorize_url())
                print("\n2. You are redirected to your redirect_uri. Copy that full URL and run:")
                print('   tiktokbot auth --code "<redirected url>"')
            else:
                bot.tiktok.exchange_code(args.code)
                print("TikTok account connected.")
        elif args.cmd == "harvest":
            print(f"{bot.harvest(force=args.force)} clip(s) created")
        elif args.cmd == "post":
            return 0 if bot.post_next(dry_run=args.dry_run) is not None else 1
        elif args.cmd == "run":
            if args.once:
                bot.tick(dry_run=args.dry_run)
            else:
                bot.run_forever(dry_run=args.dry_run)
        elif args.cmd == "status":
            rows = bot.db.clips()
            for status in ("queued", "posted", "failed"):
                print(f"{status}: {sum(r['status'] == status for r in rows)}")
            for r in rows[-10:]:
                print(f"  #{r['id']} [{r['status']}] {r['caption'][:70]}" + (f" - {r['error']}" if r["error"] else ""))
    except (ConfigError, TikTokError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
