# TikTokBot

Finds highlights in YouTube videos, live streams (Twitch, YouTube Live, Kick, ...)
and TikTok videos, cuts them into vertical clips and posts them to your TikTok
account three times a day.

## How it works

1. **Harvest** - `yt-dlp` downloads the newest unseen videos from your configured
   sources (live streams are recorded for a set time).
2. **Clip** - the loudest, most eventful stretches of the audio are picked as
   highlights and cut with `ffmpeg` into 1080x1920 clips (blurred background fill).
3. **Post** - clips are queued and uploaded through TikTok's official
   Content Posting API at the times in `schedule.post_times`.

## Setup

Requires Python 3.10+ and `ffmpeg`.

```bash
pip install -e .
cp config.example.yaml config.yaml    # then edit sources, times, captions
```

### TikTok API access

1. Create an app at <https://developers.tiktok.com>, add the **Login Kit** and
   **Content Posting API** products and enable **Direct Post** (`video.publish` scope).
2. Put the client key, client secret and your redirect URI into `config.yaml`
   (or the `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` environment variables).
3. Connect your account:

   ```bash
   tiktokbot auth                         # prints a URL - open it and approve
   tiktokbot auth --code "<redirected url>"
   ```

Until TikTok has audited your app, posts can only be private (`SELF_ONLY`).
After the audit, set `tiktok.privacy_level: PUBLIC_TO_EVERYONE`.

## Usage

```bash
tiktokbot harvest          # download sources and queue clips
tiktokbot post --dry-run   # show what would be posted next
tiktokbot post             # post the next clip now
tiktokbot status           # queue and recent posts
tiktokbot run              # scheduler: posts at the configured times, keeps the queue stocked
```

Keep `tiktokbot run` alive with systemd/tmux/Docker, or use cron instead:

```cron
*/5 * * * * cd /path/to/TikTokBot && tiktokbot run --once
```

A slot missed while the bot was down is still posted within
`schedule.grace_minutes`; each slot is posted at most once.

## Content rights

Only add sources you own or have permission to re-use (your own channels,
creators whose clipping policy allows it, licensed material). Re-uploading other
people's videos without permission violates copyright and TikTok's terms and
gets accounts struck or banned. Captions credit the original uploader by default.

## Tests

```bash
pip install pytest && pytest
```
