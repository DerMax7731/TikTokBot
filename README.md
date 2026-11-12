# TikTokBot

Finds the best moments in YouTube videos, live streams (Twitch, YouTube Live,
Kick, ...) and TikTok videos, turns them into finished vertical clips and posts
them to your TikTok account three times a day.

## What a clip gets

- **The right moment** - audio energy is combined with YouTube's "most replayed"
  viewer data. With AI enabled, Claude reads the transcripts of the strongest
  candidates and keeps the ones that work on their own.
- **Clean cuts** - starts are moved to the nearest pause, with short fades.
- **Word-by-word subtitles** burned in, the spoken word highlighted.
- **A hook headline** on screen for the first seconds, plus a written caption
  and hashtags (AI), or a caption from your template (no AI).
- **9:16 layout** (`blur`, `crop` or `fit`) and normalised loudness.

Long videos and VODs are handled efficiently: only the audio is downloaded for
analysis, then just the chosen stretches of video.

## Setup

Requires Python 3.10+ and `ffmpeg`.

```bash
pip install -e ".[ai,subtitles]"      # both extras are optional
cp config.example.yaml config.yaml    # then edit sources, times, captions
tiktokbot doctor                      # shows what is still missing
```

| Extra | Adds | Needs |
|---|---|---|
| `ai` | AI clip selection, hooks, captions, hashtags | `ANTHROPIC_API_KEY` |
| `subtitles` | Local Whisper transcription for sources without captions (Twitch, TikTok, live) | - |

Without either, YouTube sources still get subtitles from YouTube's captions and
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

Until TikTok has audited your app, posts can only be private (`SELF_ONLY`).
After the audit, set `tiktok.privacy_level: PUBLIC_TO_EVERYONE`.

## Usage

```bash
tiktokbot run              # scheduler: posts at the configured times, keeps the queue stocked
tiktokbot status           # queue, recent posts, next post times
tiktokbot harvest          # create clips now
tiktokbot post --dry-run   # show what would be posted next
tiktokbot post             # post the next clip now
```

### Reviewing clips before they go out

Set `review.require_approval: true`. New clips then wait until you approve them:

```bash
tiktokbot review                     # lists waiting clips with file path, hook and caption
tiktokbot caption 12 "Better text"   # edit a caption
tiktokbot approve 12 13              # or: approve all
tiktokbot reject 14
```

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
