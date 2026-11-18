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


# ---- sentence-aware cuts -------------------------------------------------
def test_refine_bounds_moves_to_sentence_edges():
    #  "...end of thought." | pause | "new idea starts here and goes on. | next one"
    words = [Word(8.0, 8.4, "before"), Word(8.5, 9.0, "thought."), Word(10.2, 10.6, "new"), Word(10.6, 11.0, "idea"),
             Word(11.0, 20.0, "continues"), Word(20.0, 20.5, "on."), Word(21.5, 22.0, "next"), Word(22.0, 30.0, "one")]
    start, end = transcript.refine_bounds(words, 9.0, 19.0)
    assert round(start, 2) == 10.05 and round(end, 2) == 20.75   # opens on "new", closes after "on."
    assert transcript.refine_bounds([], 5.0, 20.0) == (5.0, 20.0)
    assert transcript.refine_bounds(words[:2], 5.0, 20.0) == (5.0, 20.0)   # no boundary nearby: unchanged


def test_harvest_uses_sentence_bounds(video, tmp_path, monkeypatch):
    words = [Word(18.0, 19.0, "lead"), Word(19.0, 19.6, "in."), Word(21.4, 22.0, "boom"),
             Word(22.0, 26.0, "happens"), Word(26.0, 27.2, "here."), Word(29.0, 30.0, "after")]
    fake_source(monkeypatch, video, words=words)
    spans = []
    real = clipper.render_clip
    monkeypatch.setattr(clipper, "render_clip", lambda seg, out, offset, length, *a, **k: spans.append((offset, length)) or real(seg, out, offset, length, *a, **k))
    bot = Bot(load_config(write_cfg(tmp_path, clips={"per_source": 1, "length_seconds": 6})))
    assert bot.harvest() == 1
    offset, length = spans[0]
    assert round(offset, 2) == 21.25 and round(offset + length, 2) == 27.45   # "boom happens here."


# ---- face-centred crop ---------------------------------------------------
def test_focus_crop_follows_position(tmp_path):
    from tiktokbot import vision
    src = tmp_path / "halves.mp4"   # left half red, right half blue
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=red:size=640x360:rate=10:duration=3",
                    "-f", "lavfi", "-i", "color=blue:size=640x360:rate=10:duration=3", "-filter_complex", "[0][1]hstack",
                    "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)

    def mean_rgb(path):
        raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", "1", "-i", str(path), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
        return np.frombuffer(raw, np.uint8).reshape(-1, 3).mean(axis=0)

    left = mean_rgb(clipper.render_clip(src, tmp_path / "l.mp4", 0, 2, focus_x=0.2))
    right = mean_rgb(clipper.render_clip(src, tmp_path / "r.mp4", 0, 2, focus_x=0.8))
    assert left[0] > 200 and left[2] < 40 and right[2] > 200 and right[0] < 40
    assert probe(tmp_path / "l.mp4", "stream=width,height") == "1080,1920"
    assert vision.face_center_x(src, 0, 2) is None            # real detector, no face in the picture


def test_smart_layout_uses_face_position(video, tmp_path, monkeypatch):
    from tiktokbot import vision
    fake_source(monkeypatch, video)
    monkeypatch.setattr(vision, "face_center_x", lambda path, start, length: 0.7)
    seen = []
    real = clipper.render_clip
    monkeypatch.setattr(clipper, "render_clip", lambda *a, **k: seen.append(a) or real(*a, **k))
    bot = Bot(load_config(write_cfg(tmp_path, clips={"per_source": 1, "layout": "smart"})))
    assert bot.harvest() == 1 and seen[0][-1] == 0.7


# ---- draft mode and statistics -------------------------------------------
def test_draft_mode_uploads_to_inbox(tmp_path, monkeypatch):
    client = TikTokClient({"client_key": "k", "client_secret": "s", "redirect_uri": "https://x/cb", "mode": "draft",
                           "stats": True}, tmp_path / "tok.json")
    assert "scope=user.info.basic%2Cvideo.upload%2Cvideo.list" in client.authorize_url()
    monkeypatch.setattr(client, "access_token", lambda: "tok")
    calls = []

    def fake_post(endpoint, body=None, params=None):
        calls.append((endpoint, body))
        return {"publish_id": "p1", "upload_url": "https://upload"} if "init" in endpoint else {"status": "SEND_TO_USER_INBOX"}

    class Resp:
        status_code = 201

    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(client.session, "put", lambda url, data, headers, timeout: Resp())
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x" * 10)
    assert client.upload_video(f, "cap") == "p1"
    assert calls[0][0] == "/post/publish/inbox/video/init/" and "post_info" not in calls[0][1]
    assert len(calls) == 2   # no creator_info query, finished once the draft reached the inbox


def test_stats_sync_and_source_ranking(tmp_path, monkeypatch):
    cfg = load_config(write_cfg(tmp_path, sources=[{"url": "https://a"}, {"url": "https://b"}, {"url": "https://c"}]))
    bot = Bot(cfg)
    ids = [bot.db.add_clip(f"{u}.mp4", "cap", f"S:{u}", source_url=f"https://{u}") for u in "ab"]
    for i in ids:
        bot.db.mark_posted(i, f"pub{i}")
    monkeypatch.setattr(TikTokClient, "video_id", lambda self, publish_id: "v" + publish_id)
    monkeypatch.setattr(TikTokClient, "video_stats", lambda self, vids: {
        f"vpub{ids[0]}": {"view_count": 100, "like_count": 5}, f"vpub{ids[1]}": {"view_count": 9000, "like_count": 700}})
    assert bot.sync_stats() == 2
    assert bot.db.get(ids[1])["views"] == 9000 and bot.db.get(ids[1])["likes"] == 700
    # best performer first, the untested source in the middle, the weak one last
    assert [s["url"] for s in bot.ranked_sources()] == ["https://b", "https://c", "https://a"]


# ---- dashboard -----------------------------------------------------------
@pytest.fixture
def dashboard(video, tmp_path, monkeypatch):
    import threading
    from tiktokbot import web
    cfg = load_config(write_cfg(tmp_path, review={"require_approval": True}, web={"token": "s3cret"}))
    fake_source(monkeypatch, video)
    bot = Bot(cfg)
    bot.harvest()
    server = web.make_server(cfg, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield bot, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_dashboard_api(dashboard):
    import requests
    bot, base = dashboard
    assert requests.get(base + "/api/state").status_code == 401
    s = requests.Session()
    page = s.get(base + "/?token=s3cret")
    assert page.status_code == 200 and "TikTokBot" in page.text and s.cookies.get("tt_token")
    state = s.get(base + "/api/state").json()
    a, b = [c["id"] for c in state["clips"]]
    assert all(c["status"] == "pending" and c["has_file"] for c in state["clips"]) and len(state["next_posts"]) == 3

    part = s.get(f"{base}/media/{a}", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100 and part.headers["Content-Range"].startswith("bytes 0-99/")
    assert s.get(f"{base}/media/{a}").content[4:8] == b"ftyp"
    assert s.get(f"{base}/media/999").status_code == 404

    assert s.post(f"{base}/api/clips/{a}/approve", data="{}").status_code == 400                       # not JSON
    assert s.post(f"{base}/api/clips/{a}/approve", json={}, headers={"Origin": "https://evil.example"}).status_code == 400
    assert s.post(f"{base}/api/clips/{a}/caption", json={"text": "  New   caption "}).status_code == 200
    assert s.post(f"{base}/api/clips/{a}/approve", json={}).status_code == 200
    assert s.post(f"{base}/api/clips/{a}/approve", json={}).status_code == 409                         # already approved
    assert s.post(f"{base}/api/clips/{b}/reject", json={}).status_code == 200
    assert bot.db.get(a)["status"] == "queued" and bot.db.get(a)["caption"] == "New caption"
    assert bot.db.get(b)["status"] == "rejected" and not __import__("pathlib").Path(bot.db.get(b)["path"]).exists()


def test_dashboard_refuses_public_bind_without_token(tmp_path):
    from tiktokbot import web
    with pytest.raises(ValueError):
        web.make_server(load_config(write_cfg(tmp_path)), "0.0.0.0", 0)


def test_source_keyword_match():
    from tiktokbot.sources import matches
    assert matches({"title": "Whatever"}, [])
    assert matches({"title": "Erster Tag in GTA 6!"}, ["gta 6", "gta vi"])
    assert matches({"title": "x", "description": "we play GTA VI"}, ["gta 6", "gta vi"])
    assert not matches({"title": "Minecraft Hardcore"}, ["gta 6", "gta vi"])


def test_dashboard_sources_add_remove(tmp_path):
    bot = Bot(load_config(write_cfg(tmp_path)))
    before = len(bot.all_sources())
    src = bot.add_source("https://www.twitch.tv/somestreamer", ["GTA 6", " "], ["#gta6", "fyp"])
    assert src["record_seconds"] == 1200 and src["match"] == ["gta 6"] and src["hashtags"] == ["gta6", "fyp"]
    assert len(Bot(bot.cfg).all_sources()) == before + 1
    with pytest.raises(ValueError):
        bot.add_source("https://www.twitch.tv/somestreamer")
    with pytest.raises(ValueError):
        bot.add_source("not a link")
    assert "record_seconds" not in bot.add_source("https://www.youtube.com/@someone/videos")
    assert bot.remove_source("https://www.twitch.tv/somestreamer")
    assert not bot.remove_source("https://www.twitch.tv/somestreamer")
    assert len(bot.all_sources()) == before + 1


def test_dashboard_source_api(tmp_path):
    import json as _json
    import threading as _threading
    import urllib.request
    from tiktokbot import web
    cfg = load_config(write_cfg(tmp_path))
    server = web.make_server(cfg, "127.0.0.1", 0)
    _threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path, body):
        req = urllib.request.Request(base + path, data=_json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        assert call("/api/sources", {"url": "https://kick.com/someone", "match": "gta 6, gta vi", "hashtags": "gta6"}) == 200
        assert call("/api/sources", {"url": "nope"}) == 400
        state = _json.loads(urllib.request.urlopen(base + "/api/state").read())
        added = [s for s in state["sources"] if s["url"] == "https://kick.com/someone"][0]
        assert added["live"] and added["removable"] and added["match"] == ["gta 6", "gta vi"]
        assert call("/api/sources/remove", {"url": "https://kick.com/someone"}) == 200
    finally:
        server.shutdown()


def _fake_tiktok(monkeypatch, open_id, name):
    import json as _json
    from tiktokbot import tiktok_api

    def exchange(self, code):
        assert code == "good-code"
        tok = {"access_token": "a", "refresh_token": "r", "open_id": open_id, "expires_at": time.time() + 3600}
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(_json.dumps(tok))
        return tok

    monkeypatch.setattr(tiktok_api.TikTokClient, "exchange_code", exchange)
    monkeypatch.setattr(tiktok_api.TikTokClient, "user_info", lambda self: {"open_id": open_id, "display_name": name})


def _state_from(url):
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(url).query)["state"][0]


def test_accounts_connect_and_separate(tmp_path, monkeypatch):
    from tiktokbot.accounts import MAIN, Accounts
    cfg = load_config(write_cfg(tmp_path, tiktok={"client_key": "", "client_secret": "", "redirect_uri": ""}))
    accounts = Accounts(cfg)
    assert not accounts.app_ready()
    accounts.save_app("key", "secret", "http://localhost:8080/callback")
    assert accounts.app_ready()

    _fake_tiktok(monkeypatch, "user-one", "One")
    first = accounts.finish_connect(_state_from(accounts.start_connect()), "good-code")
    assert first["id"] == MAIN and first["name"] == "One" and first["connected"]

    _fake_tiktok(monkeypatch, "user-two", "Two")
    second = accounts.finish_connect(_state_from(accounts.start_connect()), "good-code")
    assert second["id"] == "user-two" and second["connected"]
    assert [a["id"] for a in accounts.list()] == [MAIN, "user-two"]

    _fake_tiktok(monkeypatch, "user-one", "One again")
    again = accounts.finish_connect(_state_from(accounts.start_connect()), "good-code")
    assert again["id"] == MAIN and len(accounts.list()) == 2

    two = accounts.bot("user-two")
    assert two.all_sources() == []
    two.add_source("https://www.twitch.tv/someone")
    assert all(s["url"] != "https://www.twitch.tv/someone" for s in accounts.bot(MAIN).all_sources())
    assert two.db.conn is not accounts.bot(MAIN).db.conn

    accounts.save_settings("user-two", ["18:30", "08:00"], True)
    cfg2 = accounts.cfg_for("user-two")
    assert cfg2["schedule"]["post_times"] == ["08:00", "18:30"] and cfg2["review"]["require_approval"]
    with pytest.raises(ValueError):
        accounts.save_settings("user-two", ["9 Uhr"])
    with pytest.raises(ValueError):
        accounts.finish_connect("expired-state", "good-code")

    accounts.disconnect("user-two")
    assert not accounts.describe("user-two")["connected"]


def test_dashboard_connect_flow(tmp_path, monkeypatch):
    import json as _json
    import threading as _threading
    import urllib.request
    from tiktokbot import web
    cfg = load_config(write_cfg(tmp_path, tiktok={"client_key": "", "client_secret": "", "redirect_uri": ""}))
    server = web.make_server(cfg, "127.0.0.1", 0)
    _threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect)

    def get(path):
        try:
            r = opener.open(base + path)
            return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def post(path, body):
        req = urllib.request.Request(base + path, data=_json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        state = _json.loads(get("/api/state")[2])
        assert state["app_ready"] is False
        assert post("/api/app", {"client_key": "k", "client_secret": "s", "redirect_uri": "http://localhost:8080/callback"}) == 200
        status, headers, _ = get("/connect")
        assert status == 302 and headers["Location"].startswith("https://www.tiktok.com/v2/auth/authorize/")
        _fake_tiktok(monkeypatch, "abc", "Max")
        status, headers, _ = get(f"/callback?code=good-code&state={_state_from(headers['Location'])}")
        assert status == 302 and "account=main" in headers["Location"]
        state = _json.loads(get("/api/state")[2])
        assert state["accounts"][0]["name"] == "Max" and state["connected"]
        assert get("/callback?code=good-code&state=nope")[0] == 400
        assert get("/api/state?account=ghost")[0] == 404
        assert post("/api/settings", {"post_times": "10:00, 22:00", "require_approval": False}) == 200
        state = _json.loads(get("/api/state")[2])
        assert state["post_times"] == ["10:00", "22:00"] and state["require_approval"] is False
    finally:
        server.shutdown()
