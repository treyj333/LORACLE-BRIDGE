"""Traffic aggregator — collects and categorizes mesh events for SITREP generation."""

import logging
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Auto-delete events older than this
EVENT_RETENTION_HOURS = 24


class TrafficAggregator:
    """Collects mesh traffic events in SQLite for SITREP generation."""

    def __init__(self, data_dir: Optional[str] = None):
        if data_dir is None:
            data_dir = os.path.join(os.path.expanduser("~"), ".mesh-llm")
        os.makedirs(data_dir, exist_ok=True)

        self._db_path = os.path.join(data_dir, "brief.db")
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS traffic_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    node_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    summary TEXT,
                    raw_text TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_traffic_time
                    ON traffic_events(timestamp);

                CREATE TABLE IF NOT EXISTS sitreps (
                    sitrep_id TEXT PRIMARY KEY,
                    generated_at REAL NOT NULL,
                    period_start REAL NOT NULL,
                    period_end REAL NOT NULL,
                    content TEXT NOT NULL,
                    node_count INTEGER DEFAULT 0,
                    message_count INTEGER DEFAULT 0,
                    metadata TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sitrep_time
                    ON sitreps(generated_at DESC);
            """)
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def record_event(self, node_id: str, event_type: str, summary: str, raw_text: str = ""):
        """Record a traffic event.

        Args:
            node_id: Mesh node identifier.
            event_type: One of 'message', 'command', 'checkin', 'alert'.
            summary: Short description of the event.
            raw_text: Full message text.
        """
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO traffic_events (timestamp, node_id, event_type, summary, raw_text) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), node_id, event_type, summary, raw_text),
            )
            conn.commit()
            conn.close()

    def get_events_since(self, since: float) -> List[Dict]:
        """Get all events since a timestamp."""
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM traffic_events WHERE timestamp > ? ORDER BY timestamp ASC",
                (since,),
            ).fetchall()
            conn.close()
        return [dict(r) for r in rows]

    def get_event_summary(self, since: float) -> Dict:
        """Get aggregated statistics for events since a timestamp."""
        with self._lock:
            conn = self._connect()

            # Count by type
            type_rows = conn.execute(
                "SELECT event_type, COUNT(*) as cnt "
                "FROM traffic_events WHERE timestamp > ? "
                "GROUP BY event_type",
                (since,),
            ).fetchall()
            by_type = {r["event_type"]: r["cnt"] for r in type_rows}

            # Unique nodes
            node_row = conn.execute(
                "SELECT COUNT(DISTINCT node_id) as cnt "
                "FROM traffic_events WHERE timestamp > ?",
                (since,),
            ).fetchone()

            # Total events
            total_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM traffic_events WHERE timestamp > ?",
                (since,),
            ).fetchone()

            conn.close()

        return {
            "total_events": total_row["cnt"],
            "unique_nodes": node_row["cnt"],
            "by_type": by_type,
            "since": since,
        }

    def store_sitrep(self, sitrep_id: str, content: str, period_start: float,
                     period_end: float, node_count: int, message_count: int):
        """Store a generated SITREP."""
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO sitreps "
                "(sitrep_id, generated_at, period_start, period_end, "
                "content, node_count, message_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sitrep_id, time.time(), period_start, period_end,
                 content, node_count, message_count),
            )
            conn.commit()
            conn.close()

    def get_latest_sitrep(self) -> Optional[Dict]:
        """Get the most recent SITREP."""
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM sitreps ORDER BY generated_at DESC LIMIT 1"
            ).fetchone()
            conn.close()
        return dict(row) if row else None

    def get_sitrep_history(self, limit: int = 20) -> List[Dict]:
        """Get recent SITREPs."""
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT sitrep_id, generated_at, period_start, period_end, "
                "node_count, message_count FROM sitreps "
                "ORDER BY generated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
        return [dict(r) for r in rows]

    def get_sitrep_by_id(self, sitrep_id: str) -> Optional[Dict]:
        """Get a specific SITREP by ID."""
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM sitreps WHERE sitrep_id = ?",
                (sitrep_id,),
            ).fetchone()
            conn.close()
        return dict(row) if row else None

    def cleanup_old_events(self):
        """Delete events older than retention period."""
        cutoff = time.time() - (EVENT_RETENTION_HOURS * 3600)
        with self._lock:
            conn = self._connect()
            cursor = conn.execute(
                "DELETE FROM traffic_events WHERE timestamp < ?", (cutoff,)
            )
            count = cursor.rowcount
            conn.commit()
            conn.close()
        if count:
            logger.debug(f"Brief: cleaned up {count} old traffic events")

    def get_stats(self) -> Dict:
        """Get overall aggregator statistics."""
        with self._lock:
            conn = self._connect()
            event_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM traffic_events"
            ).fetchone()["cnt"]
            sitrep_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM sitreps"
            ).fetchone()["cnt"]
            conn.close()
        return {
            "total_events": event_count,
            "total_sitreps": sitrep_count,
        }
