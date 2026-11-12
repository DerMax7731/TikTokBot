"""Word-level transcripts (YouTube captions or local Whisper) and animated ASS subtitles."""
from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Word:
    start: float
    end: float
    text: str


def from_youtube_json3(data: dict) -> list[Word]:
    """Parse YouTube's json3 caption format into words with timings."""
    words: list[Word] = []
    for event in data.get("events") or []:
        segs = [s for s in event.get("segs") or [] if (s.get("utf8") or "").strip()]
        if not segs:
            continue
        t0 = event.get("tStartMs", 0) / 1000
        t1 = t0 + event.get("dDurationMs", 0) / 1000
        if len(segs) == 1 and "tOffsetMs" not in segs[0]:
            # Manual captions: one line per event, spread the words evenly.
            parts = segs[0]["utf8"].split()
            step = (t1 - t0) / max(len(parts), 1)
            words += [Word(t0 + i * step, t0 + (i + 1) * step, p) for i, p in enumerate(parts)]
            continue
        for i, seg in enumerate(segs):
            start = t0 + seg.get("tOffsetMs", 0) / 1000
            end = t0 + segs[i + 1].get("tOffsetMs", 0) / 1000 if i + 1 < len(segs) else min(t1, start + 1.0)
            for part in seg["utf8"].split():
                words.append(Word(start, max(end, start + 0.05), part))
    words.sort(key=lambda w: w.start)
    return words


_whisper_model = None


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def transcribe(path: str | Path, start: float, length: float, model_name: str = "small") -> list[Word]:
    """Transcribe one stretch of a media file with faster-whisper. Times are relative to `start`."""
    global _whisper_model
    from faster_whisper import WhisperModel

    if _whisper_model is None:
        log.info("loading whisper model %s", model_name)
        _whisper_model = WhisperModel(model_name, device="auto", compute_type="int8")
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", str(path), "-t", f"{length:.2f}",
             "-vn", "-ac", "1", "-ar", "16000", str(wav)],
            check=True,
        )
        segments, _ = _whisper_model.transcribe(str(wav), word_timestamps=True, vad_filter=True)
        return [Word(w.start, w.end, w.word.strip()) for seg in segments for w in (seg.words or []) if w.word.strip()]


def slice_words(words: list[Word], start: float, length: float) -> list[Word]:
    """Words inside [start, start+length), re-timed relative to `start`."""
    end = start + length
    return [
        Word(max(w.start - start, 0.0), min(w.end - start, length), w.text)
        for w in words
        if w.start >= start - 0.05 and w.start < end
    ]


def text(words: list[Word]) -> str:
    return " ".join(w.text for w in words)


# ---- ASS subtitles -------------------------------------------------------
def _ts(seconds: float) -> str:
    cs = max(int(round(seconds * 100)), 0)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _esc(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def chunk_words(words: list[Word], max_words: int = 3, max_chars: int = 16, max_gap: float = 0.6) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    for w in words:
        cur = chunks[-1] if chunks else None
        if (cur and len(cur) < max_words and w.start - cur[-1].end <= max_gap
                and sum(len(x.text) + 1 for x in cur) + len(w.text) <= max_chars):
            cur.append(w)
        else:
            chunks.append([w])
    return chunks


def build_ass(words: list[Word], path: str | Path, hook: str | None = None, length: float = 0.0,
              font: str = "DejaVu Sans", highlight: str = "&H0000F0FF", hook_seconds: float = 4.0) -> Path:
    """Write an ASS file for a 1080x1920 clip: word-by-word captions that light up
    the spoken word, plus an optional hook headline at the top."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{font},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,2,2,70,70,520,1",
        f"Style: Hook,{font},64,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,14,0,8,90,90,230,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if hook:
        end = min(hook_seconds, length) if length else hook_seconds
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(end)},Hook,,0,0,0,,{{\\fad(150,250)}}{_esc(hook)}")
    for chunk in chunk_words(words):
        for i, word in enumerate(chunk):
            start = word.start
            end = chunk[i + 1].start if i + 1 < len(chunk) else max(word.end, word.start + 0.15)
            if end <= start:
                continue
            parts = [
                f"{{\\c{highlight}}}{_esc(w.text.upper())}{{\\c&H00FFFFFF}}" if j == i else _esc(w.text.upper())
                for j, w in enumerate(chunk)
            ]
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{' '.join(parts)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
