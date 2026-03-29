"""Dead Drop message store — SQLite-backed encrypted message queue."""

import logging
import os
import sqlite3
import threading
import time
import uuid
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Default message TTL: 72 hours
DEFAULT_TTL_HOURS = 72


class DeadDropStore:
    """Persistent store for encrypted dead drop messages."""

    def __init__(self, data_dir: Optional[str] = None, ttl_hours: int = DEFAULT_TTL_HOURS):
        if data_dir is None:
            data_dir = os.path.join(os.path.expanduser("~"), ".mesh-llm")
        os.makedirs(data_dir, exist_ok=True)

        self._db_path = os.path.join(data_dir, "dead_drop.db")
        self._ttl_seconds = ttl_hours * 3600
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    msg_id TEXT PRIMARY KEY,
                    sender_node TEXT NOT NULL,
                    recipient_node TEXT NOT NULL,
                    encrypted_payload BLOB NOT NULL,
                    created_at REAL NOT NULL,
                    delivered_at REAL,
                    expires_at REAL NOT NULL,
                    status TEXT DEFAULT 'pending'
                );
                CREATE INDEX IF NOT EXISTS idx_dd_recipient
                    ON messages(recipient_node, status);
                CREATE INDEX IF NOT EXISTS idx_dd_expires
                    ON messages(expires_at);

                CREATE TABLE IF NOT EXISTS node_keys (
                    node_id TEXT PRIMARY KEY,
                    derived_key BLOB NOT NULL,
                    registered_at REAL NOT NULL
                );
            """)
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def register_key(self, node_id: str, derived_key: bytes):
        """Store a node's derived encryption key."""
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO node_keys (node_id, derived_key, registered_at) "
                "VALUES (?, ?, ?)",
                (node_id, derived_key, time.time()),
            )
            conn.commit()
            conn.close()
        logger.info(f"Dead Drop: key registered for {node_id}")

    def get_key(self, node_id: str) -> Optional[bytes]:
        """Get a node's derived encryption key."""
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT derived_key FROM node_keys WHERE node_id = ?",
                (node_id,),
            ).fetchone()
            conn.close()
            return row["derived_key"] if row else None

    def has_key(self, node_id: str) -> bool:
        """Check if a node has registered an encryption key."""
        return self.get_key(node_id) is not None

    def store_message(
        self, sender: str, recipient: str, encrypted_payload: bytes
    ) -> str:
        """Store an encrypted message for later pickup.

        Returns:
            Message ID.
        """
        msg_id = uuid.uuid4().hex[:12]
        now = time.time()
        expires = now + self._ttl_seconds

        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO messages "
                "(msg_id, sender_node, recipient_node, encrypted_payload, "
                "created_at, expires_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                (msg_id, sender, recipient, encrypted_payload, now, expires),
            )
            conn.commit()
            conn.close()

        logger.info(f"Dead Drop: stored message {msg_id} from {sender} to {recipient}")
        return msg_id

    def pickup_messages(self, node_id: str) -> List[Dict]:
        """Retrieve and mark as delivered all pending messages for a node.

        Returns:
            List of dicts with keys: msg_id, sender_node, encrypted_payload, created_at
        """
        now = time.time()
        with self._lock:
            conn = self._connect()
            # Expire old messages first
            conn.execute(
                "UPDATE messages SET status = 'expired' "
                "WHERE status = 'pending' AND expires_at < ?",
                (now,),
            )
            # Get pending messages
            rows = conn.execute(
                "SELECT msg_id, sender_node, encrypted_payload, created_at "
                "FROM messages "
                "WHERE recipient_node = ? AND status = 'pending' "
                "ORDER BY created_at ASC",
                (node_id,),
            ).fetchall()
            # Mark as delivered
            if rows:
                ids = [r["msg_id"] for r in rows]
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE messages SET status = 'delivered', delivered_at = ? "
                    f"WHERE msg_id IN ({placeholders})",
                    [now] + ids,
                )
            conn.commit()
            conn.close()

        return [dict(r) for r in rows]

    def pending_count(self, node_id: str) -> int:
        """Count pending messages for a node."""
        now = time.time()
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM messages "
                "WHERE recipient_node = ? AND status = 'pending' AND expires_at > ?",
                (node_id, now),
            ).fetchone()
            conn.close()
            return row["cnt"]

    def get_stats(self) -> Dict:
        """Get overall dead drop statistics."""
        now = time.time()
        with self._lock:
            conn = self._connect()
            # Expire old messages
            conn.execute(
                "UPDATE messages SET status = 'expired' "
                "WHERE status = 'pending' AND expires_at < ?",
                (now,),
            )
            conn.commit()

            stats = {}
            for status in ("pending", "delivered", "expired"):
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM messages WHERE status = ?",
                    (status,),
                ).fetchone()
                stats[status] = row["cnt"]

            row = conn.execute("SELECT COUNT(*) as cnt FROM node_keys").fetchone()
            stats["registered_nodes"] = row["cnt"]

            conn.close()

        stats["total"] = stats["pending"] + stats["delivered"] + stats["expired"]
        return stats

    def get_recent_messages(self, limit: int = 50) -> List[Dict]:
        """Get recent messages for dashboard display (no content — encrypted)."""
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT msg_id, sender_node, recipient_node, status, "
                "created_at, delivered_at, expires_at "
                "FROM messages ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
        return [dict(r) for r in rows]

    def purge_expired(self) -> int:
        """Delete all expired messages. Returns count deleted."""
        with self._lock:
            conn = self._connect()
            cursor = conn.execute("DELETE FROM messages WHERE status = 'expired'")
            count = cursor.rowcount
            conn.commit()
            conn.close()
        if count:
            logger.info(f"Dead Drop: purged {count} expired messages")
        return count
