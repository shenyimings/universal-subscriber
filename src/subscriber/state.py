"""SQLite state: seen RSS items, page snapshots, key-value store."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    first_seen TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (source, item_id)
);
CREATE TABLE IF NOT EXISTS page_snapshots (
    source TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class State:
    def __init__(self, db_path: str | Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.executescript(SCHEMA)

    def is_seen(self, source: str, item_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM seen_items WHERE source = ? AND item_id = ?",
            (source, item_id),
        ).fetchone()
        return row is not None

    def mark_seen(self, source: str, item_id: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO seen_items (source, item_id) VALUES (?, ?)",
            (source, item_id),
        )
        self.db.commit()

    def get_snapshot(self, source: str) -> str | None:
        row = self.db.execute(
            "SELECT content FROM page_snapshots WHERE source = ?", (source,)
        ).fetchone()
        return row[0] if row else None

    def save_snapshot(self, source: str, content: str) -> None:
        self.db.execute(
            "INSERT INTO page_snapshots (source, content, updated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(source) DO UPDATE SET content = excluded.content, "
            "updated_at = datetime('now')",
            (source, content),
        )
        self.db.commit()

    def kv_get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def kv_set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.db.commit()
