"""Highlight detection: the loudest, most eventful stretches of the audio track."""
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


def pick_windows(energy: np.ndarray, duration: float, length: int, count: int, min_gap: int = 20) -> list[float]:
    """Start times (seconds, chronological) of the `count` best non-overlapping windows."""
    length = int(length)
    if duration <= length:
        return [0.0]
    last_start = int(duration - length)
    if len(energy) < length:
        # No usable audio: spread the clips evenly.
        return [float(s) for s in np.linspace(0, last_start, num=count + 2)[1:-1].astype(int)]

    # Score = loudness plus how much it rises above the video's own baseline.
    norm = energy / (energy.max() or 1.0)
    score = norm + np.clip(norm - np.median(norm), 0, None)
    sums = np.convolve(score, np.ones(length), mode="valid")[: last_start + 1]

    starts: list[int] = []
    order = np.argsort(sums)[::-1]
    for idx in order:
        if all(abs(int(idx) - s) >= length + min_gap for s in starts):
            starts.append(int(idx))
            if len(starts) == count:
                break
    return [float(s) for s in sorted(starts)]
