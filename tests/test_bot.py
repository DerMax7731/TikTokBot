import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest
import yaml

from tiktokbot import clipper, highlights, sources
from tiktokbot.config import ConfigError, load_config
from tiktokbot.pipeline import Bot, build_caption
from tiktokbot.tiktok_api import TikTokClient, plan_chunks


def write_cfg(tmp_path, **over):
    cfg = {"sources": [{"url": "https://example.com/v", "hashtags": ["fyp"]}],
           "clips": {"length_seconds": 5, "per_source": 2, "min_gap_seconds": 2}}
    cfg.update(over)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


@pytest.fixture
def video(tmp_path):
    """40s landscape test video that is silent except for a loud burst at 20-26s."""
    path = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15:duration=40",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=40",
         "-af", "volume='if(between(t,20,26),1,0.01)':eval=frame",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)],
        check=True,
    )
    return path


def test_config_defaults_and_validation(tmp_path):
    cfg = load_config(write_cfg(tmp_path))
    assert len(cfg["schedule"]["post_times"]) == 3
    with pytest.raises(ConfigError):
        load_config(write_cfg(tmp_path, schedule={"post_times": ["25:00"]}))


def test_pick_windows_finds_loud_part_without_overlap():
    energy = np.full(300, 0.05, dtype=np.float32)
    energy[100:110] = 1.0
    energy[200:210] = 0.8
    starts = pick_windows = highlights.pick_windows(energy, 300, 20, 2, min_gap=10)
    assert len(starts) == 2
    assert starts[0] <= 100 and starts[0] + 20 >= 110
    assert starts[1] <= 200 and starts[1] + 20 >= 210
    assert pick_windows[1] - pick_windows[0] >= 30


def test_pick_windows_short_and_silent():
    assert highlights.pick_windows(np.zeros(0), 10, 30, 3) == [0.0]
    starts = highlights.pick_windows(np.zeros(0), 100, 10, 3)
    assert len(starts) == 3 and starts == sorted(starts)


def test_plan_chunks():
    assert plan_chunks(1_000_000) == (1_000_000, 1)
    assert plan_chunks(25 * 1024 * 1024) == (10 * 1024 * 1024, 2)


def test_audio_energy_and_vertical_cut(video, tmp_path):
    energy = highlights.audio_energy(video)
    assert 38 <= len(energy) <= 41
    start = highlights.pick_windows(energy, highlights.probe_duration(video), 6, 1)[0]
    assert 18 <= start <= 22
    out = clipper.cut_clip(video, tmp_path / "clip.mp4", start, 6)
    dims = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.strip()
    assert dims == "1080,1920"
    assert 5.5 <= highlights.probe_duration(out) <= 6.6


def test_caption(tmp_path):
    cfg = load_config(write_cfg(tmp_path))
    assert build_caption(cfg, " Big play ", "Streamer", ["fyp", "#clips"], 1, 2) == "Big play #fyp #clips (via Streamer)"


def test_harvest_and_schedule(video, tmp_path, monkeypatch):
    bot = Bot(load_config(write_cfg(tmp_path)))
    monkeypatch.setattr(sources, "list_candidates", lambda src: [{"id": "X:1", "url": src["url"], "live": False}])

    def fake_fetch(entry, src, workdir):
        workdir.mkdir(parents=True, exist_ok=True)
        copy = workdir / "v.mp4"
        copy.write_bytes(video.read_bytes())
        return sources.Media(entry["id"], copy, "Title", "Up", entry["url"])

    monkeypatch.setattr(sources, "fetch", fake_fetch)
    assert bot.harvest() == 2
    assert bot.harvest() == 0  # already seen
    assert bot.db.queued_count() == 2

    uploads = []
    monkeypatch.setattr(TikTokClient, "upload_video", lambda self, path, caption: uploads.append(caption) or "pid")

    tz = ZoneInfo("UTC")
    assert bot.tick(datetime(2026, 1, 1, 8, 0, tzinfo=tz)) == 0        # before first slot
    assert bot.tick(datetime(2026, 1, 1, 9, 1, tzinfo=tz)) == 1        # 09:00 slot
    assert bot.tick(datetime(2026, 1, 1, 9, 2, tzinfo=tz)) == 0        # not posted twice
    assert bot.tick(datetime(2026, 1, 1, 15, 0, tzinfo=tz)) == 1       # 15:00 slot
    assert len(uploads) == 2 and bot.db.queued_count() == 0
    assert len(bot.db.clips("posted")) == 2
    # Three slots a day in total.
    assert len(Bot(load_config(write_cfg(tmp_path))).cfg["schedule"]["post_times"]) == 3


def test_upload_flow(tmp_path, monkeypatch):
    client = TikTokClient({"client_key": "k", "client_secret": "s", "redirect_uri": "https://x/cb",
                           "privacy_level": "PUBLIC_TO_EVERYONE"}, tmp_path / "tok.json")
    monkeypatch.setattr(client, "access_token", lambda: "tok")
    calls, puts = [], []

    def fake_post(endpoint, body=None):
        calls.append((endpoint, body))
        if "creator_info" in endpoint:
            return {"privacy_level_options": ["SELF_ONLY"]}
        if "init" in endpoint:
            return {"publish_id": "p1", "upload_url": "https://upload"}
        return {"status": "PUBLISH_COMPLETE"}

    class Resp:
        status_code = 201

    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(client.session, "put", lambda url, data, headers, timeout: puts.append(headers) or Resp())
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x" * 1000)
    assert client.upload_video(f, "cap") == "p1"
    init = calls[1][1]
    assert init["post_info"]["privacy_level"] == "SELF_ONLY"  # fell back to an allowed level
    assert init["source_info"] == {"source": "FILE_UPLOAD", "video_size": 1000, "chunk_size": 1000, "total_chunk_count": 1}
    assert puts[0]["Content-Range"] == "bytes 0-999/1000"
