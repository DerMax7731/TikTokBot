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
    status TEXT NOT NULL DEFAULT 'queued',   -- queued | posted | failed
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


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def is_seen(self, source_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM seen WHERE source_id=?", (source_id,)).fetchone() is not None

    def mark_seen(self, source_id: str, url: str = "") -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?)", (source_id, url, time.time()))

    def add_clip(self, path: str, caption: str, source_id: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO clips (path, caption, source_id, created_at) VALUES (?,?,?,?)",
                (path, caption, source_id, time.time()),
            )
        return cur.lastrowid

    def queued_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM clips WHERE status='queued'").fetchone()[0]

    def next_clip(self):
        """Oldest queued clip, preferring a different source than the last post."""
        last = self.conn.execute(
            "SELECT source_id FROM clips WHERE status='posted' ORDER BY posted_at DESC LIMIT 1"
        ).fetchone()
        if last:
            row = self.conn.execute(
                "SELECT * FROM clips WHERE status='queued' AND source_id IS NOT ? ORDER BY id LIMIT 1",
                (last["source_id"],),
            ).fetchone()
            if row:
                return row
        return self.conn.execute("SELECT * FROM clips WHERE status='queued' ORDER BY id LIMIT 1").fetchone()

    def mark_posted(self, clip_id: int, publish_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE clips SET status='posted', publish_id=?, posted_at=? WHERE id=?",
                (publish_id, time.time(), clip_id),
            )

    def mark_failed(self, clip_id: int, error: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE clips SET status='failed', error=? WHERE id=?", (error[:500], clip_id))

    def slot_done(self, slot: str) -> bool:
        return self.conn.execute("SELECT 1 FROM slots WHERE slot=?", (slot,)).fetchone() is not None

    def mark_slot(self, slot: str, clip_id: int | None) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO slots VALUES (?,?,?)", (slot, clip_id, time.time()))

    def clips(self, status: str | None = None):
        if status:
            return self.conn.execute("SELECT * FROM clips WHERE status=? ORDER BY id", (status,)).fetchall()
        return self.conn.execute("SELECT * FROM clips ORDER BY id").fetchall()
