"""Contact CRUD operations for LORACLE Bridge."""

import sqlite3
import time
from typing import Dict, List, Optional


class ContactStore:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def upsert(self, contact_id: str, protocol: str, backend_id: str,
               short_name: str, long_name: Optional[str] = None,
               rssi: Optional[int] = None, snr: Optional[float] = None,
               hops: Optional[int] = None, is_channel: bool = False,
               channel_idx: Optional[int] = None,
               channel_name: Optional[str] = None) -> None:
        """Create or update a contact. Preserves ai_enabled and unread on update."""
        now = time.time()
        existing = self.get(contact_id)
        if existing is None:
            self._db.execute(
                """INSERT INTO contacts
                   (id, protocol, backend_id, short_name, long_name,
                    last_rssi, last_snr, last_hops, first_seen, last_heard,
                    is_channel, channel_idx, channel_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (contact_id, protocol, backend_id, short_name, long_name,
                 rssi, snr, hops, now, now,
                 1 if is_channel else 0, channel_idx, channel_name),
            )
        else:
            updates = ["last_heard = ?", "last_rssi = ?", "last_snr = ?", "last_hops = ?"]
            values = [now, rssi, snr, hops]
            if long_name and not existing["long_name"]:
                updates.append("long_name = ?")
                values.append(long_name)
            values.append(contact_id)
            self._db.execute(
                f"UPDATE contacts SET {', '.join(updates)} WHERE id = ?",
                values,
            )
        self._db.commit()

    def get(self, contact_id: str) -> Optional[dict]:
        row = self._db.execute(
            "SELECT * FROM contacts WHERE id = ?", (contact_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_all(self, filter_type: Optional[str] = None,
                 sort: str = "last_heard") -> List[dict]:
        """List contacts. filter_type: 'dm', 'channel', or None for all."""
        query = "SELECT * FROM contacts"
        params = []
        if filter_type == "dm":
            query += " WHERE is_channel = 0"
        elif filter_type == "channel":
            query += " WHERE is_channel = 1"
        valid_sorts = {"last_heard", "first_seen", "short_name", "unread_count"}
        col = sort if sort in valid_sorts else "last_heard"
        query += f" ORDER BY {col} DESC"
        rows = self._db.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def list_with_preview(self) -> List[dict]:
        """List contacts with last message preview for sidebar."""
        rows = self._db.execute("""
            SELECT c.*,
                   m.text AS last_message_text,
                   m.timestamp AS last_message_ts,
                   m.direction AS last_message_dir
            FROM contacts c
            LEFT JOIN messages m ON m.contact_id = c.id
                AND m.timestamp = (
                    SELECT MAX(m2.timestamp) FROM messages m2
                    WHERE m2.contact_id = c.id
                )
            ORDER BY c.last_heard DESC
        """).fetchall()
        return [dict(r) for r in rows]

    def set_ai_enabled(self, contact_id: str, value) -> None:
        """Set ai_enabled: None (inherit), 0 (off), or 1 (on)."""
        self._db.execute(
            "UPDATE contacts SET ai_enabled = ? WHERE id = ?",
            (value, contact_id),
        )
        self._db.commit()

    def cycle_ai_enabled(self, contact_id: str):
        """Cycle NULL→0→1→NULL. Returns new value."""
        current = self.get(contact_id)
        if current is None:
            return None
        cur_val = current["ai_enabled"]
        if cur_val is None:
            new_val = 0
        elif cur_val == 0:
            new_val = 1
        else:
            new_val = None
        self.set_ai_enabled(contact_id, new_val)
        return new_val

    def get_effective_ai(self, contact_id: str, global_default: bool) -> bool:
        """Resolve effective AI state: per-contact overrides global."""
        contact = self.get(contact_id)
        if contact is None:
            return global_default
        val = contact["ai_enabled"]
        if val is None:
            return global_default
        return bool(val)

    def increment_unread(self, contact_id: str) -> None:
        self._db.execute(
            "UPDATE contacts SET unread_count = unread_count + 1 WHERE id = ?",
            (contact_id,),
        )
        self._db.commit()

    def reset_unread(self, contact_id: str) -> None:
        self._db.execute(
            "UPDATE contacts SET unread_count = 0, last_read_at = ? WHERE id = ?",
            (time.time(), contact_id),
        )
        self._db.commit()

    def update_last_heard(self, contact_id: str, ts: float) -> None:
        self._db.execute(
            "UPDATE contacts SET last_heard = ? WHERE id = ?", (ts, contact_id)
        )
        self._db.commit()

    def total_unread(self) -> int:
        row = self._db.execute(
            "SELECT COALESCE(SUM(unread_count), 0) AS total FROM contacts"
        ).fetchone()
        return row["total"] if row else 0

    def count(self) -> int:
        row = self._db.execute("SELECT COUNT(*) AS cnt FROM contacts").fetchone()
        return row["cnt"] if row else 0

    def toggle_favorite(self, contact_id: str) -> int:
        row = self.get(contact_id)
        if not row:
            return 0
        new_val = 0 if row.get("is_favorite") else 1
        self._db.execute(
            "UPDATE contacts SET is_favorite = ? WHERE id = ?",
            (new_val, contact_id),
        )
        self._db.commit()
        return new_val

    def set_custom_name(self, contact_id: str, name: Optional[str]) -> bool:
        """Set or clear the custom display name. Returns True if the contact existed."""
        row = self.get(contact_id)
        if not row:
            return False
        clean = (name or "").strip() or None
        self._db.execute(
            "UPDATE contacts SET custom_name = ? WHERE id = ?",
            (clean, contact_id),
        )
        self._db.commit()
        return True

    def get_display_meta(self) -> Dict[str, dict]:
        """Compact {contact_id: {custom_name, is_favorite}} for poll-time UI.

        Only includes contacts that have at least one non-default field set,
        to keep the state payload small on large meshes.
        """
        rows = self._db.execute(
            "SELECT id, custom_name, is_favorite FROM contacts "
            "WHERE custom_name IS NOT NULL OR is_favorite = 1"
        ).fetchall()
        return {
            r["id"]: {
                "custom_name": r["custom_name"],
                "is_favorite": bool(r["is_favorite"]),
            }
            for r in rows
        }
