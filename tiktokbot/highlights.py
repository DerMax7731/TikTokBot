"""Highlight detection: audio energy fused with YouTube's "most replayed" heatmap."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

RATE = 8000


def probe_duration(path: str | Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


def audio_energy(path: str | Path) -> np.ndarray:
    """RMS loudness per second of the file's audio (empty array when there is none)."""
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(RATE),
         "-f", "s16le", "-"],
        capture_output=True,
    )
    samples = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32)
    seconds = len(samples) // RATE
    if seconds == 0:
        return np.zeros(0, dtype=np.float32)
    frames = samples[: seconds * RATE].reshape(seconds, RATE)
    return np.sqrt((frames ** 2).mean(axis=1))


def heatmap_curve(heatmap: list[dict] | None, seconds: int) -> np.ndarray | None:
    """YouTube 'most replayed' data as one value (0..1) per second."""
    if not heatmap or seconds <= 0:
        return None
    curve = np.zeros(seconds, dtype=np.float32)
    for point in heatmap:
        a, b = int(point.get("start_time", 0)), int(np.ceil(point.get("end_time", 0)))
        curve[max(a, 0): min(max(b, a + 1), seconds)] = point.get("value", 0.0)
    # The first seconds of every video are "most replayed" simply because everyone starts there.
    curve[: min(20, seconds // 10)] *= 0.3
    peak = curve.max()
    return curve / peak if peak > 0 else None


def build_score(energy: np.ndarray, heatmap: list[dict] | None = None) -> np.ndarray:
    """Per-second interest score. Audio: loudness plus how far it rises above the
    video's own baseline. Viewer replay data, when available, counts more."""
    if len(energy) == 0:
        return energy
    norm = energy / (energy.max() or 1.0)
    score = (norm + np.clip(norm - np.median(norm), 0, None)) / 2
    heat = heatmap_curve(heatmap, len(energy))
    if heat is not None:
        score = score + 1.5 * heat
    return score


def pick_windows(score: np.ndarray, duration: float, length: int, count: int,
                 min_gap: int = 20) -> list[tuple[float, float]]:
    """The `count` best non-overlapping windows as (start_seconds, score), best first."""
    length = int(length)
    if duration <= length:
        return [(0.0, 0.0)]
    last_start = int(duration - length)
    if len(score) < length:
        # No usable audio: spread the clips evenly.
        return [(float(s), 0.0) for s in np.linspace(0, last_start, num=count + 2)[1:-1].astype(int)]

    sums = np.convolve(score, np.ones(length), mode="valid")[: last_start + 1]
    picked: list[tuple[float, float]] = []
    for idx in np.argsort(sums)[::-1]:
        if all(abs(int(idx) - s) >= length + min_gap for s, _ in picked):
            picked.append((float(idx), float(sums[idx] / length)))
            if len(picked) == count:
                break
    return picked


def snap_to_quiet(energy: np.ndarray, t: float, before: int = 3, after: int = 1) -> float:
    """Move a cut point to the quietest second nearby so clips don't start or end mid-sentence."""
    if len(energy) == 0:
        return t
    lo, hi = max(int(t) - before, 0), min(int(t) + after, len(energy) - 1)
    if hi <= lo:
        return t
    return float(lo + int(np.argmin(energy[lo: hi + 1])))
