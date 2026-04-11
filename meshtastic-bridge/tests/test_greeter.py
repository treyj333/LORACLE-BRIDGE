"""Tests for GreeterService — auto-greeting new mesh nodes (Phase 5)."""

import json
import os
import tempfile
import time
import unittest
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from greeter import GreeterService, DEFAULT_GREETING


class TestIsConcreteNode(unittest.TestCase):
    def test_valid_node_id(self):
        self.assertTrue(GreeterService._is_concrete_node("!12345678"))
        self.assertTrue(GreeterService._is_concrete_node("!abcdef01"))

    def test_broadcast_rejected(self):
        self.assertFalse(GreeterService._is_concrete_node("^all"))
        self.assertFalse(GreeterService._is_concrete_node("!ffffffff"))
        self.assertFalse(GreeterService._is_concrete_node("!FFFFFFFF"))
        self.assertFalse(GreeterService._is_concrete_node("broadcast"))

    def test_empty_string(self):
        self.assertFalse(GreeterService._is_concrete_node(""))

    def test_no_exclamation_mark(self):
        self.assertFalse(GreeterService._is_concrete_node("12345678"))

    def test_too_short(self):
        self.assertFalse(GreeterService._is_concrete_node("!12345"))

    def test_non_hex(self):
        self.assertFalse(GreeterService._is_concrete_node("!xyzxyzxy"))

    def test_non_string(self):
        self.assertFalse(GreeterService._is_concrete_node(None))
        self.assertFalse(GreeterService._is_concrete_node(12345678))


class TestGreeterService(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_path = os.path.join(self.tmpdir, "greeted.json")
        self.greeter = GreeterService(
            path=self.state_path,
            grace_period_s=0,  # disable grace for tests
            send_interval_s=0,  # disable rate limit for tests
        )

    def test_default_message(self):
        self.assertEqual(self.greeter.message, DEFAULT_GREETING)

    def test_maybe_greet_new_node(self):
        self.greeter.maybe_greet("!aabbccdd")
        stats = self.greeter.stats()
        self.assertEqual(stats["queued"], 1)

    def test_maybe_greet_already_greeted(self):
        self.greeter.mark_known("!aabbccdd")
        self.greeter.maybe_greet("!aabbccdd")
        stats = self.greeter.stats()
        self.assertEqual(stats["queued"], 0)

    def test_maybe_greet_broadcast_ignored(self):
        self.greeter.maybe_greet("^all")
        self.assertEqual(self.greeter.stats()["queued"], 0)

    def test_maybe_greet_self_ignored(self):
        self.greeter.set_self_id("!11111111")
        self.greeter.maybe_greet("!11111111")
        self.assertEqual(self.greeter.stats()["queued"], 0)

    def test_seed_from_nodedb(self):
        self.greeter.seed_from_nodedb(["!aabbccdd", "!eeff0011", "^all"])
        stats = self.greeter.stats()
        self.assertEqual(stats["greeted_count"], 2)  # ^all filtered
        self.assertTrue(stats["first_seed_done"])

    def test_seed_idempotent(self):
        self.greeter.seed_from_nodedb(["!aabbccdd"])
        self.greeter.seed_from_nodedb(["!eeff0011"])
        # Second call should be no-op
        self.assertEqual(self.greeter.stats()["greeted_count"], 1)

    def test_pump_sends_greeting(self):
        self.greeter.maybe_greet("!aabbccdd")
        interface = MagicMock()
        self.greeter.pump(interface)
        interface.sendText.assert_called_once()
        call_args = interface.sendText.call_args
        self.assertIn("!aabbccdd", str(call_args))
        self.assertEqual(self.greeter.stats()["sent_this_session"], 1)

    def test_pump_no_interface_noop(self):
        self.greeter.maybe_greet("!aabbccdd")
        self.greeter.pump(None)
        self.assertEqual(self.greeter.stats()["sent_this_session"], 0)

    def test_state_persists(self):
        self.greeter.mark_known("!aabbccdd")
        # Create a new greeter from same path
        greeter2 = GreeterService(path=self.state_path, grace_period_s=0)
        self.assertEqual(greeter2.stats()["greeted_count"], 1)

    def test_stats_shape(self):
        stats = self.greeter.stats()
        expected_keys = {"enabled", "message", "greeted_count", "queued",
                         "sent_this_session", "self_id", "last_send_ts",
                         "first_seed_done", "grace_remaining_s"}
        self.assertTrue(expected_keys.issubset(stats.keys()))


if __name__ == "__main__":
    unittest.main()
