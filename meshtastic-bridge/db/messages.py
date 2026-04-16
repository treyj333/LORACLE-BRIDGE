"""Message CRUD and retention pruning for LORACLE Bridge."""

import logging
import sqlite3
import time
import uuid
from typing import Dict, List, Optional

logger = logging.getLogger("db.messages")


class MessageStore:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def insert(
        self,
        contact_id: str,
        direction: str,
        author: str,
        text: str,
        protocol: str,
        hops_traveled: Optional[int] = None,
        rx_rssi: Optional[int] = None,
        rx_snr: Optional[float] = None,
        is_channel_msg: bool = False,
        originating_channel_id: Optional[str] = None,
        delivery_status: str = "sent",
        timestamp: Optional[float] = None,
        msg_id: Optional[str] = None,
    ) -> str:
        """Insert a message and return its ID."""
        mid = msg_id or str(uuid.uuid4())
        ts = timestamp or time.time()
        self._db.execute(
            """INSERT INTO messages
               (id, contact_id, direction, author, text, timestamp, protocol,
                hops_traveled, rx_rssi, rx_snr, is_channel_msg,
                originating_channel_id, delivery_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (mid, contact_id, direction, author, text[:4000], ts, protocol,
             hops_traveled, rx_rssi, rx_snr,
             1 if is_channel_msg else 0,
             originating_channel_id, delivery_status),
        )
        self._db.commit()
        return mid

    def get_thread(self, contact_id: str, limit: int = 50,
                   offset: int = 0) -> List[dict]:
        """Get messages for a contact, newest first."""
        rows = self._db.execute(
            """SELECT * FROM messages
               WHERE contact_id = ?
               ORDER BY timestamp DESC
               LIMIT ? OFFSET ?""",
            (contact_id, min(limit, 500), offset),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]  # return oldest-first for display

    def get_recent(self, limit: int = 100) -> List[dict]:
        """Get recent messages across all contacts."""
        rows = self._db.execute(
            """SELECT m.*, c.short_name, c.long_name, c.protocol AS contact_protocol
               FROM messages m
               LEFT JOIN contacts c ON c.id = m.contact_id
               ORDER BY m.timestamp DESC
               LIMIT ?""",
            (min(limit, 500),),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def count_by_contact(self, contact_id: str) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS cnt FROM messages WHERE contact_id = ?",
            (contact_id,),
        ).fetchone()
        return row["cnt"] if row else 0

    def count_total(self) -> int:
        row = self._db.execute("SELECT COUNT(*) AS cnt FROM messages").fetchone()
        return row["cnt"] if row else 0

    def prune(self, max_per_contact: int = 500, max_age_days: int = 90) -> int:
        """Delete old messages. 500-cap first, then 90-day cutoff."""
        total_pruned = 0
        cutoff_ts = time.time() - (max_age_days * 86400)

        contacts = self._db.execute(
            "SELECT DISTINCT contact_id FROM messages"
        ).fetchall()

        for row in contacts:
            cid = row["contact_id"]

            # 1. Cap at max_per_contact: delete oldest beyond limit
            count = self.count_by_contact(cid)
            if count > max_per_contact:
                excess = count - max_per_contact
                self._db.execute(
                    """DELETE FROM messages WHERE id IN (
                         SELECT id FROM messages
                         WHERE contact_id = ?
                         ORDER BY timestamp ASC
                         LIMIT ?
                       )""",
                    (cid, excess),
                )
                total_pruned += excess

            # 2. Delete messages older than cutoff
            cursor = self._db.execute(
                "DELETE FROM messages WHERE contact_id = ? AND timestamp < ?",
                (cid, cutoff_ts),
            )
            total_pruned += cursor.rowcount

        if total_pruned > 0:
            self._db.commit()
            logger.info(
                f"Pruned {total_pruned} messages "
                f"(>{max_per_contact}/contact or >{max_age_days}d) "
                f"across {len(contacts)} contacts"
            )
        return total_pruned

    def delete_all(self) -> int:
        """Delete all messages. Returns count deleted."""
        cursor = self._db.execute("DELETE FROM messages")
        self._db.commit()
        count = cursor.rowcount
        logger.info(f"Deleted all {count} messages")
        return count

    def update_delivery_status(self, msg_id: str, status: str) -> None:
        self._db.execute(
            "UPDATE messages SET delivery_status = ? WHERE id = ?",
            (status, msg_id),
        )
        self._db.commit()
