"""Persistent audit log for bridge relay events (LORACLE v2 Phase 5).

Every relay decision — delivered, blocked by policy, rate-limited,
deduped, or loop-guarded — lands in the ``bridge_events`` table. The
in-memory ring buffer in StandaloneBridge handles live UI updates;
this table is the historical record for auditing / after-action review.

Retention: the bridge prunes rows older than RETENTION_DAYS on startup
(matches the existing messages-retention pattern).
"""

import sqlite3
import time
from typing import Any, Dict, List, Optional


RETENTION_DAYS = 30


class BridgeEventStore:
    """SQLite-backed audit log for cross-protocol relay decisions."""

    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def insert(
        self,
        source_protocol: str,
        dest_protocol: str,
        channel: int,
        sender: str,
        text: str,
        outcome: str,
        sender_display: Optional[str] = None,
        force_relay: bool = False,
        timestamp: Optional[float] = None,
    ) -> None:
        """Record one relay-decision event."""
        self._db.execute(
            """INSERT INTO bridge_events
               (timestamp, source_protocol, dest_protocol, channel, sender,
                sender_display, text, outcome, force_relay)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                timestamp if timestamp is not None else time.time(),
                source_protocol, dest_protocol, int(channel),
                sender, sender_display, text[:2000], outcome,
                1 if force_relay else 0,
            ),
        )
        self._db.commit()

    def recent(
        self,
        limit: int = 100,
        outcome: Optional[str] = None,
        since: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Query recent events. ``outcome`` and ``since`` narrow the set."""
        clauses = []
        params: List[Any] = []
        if outcome is not None:
            clauses.append("outcome = ?")
            params.append(outcome)
        if since is not None:
            clauses.append("timestamp > ?")
            params.append(float(since))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        rows = self._db.execute(
            f"SELECT * FROM bridge_events{where} ORDER BY timestamp DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self, since: Optional[float] = None) -> Dict[str, int]:
        """Return a by-outcome count of events since ``since`` (or ever)."""
        if since is None:
            rows = self._db.execute(
                "SELECT outcome, COUNT(*) AS n FROM bridge_events GROUP BY outcome"
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT outcome, COUNT(*) AS n FROM bridge_events "
                "WHERE timestamp > ? GROUP BY outcome",
                (float(since),),
            ).fetchall()
        return {r["outcome"]: r["n"] for r in rows}

    def prune(self, days: int = RETENTION_DAYS) -> int:
        """Delete events older than ``days``. Returns number removed."""
        cutoff = time.time() - days * 86400
        cur = self._db.execute(
            "DELETE FROM bridge_events WHERE timestamp < ?",
            (cutoff,),
        )
        self._db.commit()
        return cur.rowcount
