"""Tests for db.bridge_events — BridgeEventStore persistence."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.schema import init_db
from db.bridge_events import BridgeEventStore


class TestBridgeEventStore(unittest.TestCase):
    def setUp(self):
        self.db = init_db(":memory:")
        self.store = BridgeEventStore(self.db)

    def test_insert_and_recent(self):
        self.store.insert(
            source_protocol="meshtastic",
            dest_protocol="meshcore",
            channel=0,
            sender="!abc",
            text="[mt-Alice] hello",
            outcome="relayed",
            sender_display="Alice",
        )
        rows = self.store.recent(limit=10)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["source_protocol"], "meshtastic")
        self.assertEqual(r["dest_protocol"], "meshcore")
        self.assertEqual(r["channel"], 0)
        self.assertEqual(r["sender"], "!abc")
        self.assertEqual(r["sender_display"], "Alice")
        self.assertEqual(r["outcome"], "relayed")
        self.assertIn("hello", r["text"])

    def test_recent_orders_newest_first(self):
        t0 = time.time()
        self.store.insert(
            source_protocol="meshtastic", dest_protocol="meshcore",
            channel=0, sender="a", text="first", outcome="relayed",
            timestamp=t0,
        )
        self.store.insert(
            source_protocol="meshtastic", dest_protocol="meshcore",
            channel=0, sender="b", text="second", outcome="relayed",
            timestamp=t0 + 1,
        )
        rows = self.store.recent(limit=10)
        self.assertEqual(rows[0]["sender"], "b")
        self.assertEqual(rows[1]["sender"], "a")

    def test_filter_by_outcome(self):
        self.store.insert(
            source_protocol="mt", dest_protocol="mc", channel=0,
            sender="a", text="x", outcome="relayed",
        )
        self.store.insert(
            source_protocol="mt", dest_protocol="mc", channel=0,
            sender="b", text="y", outcome="rate_limited",
        )
        relayed = self.store.recent(outcome="relayed")
        self.assertEqual(len(relayed), 1)
        self.assertEqual(relayed[0]["sender"], "a")

    def test_filter_by_since(self):
        past = time.time() - 100
        self.store.insert(
            source_protocol="mt", dest_protocol="mc", channel=0,
            sender="old", text="x", outcome="relayed",
            timestamp=past,
        )
        self.store.insert(
            source_protocol="mt", dest_protocol="mc", channel=0,
            sender="new", text="x", outcome="relayed",
            timestamp=time.time(),
        )
        rows = self.store.recent(since=past + 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sender"], "new")

    def test_count_groups_by_outcome(self):
        self.store.insert(
            source_protocol="mt", dest_protocol="mc", channel=0,
            sender="a", text="x", outcome="relayed",
        )
        self.store.insert(
            source_protocol="mt", dest_protocol="mc", channel=0,
            sender="b", text="y", outcome="relayed",
        )
        self.store.insert(
            source_protocol="mt", dest_protocol="mc", channel=0,
            sender="c", text="z", outcome="blocked",
        )
        counts = self.store.count()
        self.assertEqual(counts.get("relayed"), 2)
        self.assertEqual(counts.get("blocked"), 1)

    def test_prune_removes_stale(self):
        stale = time.time() - (40 * 86400)
        self.store.insert(
            source_protocol="mt", dest_protocol="mc", channel=0,
            sender="a", text="x", outcome="relayed", timestamp=stale,
        )
        self.store.insert(
            source_protocol="mt", dest_protocol="mc", channel=0,
            sender="b", text="y", outcome="relayed",
        )
        removed = self.store.prune(days=30)
        self.assertEqual(removed, 1)
        rows = self.store.recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sender"], "b")

    def test_invalid_outcome_rejected_by_schema(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.insert(
                source_protocol="mt", dest_protocol="mc", channel=0,
                sender="a", text="x", outcome="bogus",
            )

    def test_force_relay_flag(self):
        self.store.insert(
            source_protocol="mt", dest_protocol="mc", channel=0,
            sender="a", text="!urgent x", outcome="relayed",
            force_relay=True,
        )
        rows = self.store.recent()
        self.assertEqual(rows[0]["force_relay"], 1)

    def test_text_truncation(self):
        huge = "x" * 5000
        self.store.insert(
            source_protocol="mt", dest_protocol="mc", channel=0,
            sender="a", text=huge, outcome="relayed",
        )
        rows = self.store.recent()
        self.assertLessEqual(len(rows[0]["text"]), 2000)


if __name__ == "__main__":
    unittest.main()
