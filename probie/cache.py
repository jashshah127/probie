"""A response cache keyed by the exact request, so rerunning the same setup is free."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path


def request_key(namespace: str, payload: dict) -> str:
    """Same endpoint and same payload give the same key. API keys are never part of it."""
    canonical = json.dumps({"endpoint": namespace, "payload": payload}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ResponseCache:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        with self._lock, self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, response TEXT NOT NULL)")

    def get(self, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT response FROM responses WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None

    def put(self, key: str, response: dict) -> None:
        text = json.dumps(response, ensure_ascii=False)
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO responses (key, response) VALUES (?, ?)", (key, text))

    def __len__(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM responses").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()
