"""SQLite state: which sources were processed, the clip queue, filled post slots."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    source_id TEXT PRIMARY KEY,
    url TEXT,
    added_at REAL
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    caption TEXT NOT NULL,
    source_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued',   -- pending | queued | posted | failed | rejected
    publish_id TEXT,
    error TEXT,
    created_at REAL,
    posted_at REAL
);
CREATE TABLE IF NOT EXISTS slots (
    slot TEXT PRIMARY KEY,                   -- 'YYYY-MM-DD HH:MM'
    clip_id INTEGER,
    handled_at REAL
);
"""
MIGRATIONS = {"source_url": "TEXT", "video_id": "TEXT", "views": "INTEGER", "likes": "INTEGER", "comments": "INTEGER",
              "shares": "INTEGER", "stats_at": "REAL", "hook": "TEXT", "score": "REAL DEFAULT 0", "attempts": "INTEGER DEFAULT 0", "retry_at": "REAL DEFAULT 0"}


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(clips)")}
        with self.conn:
            for col, ddl in MIGRATIONS.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE clips ADD COLUMN {col} {ddl}")

    def is_seen(self, source_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM seen WHERE source_id=?", (source_id,)).fetchone() is not None

    def mark_seen(self, source_id: str, url: str = "") -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?)", (source_id, url, time.time()))

    def add_clip(self, path: str, caption: str, source_id: str, hook: str | None = None,
                 score: float = 0.0, status: str = "queued", source_url: str | None = None) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO clips (path, caption, source_id, hook, score, status, source_url, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (path, caption, source_id, hook, score, status, source_url, time.time()),
            )
        return cur.lastrowid

    def count(self, *statuses: str) -> int:
        marks = ",".join("?" * len(statuses))
        return self.conn.execute(f"SELECT COUNT(*) FROM clips WHERE status IN ({marks})", statuses).fetchone()[0]

    def queued_count(self) -> int:
        return self.count("queued")

    def next_clip(self, now: float | None = None):
        """Oldest postable clip, preferring a different source than the last post."""
        now = time.time() if now is None else now
        last = self.conn.execute(
            "SELECT source_id FROM clips WHERE status='posted' ORDER BY posted_at DESC LIMIT 1"
        ).fetchone()
        base = "SELECT * FROM clips WHERE status='queued' AND retry_at<=?"
        if last:
            row = self.conn.execute(base + " AND source_id IS NOT ? ORDER BY id LIMIT 1", (now, last["source_id"])).fetchone()
            if row:
                return row
        return self.conn.execute(base + " ORDER BY id LIMIT 1", (now,)).fetchone()

    def get(self, clip_id: int):
        return self.conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()

    def set_status(self, clip_id: int, status: str, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute("UPDATE clips SET status=?, error=? WHERE id=?", (status, (error or "")[:500] or None, clip_id))

    def set_caption(self, clip_id: int, caption: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE clips SET caption=? WHERE id=?", (caption, clip_id))

    def mark_posted(self, clip_id: int, publish_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE clips SET status='posted', publish_id=?, posted_at=?, error=NULL WHERE id=?",
                (publish_id, time.time(), clip_id),
            )

    def mark_failed(self, clip_id: int, error: str) -> None:
        self.set_status(clip_id, "failed", error)

    def mark_retry(self, clip_id: int, error: str, retry_at: float) -> int:
        """Record a failed attempt and schedule the next one. Returns attempts so far."""
        with self.conn:
            self.conn.execute(
                "UPDATE clips SET attempts=attempts+1, retry_at=?, error=? WHERE id=?", (retry_at, error[:500], clip_id)
            )
        return self.get(clip_id)["attempts"]

    def slot_done(self, slot: str) -> bool:
        return self.conn.execute("SELECT 1 FROM slots WHERE slot=?", (slot,)).fetchone() is not None

    def mark_slot(self, slot: str, clip_id: int | None) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO slots VALUES (?,?,?)", (slot, clip_id, time.time()))

    def clips(self, status: str | None = None):
        if status:
            return self.conn.execute("SELECT * FROM clips WHERE status=? ORDER BY id", (status,)).fetchall()
        return self.conn.execute("SELECT * FROM clips ORDER BY id").fetchall()

    def set_video_id(self, clip_id: int, video_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE clips SET video_id=? WHERE id=?", (video_id, clip_id))

    def set_stats(self, clip_id: int, views: int, likes: int, comments: int, shares: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE clips SET views=?, likes=?, comments=?, shares=?, stats_at=? WHERE id=?",
                (views, likes, comments, shares, time.time(), clip_id),
            )

    def source_performance(self) -> dict[str, float]:
        """Average views per posted clip for each configured source url."""
        rows = self.conn.execute(
            "SELECT source_url, AVG(views) AS v FROM clips WHERE views IS NOT NULL AND source_url IS NOT NULL"
            " GROUP BY source_url"
        ).fetchall()
        return {r["source_url"]: float(r["v"]) for r in rows}
