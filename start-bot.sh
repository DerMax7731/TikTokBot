#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is missing. Install it (macOS: brew install ffmpeg, Debian/Ubuntu: sudo apt install ffmpeg) and start again."
  exit 1
fi

if [ ! -x .venv/bin/tiktokbot ]; then
  echo "First start: installing the bot. This takes a few minutes."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -e ".[subtitles,vision]"
fi

echo "Dashboard: http://localhost:8080"
( sleep 2; command -v open >/dev/null 2>&1 && open http://localhost:8080 || command -v xdg-open >/dev/null 2>&1 && xdg-open http://localhost:8080 ) >/dev/null 2>&1 &
exec .venv/bin/tiktokbot run --web
