"""Key-value settings store backed by SQLite."""

import json
import sqlite3
import time
from typing import Any, Dict, Optional

from db.schema import get_lock


class SettingsStore:
    def __init__(self, db: sqlite3.Connection):
        self._db = db
        self._lock = get_lock(db)

    def get(self, key: str, default: Any = None) -> Any:
        row = self._db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO settings (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?""",
                (key, json.dumps(value), time.time(),
                 json.dumps(value), time.time()),
            )
            self._db.commit()

    def get_all(self) -> Dict[str, Any]:
        rows = self._db.execute("SELECT key, value FROM settings").fetchall()
        result = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                result[row["key"]] = row["value"]
        return result

    def delete(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM settings WHERE key = ?", (key,))
            self._db.commit()
