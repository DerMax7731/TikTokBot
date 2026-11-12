import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest
import yaml

from tiktokbot import ai, clipper, highlights, sources, transcript
from tiktokbot.cli import main
from tiktokbot.config import ConfigError, load_config
from tiktokbot.pipeline import Bot, build_caption
from tiktokbot.tiktok_api import TikTokAuthError, TikTokClient, plan_chunks
from tiktokbot.transcript import Word

UTC = ZoneInfo("UTC")


def write_cfg(tmp_path, **over):
    cfg = {"sources": [{"url": "https://example.com/v", "hashtags": ["fyp"]}],
           "clips": {"length_seconds": 5, "per_source": 2, "min_gap_seconds": 2},
           "posting": {"max_attempts": 2, "retry_minutes": 0}}
    for key, value in over.items():
        cfg[key] = {**cfg.get(key, {}), **value} if isinstance(value, dict) else value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


@pytest.fixture(autouse=True)
def no_ai(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(transcript, "whisper_available", lambda: False)


@pytest.fixture(scope="session")
def video(tmp_path_factory):
    """40s landscape test video that is quiet except for a loud burst at 20-26s."""
    path = tmp_path_factory.mktemp("src") / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15:duration=40",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=40",
         "-af", "volume='if(between(t,20,26),1,0.01)':eval=frame",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)],
        check=True,
    )
    return path


def fake_source(monkeypatch, video, words=None, heatmap=None):
    monkeypatch.setattr(sources, "list_candidates", lambda src: [{"id": "X:1", "url": src["url"], "live": False}])

    def fetch(entry, src, workdir, long_video_minutes=20):
        workdir.mkdir(parents=True, exist_ok=True)
        copy = workdir / "v.mp4"
        copy.write_bytes(video.read_bytes())
        return sources.Fetched(entry["id"], entry["url"], "Title", "Up", 40.0, copy, workdir, True, heatmap, words)

    monkeypatch.setattr(sources, "fetch", fetch)


def probe(path, entries, stream="v:0"):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", entries, "-of", "csv=p=0", str(path)],
        capture_output=True, text=True).stdout.strip()


# ---- config --------------------------------------------------------------
def test_config_defaults_and_validation(tmp_path):
    cfg = load_config(write_cfg(tmp_path))
    assert len(cfg["schedule"]["post_times"]) == 3
    assert cfg["clips"]["layout"] == "blur"
    assert load_config(write_cfg(tmp_path, clips={"vertical": False}))["clips"]["layout"] == "fit"
    with pytest.raises(ConfigError):
        load_config(write_cfg(tmp_path, schedule={"post_times": ["25:00"]}))
    with pytest.raises(ConfigError):
        load_config(write_cfg(tmp_path, clips={"layout": "sideways"}))


# ---- highlight scoring ---------------------------------------------------
def test_pick_windows_ranks_and_does_not_overlap():
    energy = np.full(300, 0.05, dtype=np.float32)
    energy[100:110] = 1.0
    energy[200:210] = 0.8
    picks = highlights.pick_windows(highlights.build_score(energy), 300, 20, 2, min_gap=10)
    (s1, sc1), (s2, sc2) = picks
    assert s1 <= 100 and s1 + 20 >= 110 and s2 <= 200 and s2 + 20 >= 210   # best first
    assert sc1 > sc2 and abs(s1 - s2) >= 30


def test_heatmap_outweighs_audio():
    energy = np.full(300, 0.05, dtype=np.float32)
    energy[100:110] = 1.0
    heat = [{"start_time": 0, "end_time": 10, "value": 1.0},      # intro bias is damped
            {"start_time": 240, "end_time": 260, "value": 0.9}]
    start, _ = highlights.pick_windows(highlights.build_score(energy, heat), 300, 20, 1)[0]
    assert 235 <= start <= 245


def test_pick_windows_short_and_silent():
    assert highlights.pick_windows(np.zeros(0), 10, 30, 3) == [(0.0, 0.0)]
    starts = [s for s, _ in highlights.pick_windows(np.zeros(0), 100, 10, 3)]
    assert len(starts) == 3 and starts == sorted(starts)


def test_snap_to_quiet():
    energy = np.ones(60, dtype=np.float32)
    energy[28] = 0.01
    assert highlights.snap_to_quiet(energy, 30) == 28.0
    assert highlights.snap_to_quiet(np.zeros(0), 30) == 30


# ---- transcripts and subtitles -------------------------------------------
def test_youtube_json3_parsing():
    data = {"events": [
        {"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "hello"}, {"utf8": " big world", "tOffsetMs": 500}]},
        {"tStartMs": 2000, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 5000, "dDurationMs": 2000, "segs": [{"utf8": "manual caption line"}]},
    ]}
    words = transcript.from_youtube_json3(data)
    assert [w.text for w in words] == ["hello", "big", "world", "manual", "caption", "line"]
    assert words[0].start == 1.0 and words[0].end == 1.5 and words[1].start == 1.5
    assert words[3].start == 5.0 and round(words[5].end, 2) == 7.0
    cut = transcript.slice_words(words, 1.4, 2.0)
    assert [w.text for w in cut] == ["big", "world"] and round(cut[0].start, 2) == 0.1


def test_ass_generation(tmp_path):
    words = [Word(0.0, 0.4, "this"), Word(0.4, 0.8, "is"), Word(0.8, 1.2, "{wild}"), Word(3.0, 3.5, "right")]
    ass = transcript.build_ass(words, tmp_path / "c.ass", hook="You won't believe this", length=6).read_text()
    assert "Style: Cap" in ass and "Style: Hook" in ass
    assert "You won't believe this" in ass
    dialog = [line for line in ass.splitlines() if line.startswith("Dialogue: 0")]
    assert len(dialog) == 4                                   # one event per spoken word
    assert "{\\c&H0000F0FF}THIS{\\c&H00FFFFFF} IS (WILD)" in dialog[0]   # active word highlighted, braces escaped
    assert dialog[3].endswith("RIGHT{\\c&H00FFFFFF}")          # pause starts a new chunk


# ---- rendering -----------------------------------------------------------
@pytest.mark.parametrize("layout", ["blur", "crop", "fit"])
def test_render_layouts_with_subtitles(video, tmp_path, layout):
    ass = transcript.build_ass([Word(0.5, 1.5, "hello"), Word(1.5, 2.5, "world")], tmp_path / "s.ass", "Hook", 4)
    out = clipper.render_clip(video, tmp_path / "clip.mp4", 20, 4, layout, ass)
    assert probe(out, "stream=width,height") == "1080,1920"
    assert probe(out, "stream=codec_name", "a:0") == "aac"
    assert 3.7 <= highlights.probe_duration(out) <= 4.4


def test_subtitles_are_actually_burned_in(video, tmp_path):
    ass = transcript.build_ass([Word(0.0, 3.0, "hello")], tmp_path / "s.ass", None, 3)
    plain = clipper.render_clip(video, tmp_path / "plain.mp4", 5, 3, "fit")
    subbed = clipper.render_clip(video, tmp_path / "sub.mp4", 5, 3, "fit", ass)

    def frame(path):
        return subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", "1.5", "-i", str(path), "-frames:v", "1",
                               "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout

    a = np.frombuffer(frame(plain), np.uint8).astype(int)
    b = np.frombuffer(frame(subbed), np.uint8).astype(int)
    assert (np.abs(a - b) > 60).sum() > 2000   # the caption changed a visible patch of the frame


def test_render_without_audio_track(tmp_path):
    src = tmp_path / "mute.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=6",
                    "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
    assert highlights.audio_energy(src).size == 0
    out = clipper.render_clip(src, tmp_path / "o.mp4", 1, 3)
    assert probe(out, "stream=width,height") == "1080,1920"


# ---- captions ------------------------------------------------------------
def test_caption(tmp_path):
    cfg = load_config(write_cfg(tmp_path))
    assert build_caption(cfg, " Big play ", "Streamer", ["fyp", "#clips"]) == "Big play #fyp #clips (via Streamer)"
    text = build_caption(cfg, "T", "Up", ["fyp"], ai_caption="He did it.", ai_hashtags=["FYP", "clutch win"])
    assert text == "He did it. #fyp #clutchwin (via Up)"


# ---- pipeline ------------------------------------------------------------
def test_harvest_and_schedule(video, tmp_path, monkeypatch):
    bot = Bot(load_config(write_cfg(tmp_path)))
    fake_source(monkeypatch, video)
    assert bot.harvest() == 2
    assert bot.harvest() == 0  # already seen
    assert bot.db.queued_count() == 2
    assert not list((bot.data / "downloads").glob("*"))  # source removed after clipping

    uploads = []
    monkeypatch.setattr(TikTokClient, "upload_video", lambda self, path, caption: uploads.append(caption) or "pid")
    assert bot.tick(datetime(2026, 1, 1, 8, 0, tzinfo=UTC)) == 0        # before first slot
    assert bot.tick(datetime(2026, 1, 1, 9, 1, tzinfo=UTC)) == 1        # 09:00 slot
    assert bot.tick(datetime(2026, 1, 1, 9, 2, tzinfo=UTC)) == 0        # not posted twice
    assert bot.tick(datetime(2026, 1, 1, 15, 0, tzinfo=UTC)) == 1       # 15:00 slot
    assert len(uploads) == 2 and bot.db.queued_count() == 0
    assert len(bot.db.clips("posted")) == 2
    nxt = bot.upcoming_slots(3, datetime(2026, 1, 1, 15, 0, tzinfo=UTC))
    assert [t.strftime("%d %H:%M") for t in nxt] == ["01 20:00", "02 09:00", "02 15:00"]


def test_ai_picks_clips_and_writes_copy(video, tmp_path, monkeypatch):
    words = [Word(float(t), t + 0.5, f"w{t}") for t in range(40)]
    fake_source(monkeypatch, video, words=words)
    monkeypatch.setattr(ai, "available", lambda cfg: True)
    seen = {}

    def pick(cfg, title, uploader, cands, count):
        seen["cands"] = cands
        return [{"index": cands[-1]["index"], "hook": "Wait for it", "caption": "The moment.", "hashtags": ["wow"]}]

    monkeypatch.setattr(ai, "pick_clips", pick)
    captured = {}
    real = transcript.build_ass
    monkeypatch.setattr(transcript, "build_ass",
                        lambda words, path, hook, length, font: captured.update(words=words, hook=hook) or real(words, path, hook, length, font))
    bot = Bot(load_config(write_cfg(tmp_path, clips={"per_source": 1})))
    assert bot.harvest() == 1
    assert len(seen["cands"]) == 3 and all(c["transcript"] for c in seen["cands"])   # 3x candidates offered
    clip = bot.db.clips("queued")[0]
    assert clip["hook"] == "Wait for it" and clip["caption"] == "The moment. #fyp #wow (via Up)"
    assert captured["hook"] == "Wait for it" and len(captured["words"]) >= 4
    assert not list((bot.data / "clips").glob("*.ass"))


def test_ai_failure_falls_back(video, tmp_path, monkeypatch):
    fake_source(monkeypatch, video, words=[Word(float(t), t + 0.5, "w") for t in range(40)])
    monkeypatch.setattr(ai, "available", lambda cfg: True)
    monkeypatch.setattr(ai, "pick_clips", lambda *a: None)
    bot = Bot(load_config(write_cfg(tmp_path)))
    assert bot.harvest() == 2
    assert bot.db.clips("queued")[0]["caption"] == "Title #fyp (via Up)"


def test_retry_then_give_up(video, tmp_path, monkeypatch):
    bot = Bot(load_config(write_cfg(tmp_path, clips={"per_source": 1})))
    fake_source(monkeypatch, video)
    bot.harvest()

    def boom(self, path, caption):
        raise RuntimeError("upload exploded")

    monkeypatch.setattr(TikTokClient, "upload_video", boom)
    now = datetime(2026, 1, 1, 9, 1, tzinfo=UTC)
    assert bot.tick(now) == 0 and not bot.db.slot_done("2026-01-01 09:00")   # slot stays open for a retry
    assert bot.db.clips("queued")[0]["attempts"] == 1
    assert bot.tick(now) == 0
    failed = bot.db.clips("failed")
    assert len(failed) == 1 and "exploded" in failed[0]["error"]
    assert bot.tick(now) == 0 and bot.db.slot_done("2026-01-01 09:00")       # nothing left: slot settled


def test_auth_error_does_not_burn_the_clip(video, tmp_path, monkeypatch):
    bot = Bot(load_config(write_cfg(tmp_path, clips={"per_source": 1})))
    fake_source(monkeypatch, video)
    bot.harvest()
    assert bot.post_next() == (None, True)          # no token yet -> TikTokAuthError
    clip = bot.db.clips("queued")[0]
    assert clip["attempts"] == 0


def test_approval_flow(video, tmp_path, monkeypatch, capsys):
    cfg_path = write_cfg(tmp_path, review={"require_approval": True})
    fake_source(monkeypatch, video)
    monkeypatch.setattr(TikTokClient, "upload_video", lambda self, path, caption: "pid")
    c = ["-c", str(cfg_path)]
    assert main(c + ["harvest"]) == 0
    bot = Bot(load_config(cfg_path))
    assert bot.db.count("pending") == 2 and bot.db.queued_count() == 0
    assert main(c + ["post"]) == 1                  # nothing approved yet
    ids = [r["id"] for r in bot.db.clips("pending")]
    assert main(c + ["caption", str(ids[0]), "My caption"]) == 0
    assert main(c + ["approve", str(ids[0])]) == 0
    assert main(c + ["reject", str(ids[1])]) == 0
    assert main(c + ["post"]) == 0
    assert bot.db.get(ids[0])["status"] == "posted" and bot.db.get(ids[0])["caption"] == "My caption"
    assert bot.db.get(ids[1])["status"] == "rejected"
    assert main(c + ["status"]) == 0 and main(c + ["review"]) == 0
    assert "next posts:" in capsys.readouterr().out
    assert main(c + ["doctor"]) == 1                # TikTok credentials missing


def test_db_migrates_old_schema(tmp_path):
    import sqlite3
    from tiktokbot.db import DB
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE clips (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, caption TEXT NOT NULL,"
                 " source_id TEXT, status TEXT NOT NULL DEFAULT 'queued', publish_id TEXT, error TEXT, created_at REAL, posted_at REAL)")
    conn.execute("INSERT INTO clips (path, caption) VALUES ('a.mp4', 'old')")
    conn.commit(); conn.close()
    db = DB(path)
    clip = db.next_clip(time.time())
    assert clip["caption"] == "old" and clip["attempts"] == 0


# ---- TikTok API ----------------------------------------------------------
def test_plan_chunks():
    assert plan_chunks(1_000_000) == (1_000_000, 1)
    assert plan_chunks(25 * 1024 * 1024) == (10 * 1024 * 1024, 2)


def test_upload_flow(tmp_path, monkeypatch):
    client = TikTokClient({"client_key": "k", "client_secret": "s", "redirect_uri": "https://x/cb",
                           "privacy_level": "PUBLIC_TO_EVERYONE"}, tmp_path / "tok.json")
    with pytest.raises(TikTokAuthError):
        client.access_token()
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
