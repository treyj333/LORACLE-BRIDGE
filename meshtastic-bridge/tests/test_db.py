"""Tests for the db/ SQLite storage layer."""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.schema import init_db, migrate_from_json
from db.contacts import ContactStore
from db.messages import MessageStore
from db.settings import SettingsStore


class TestSchema(unittest.TestCase):
    def test_init_db_creates_tables(self):
        with tempfile.TemporaryDirectory() as d:
            db = init_db(os.path.join(d, "test.db"))
            tables = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            names = {r["name"] for r in tables}
            self.assertIn("contacts", names)
            self.assertIn("messages", names)
            self.assertIn("settings", names)
            db.close()

    def test_init_db_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "test.db")
            db1 = init_db(path)
            db1.close()
            db2 = init_db(path)
            db2.close()


class TestMigration(unittest.TestCase):
    def test_migrate_from_json(self):
        with tempfile.TemporaryDirectory() as d:
            json_path = os.path.join(d, "settings.json")
            with open(json_path, "w") as f:
                json.dump({"ai_replies": False, "second_radio": "meshcore:serial:/dev/ttyUSB1"}, f)

            db = init_db(os.path.join(d, "test.db"))
            migrate_from_json(db, json_path)

            settings = SettingsStore(db)
            self.assertEqual(settings.get("ai_replies"), False)
            self.assertEqual(settings.get("second_radio"), "meshcore:serial:/dev/ttyUSB1")

            # Original file should be renamed to .bak
            self.assertFalse(os.path.exists(json_path))
            self.assertTrue(os.path.exists(json_path + ".bak"))
            db.close()

    def test_migrate_missing_json_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            db = init_db(os.path.join(d, "test.db"))
            migrate_from_json(db, os.path.join(d, "nonexistent.json"))
            db.close()


class TestContactStore(unittest.TestCase):
    def setUp(self):
        self.db = init_db(":memory:")
        self.store = ContactStore(self.db)

    def tearDown(self):
        self.db.close()

    def test_upsert_creates_contact(self):
        self.store.upsert("mt:!abc123", "meshtastic", "!abc123", "abc123")
        contact = self.store.get("mt:!abc123")
        self.assertIsNotNone(contact)
        self.assertEqual(contact["protocol"], "meshtastic")
        self.assertEqual(contact["short_name"], "abc123")

    def test_upsert_updates_existing(self):
        self.store.upsert("mt:!abc123", "meshtastic", "!abc123", "abc123")
        self.store.upsert("mt:!abc123", "meshtastic", "!abc123", "abc123",
                          long_name="Hiker", rssi=-80)
        contact = self.store.get("mt:!abc123")
        self.assertEqual(contact["long_name"], "Hiker")
        self.assertEqual(contact["last_rssi"], -80)

    def test_upsert_preserves_long_name(self):
        self.store.upsert("mt:!abc", "meshtastic", "!abc", "abc", long_name="Alice")
        self.store.upsert("mt:!abc", "meshtastic", "!abc", "abc", long_name=None)
        contact = self.store.get("mt:!abc")
        self.assertEqual(contact["long_name"], "Alice")

    def test_list_all(self):
        self.store.upsert("mt:!a", "meshtastic", "!a", "aaa")
        self.store.upsert("mc:b", "meshcore", "b", "bbb")
        all_contacts = self.store.list_all()
        self.assertEqual(len(all_contacts), 2)

    def test_list_filter_dm(self):
        self.store.upsert("mt:!a", "meshtastic", "!a", "aaa")
        self.store.upsert("mt:ch0", "meshtastic", "ch0", "LongFast",
                          is_channel=True, channel_idx=0)
        dms = self.store.list_all(filter_type="dm")
        self.assertEqual(len(dms), 1)
        channels = self.store.list_all(filter_type="channel")
        self.assertEqual(len(channels), 1)

    def test_ai_enabled_cycle(self):
        self.store.upsert("mt:!a", "meshtastic", "!a", "aaa")
        # NULL → 0
        val = self.store.cycle_ai_enabled("mt:!a")
        self.assertEqual(val, 0)
        # 0 → 1
        val = self.store.cycle_ai_enabled("mt:!a")
        self.assertEqual(val, 1)
        # 1 → NULL
        val = self.store.cycle_ai_enabled("mt:!a")
        self.assertIsNone(val)

    def test_effective_ai_inherits_global(self):
        self.store.upsert("mt:!a", "meshtastic", "!a", "aaa")
        self.assertTrue(self.store.get_effective_ai("mt:!a", True))
        self.assertFalse(self.store.get_effective_ai("mt:!a", False))

    def test_effective_ai_override(self):
        self.store.upsert("mt:!a", "meshtastic", "!a", "aaa")
        self.store.set_ai_enabled("mt:!a", 0)
        self.assertFalse(self.store.get_effective_ai("mt:!a", True))
        self.store.set_ai_enabled("mt:!a", 1)
        self.assertTrue(self.store.get_effective_ai("mt:!a", False))

    def test_unread_count(self):
        self.store.upsert("mt:!a", "meshtastic", "!a", "aaa")
        self.store.increment_unread("mt:!a")
        self.store.increment_unread("mt:!a")
        contact = self.store.get("mt:!a")
        self.assertEqual(contact["unread_count"], 2)
        self.store.reset_unread("mt:!a")
        contact = self.store.get("mt:!a")
        self.assertEqual(contact["unread_count"], 0)

    def test_total_unread(self):
        self.store.upsert("mt:!a", "meshtastic", "!a", "aaa")
        self.store.upsert("mt:!b", "meshtastic", "!b", "bbb")
        self.store.increment_unread("mt:!a")
        self.store.increment_unread("mt:!a")
        self.store.increment_unread("mt:!b")
        self.assertEqual(self.store.total_unread(), 3)

    def test_list_with_preview(self):
        self.store.upsert("mt:!a", "meshtastic", "!a", "aaa")
        msgs = MessageStore(self.db)
        msgs.insert("mt:!a", "in", "human", "hello", "meshtastic")
        result = self.store.list_with_preview()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["last_message_text"], "hello")


class TestMessageStore(unittest.TestCase):
    def setUp(self):
        self.db = init_db(":memory:")
        self.contacts = ContactStore(self.db)
        self.store = MessageStore(self.db)
        self.contacts.upsert("mt:!abc", "meshtastic", "!abc", "abc123")

    def tearDown(self):
        self.db.close()

    def test_insert_and_get_thread(self):
        self.store.insert("mt:!abc", "in", "human", "hello", "meshtastic")
        self.store.insert("mt:!abc", "out", "ai", "hi there", "meshtastic")
        thread = self.store.get_thread("mt:!abc")
        self.assertEqual(len(thread), 2)
        self.assertEqual(thread[0]["text"], "hello")  # oldest first
        self.assertEqual(thread[1]["text"], "hi there")

    def test_get_recent(self):
        self.store.insert("mt:!abc", "in", "human", "msg1", "meshtastic")
        self.store.insert("mt:!abc", "out", "ai", "msg2", "meshtastic")
        recent = self.store.get_recent(limit=10)
        self.assertEqual(len(recent), 2)

    def test_count(self):
        self.store.insert("mt:!abc", "in", "human", "a", "meshtastic")
        self.store.insert("mt:!abc", "in", "human", "b", "meshtastic")
        self.assertEqual(self.store.count_by_contact("mt:!abc"), 2)
        self.assertEqual(self.store.count_total(), 2)

    def test_prune_cap(self):
        for i in range(10):
            self.store.insert("mt:!abc", "in", "human", f"msg {i}", "meshtastic",
                              timestamp=time.time() - i)
        pruned = self.store.prune(max_per_contact=5, max_age_days=9999)
        self.assertEqual(self.store.count_by_contact("mt:!abc"), 5)
        self.assertGreater(pruned, 0)

    def test_prune_age(self):
        old_ts = time.time() - (100 * 86400)  # 100 days ago
        self.store.insert("mt:!abc", "in", "human", "old msg", "meshtastic",
                          timestamp=old_ts)
        self.store.insert("mt:!abc", "in", "human", "new msg", "meshtastic")
        pruned = self.store.prune(max_per_contact=9999, max_age_days=90)
        self.assertEqual(self.store.count_by_contact("mt:!abc"), 1)

    def test_delete_all(self):
        self.store.insert("mt:!abc", "in", "human", "a", "meshtastic")
        self.store.insert("mt:!abc", "in", "human", "b", "meshtastic")
        deleted = self.store.delete_all()
        self.assertEqual(deleted, 2)
        self.assertEqual(self.store.count_total(), 0)

    def test_delivery_status(self):
        mid = self.store.insert("mt:!abc", "out", "human", "test", "meshtastic",
                                delivery_status="queued")
        self.store.update_delivery_status(mid, "sent")
        thread = self.store.get_thread("mt:!abc")
        self.assertEqual(thread[0]["delivery_status"], "sent")


class TestSettingsStore(unittest.TestCase):
    def setUp(self):
        self.db = init_db(":memory:")
        self.store = SettingsStore(self.db)

    def tearDown(self):
        self.db.close()

    def test_get_set(self):
        self.store.set("theme", "dark")
        self.assertEqual(self.store.get("theme"), "dark")

    def test_get_default(self):
        self.assertEqual(self.store.get("missing", "fallback"), "fallback")

    def test_set_overwrites(self):
        self.store.set("key", "v1")
        self.store.set("key", "v2")
        self.assertEqual(self.store.get("key"), "v2")

    def test_json_values(self):
        self.store.set("config", {"a": 1, "b": [2, 3]})
        val = self.store.get("config")
        self.assertEqual(val, {"a": 1, "b": [2, 3]})

    def test_get_all(self):
        self.store.set("k1", "v1")
        self.store.set("k2", 42)
        all_settings = self.store.get_all()
        self.assertEqual(all_settings["k1"], "v1")
        self.assertEqual(all_settings["k2"], 42)

    def test_delete(self):
        self.store.set("k1", "v1")
        self.store.delete("k1")
        self.assertIsNone(self.store.get("k1"))


if __name__ == "__main__":
    unittest.main()
