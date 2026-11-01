"""Conversation storage: one SQLite file, separate from the brain (chats aren't knowledge until
they're saved into the brain)."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    recipe TEXT NOT NULL,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '{}',      -- engine state: system prompt, provider history, CLI session
    saved_to_brain TEXT                     -- source name once saved into the brain
);
CREATE TABLE IF NOT EXISTS items (
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,                     -- user | assistant | tool_call | tool_result | error
    data TEXT NOT NULL,
    created TEXT NOT NULL,
    PRIMARY KEY (thread_id, seq)
);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def create_thread(self, recipe: str, title: str = "New chat") -> dict:
        tid, now = uuid.uuid4().hex[:12], _now()
        with self._lock, self._db:
            self._db.execute("INSERT INTO threads (id, title, recipe, created, updated) VALUES (?, ?, ?, ?, ?)",
                             (tid, title, recipe, now, now))
        return self.thread(tid)

    def thread(self, tid: str) -> dict | None:
        row = self._db.execute("SELECT * FROM threads WHERE id = ?", (tid,)).fetchone()
        if row is None:
            return None
        t = dict(row)
        t["state"] = json.loads(t["state"])
        return t

    def threads(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, title, recipe, created, updated, saved_to_brain FROM threads ORDER BY updated DESC").fetchall()
        return [dict(r) for r in rows]

    def update_thread(self, tid: str, **fields) -> None:
        if "state" in fields:
            fields["state"] = json.dumps(fields["state"], ensure_ascii=False)
        fields["updated"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._db:
            self._db.execute(f"UPDATE threads SET {cols} WHERE id = ?", (*fields.values(), tid))

    def delete_thread(self, tid: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM threads WHERE id = ?", (tid,))

    def add_items(self, tid: str, items: list[tuple[str, dict]]) -> None:
        with self._lock, self._db:
            seq = self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM items WHERE thread_id = ?",
                                   (tid,)).fetchone()[0]
            now = _now()
            self._db.executemany(
                "INSERT INTO items (thread_id, seq, kind, data, created) VALUES (?, ?, ?, ?, ?)",
                [(tid, seq + i, kind, json.dumps(data, ensure_ascii=False), now)
                 for i, (kind, data) in enumerate(items, 1)])
            self._db.execute("UPDATE threads SET updated = ? WHERE id = ?", (now, tid))

    def items(self, tid: str) -> list[dict]:
        rows = self._db.execute("SELECT seq, kind, data, created FROM items WHERE thread_id = ? ORDER BY seq",
                                (tid,)).fetchall()
        return [{"seq": r["seq"], "kind": r["kind"], "created": r["created"], **json.loads(r["data"])} for r in rows]
