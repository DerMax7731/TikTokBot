# TikTokBot

Finds the best moments in YouTube videos, live streams (Twitch, YouTube Live,
Kick, ...) and TikTok videos, turns them into finished vertical clips and posts
them to your TikTok account three times a day.

## What a clip gets

- **The right moment** - audio energy is combined with YouTube's "most replayed"
  viewer data. With AI enabled, Claude reads the transcripts of the strongest
  candidates and keeps the ones that work on their own.
- **Complete thoughts** - with a transcript, clips open at the start of a
  sentence and run until one ends; otherwise cuts move to the nearest pause.
- **Word-by-word subtitles** burned in, the spoken word highlighted.
- **A hook headline** on screen for the first seconds, plus a written caption
  and hashtags (AI), or a caption from your template (no AI).
- **9:16 layout** - `smart` fills the screen centred on the speaker's face;
  also `blur`, `crop` and `fit`. Loudness is normalised.

Long videos and VODs are handled efficiently: only the audio is downloaded for
analysis, then just the chosen stretches of video.

## Setup

Requires Python 3.10+ and `ffmpeg`.

```bash
pip install -e ".[ai,subtitles,vision]"   # all extras are optional
cp config.example.yaml config.yaml    # then edit sources, times, captions
tiktokbot doctor                      # shows what is still missing
```

| Extra | Adds | Needs |
|---|---|---|
| `ai` | AI clip selection, hooks, captions, hashtags | `ANTHROPIC_API_KEY` |
| `subtitles` | Local Whisper transcription for sources without captions (Twitch, TikTok, live) | - |
| `vision` | Face-aware cropping for `layout: smart` | - |

Without any of them, YouTube sources still get subtitles from YouTube's captions and
clips are chosen by audio and replay data.

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

Until TikTok has audited your app, direct posts can only be private (`SELF_ONLY`).
Two ways forward:

- pass TikTok's audit, then set `tiktok.privacy_level: PUBLIC_TO_EVERYONE`, or
- set `tiktok.mode: draft`: clips are sent to your TikTok inbox at the scheduled
  times and you publish them in the app with one tap (the caption is sent to
  your notification webhook and shown in the dashboard to paste).

## Usage

```bash
tiktokbot run              # scheduler: posts at the configured times, keeps the queue stocked
tiktokbot status           # queue, recent posts, next post times
tiktokbot harvest          # create clips now
tiktokbot post --dry-run   # show what would be posted next
tiktokbot post             # post the next clip now
```

### Dashboard

```bash
tiktokbot web              # http://127.0.0.1:8080
tiktokbot run --web        # scheduler and dashboard together
```

Watch each clip, edit its caption, approve or reject it, and see the upcoming
post times, failures and results. To reach it from your phone set
`web.host: "0.0.0.0"` and a `web.token`, then open `http://<machine>:8080/?token=<token>`.

### Reviewing clips before they go out

Set `review.require_approval: true`. New clips then wait until you approve them
in the dashboard or on the command line:

```bash
tiktokbot review                     # lists waiting clips with file path, hook and caption
tiktokbot caption 12 "Better text"   # edit a caption
tiktokbot approve 12 13              # or: approve all
tiktokbot reject 14
```

### Learning what works

Set `stats.enabled: true` (and run `tiktokbot auth` again). The bot then records
views, likes, comments and shares of its posts; `tiktokbot stats` shows the best
clips and the average per source, and sources whose clips perform best are
harvested first.

### Keeping it running

- **Docker:** `docker build -t tiktokbot .` then see the `docker run` line in the `Dockerfile`.
- **systemd:** `deploy/tiktokbot.service`.
- **cron:** `*/5 * * * * cd /path/to/TikTokBot && tiktokbot run --once`

A slot missed while the bot was down is still posted within
`schedule.grace_minutes`; each slot is posted at most once. Failed uploads are
retried (`posting.max_attempts`). Set `notify.webhook_url` to a Discord or Slack
webhook to hear about posts, failures and an empty queue.

## Content rights

Only add sources you own or have permission to re-use (your own channels,
creators whose clipping policy allows it, licensed material). Re-uploading other
people's videos without permission violates copyright and TikTok's terms and
gets accounts struck or banned. Captions credit the original uploader by default.

## Tests

```bash
pip install -e ".[dev]" && pytest
```
