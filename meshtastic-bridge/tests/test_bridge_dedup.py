"""Tests for bridge.dedup — relay dedup cache semantics."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.dedup import RelayDedupCache


class TestRelayDedupCache(unittest.TestCase):
    def test_unseen_by_default(self):
        c = RelayDedupCache()
        self.assertFalse(c.seen("meshtastic", "meshcore", "!abc", "hi"))

    def test_recorded_seen(self):
        c = RelayDedupCache()
        c.record("meshtastic", "meshcore", "!abc", "hi")
        self.assertTrue(c.seen("meshtastic", "meshcore", "!abc", "hi"))

    def test_different_sender_not_seen(self):
        c = RelayDedupCache()
        c.record("meshtastic", "meshcore", "!abc", "hi")
        self.assertFalse(c.seen("meshtastic", "meshcore", "!xyz", "hi"))

    def test_different_text_not_seen(self):
        c = RelayDedupCache()
        c.record("meshtastic", "meshcore", "!abc", "hi")
        self.assertFalse(c.seen("meshtastic", "meshcore", "!abc", "different"))

    def test_different_direction_not_seen(self):
        # Dedup is direction-aware — same (sender, text) going the other
        # way is a legitimate new event.
        c = RelayDedupCache()
        c.record("meshtastic", "meshcore", "!abc", "hi")
        self.assertFalse(c.seen("meshcore", "meshtastic", "!abc", "hi"))

    def test_whitespace_insensitive(self):
        # "hi" and "hi  " should dedup — trailing whitespace shouldn't
        # defeat the guard.
        c = RelayDedupCache()
        c.record("meshtastic", "meshcore", "!abc", "hi")
        self.assertTrue(c.seen("meshtastic", "meshcore", "!abc", "hi  "))

    def test_ttl_expiry(self):
        c = RelayDedupCache(ttl_seconds=0.05)
        c.record("meshtastic", "meshcore", "!abc", "hi")
        self.assertTrue(c.seen("meshtastic", "meshcore", "!abc", "hi"))
        time.sleep(0.07)
        self.assertFalse(c.seen("meshtastic", "meshcore", "!abc", "hi"))

    def test_cleanup_evicts_stale(self):
        c = RelayDedupCache(ttl_seconds=0.05)
        c.record("meshtastic", "meshcore", "!abc", "hi")
        c.record("meshtastic", "meshcore", "!def", "yo")
        self.assertEqual(c.size(), 2)
        time.sleep(0.07)
        removed = c.cleanup()
        self.assertEqual(removed, 2)
        self.assertEqual(c.size(), 0)

    def test_cleanup_preserves_fresh(self):
        c = RelayDedupCache(ttl_seconds=60)
        c.record("meshtastic", "meshcore", "!abc", "hi")
        self.assertEqual(c.cleanup(), 0)
        self.assertEqual(c.size(), 1)


if __name__ == "__main__":
    unittest.main()
