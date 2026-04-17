"""Tests for bridge.identity — sender prefix formatting + loop-guard recognition."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.identity import format_bridged, looks_bridged


class TestFormatBridged(unittest.TestCase):
    def test_meshtastic_to_short_code(self):
        self.assertEqual(
            format_bridged("meshtastic", "Alice", "hello"),
            "[mt-Alice] hello",
        )

    def test_meshcore_to_short_code(self):
        self.assertEqual(
            format_bridged("meshcore", "NODE-01", "hi"),
            "[mc-NODE-01] hi",
        )

    def test_unknown_protocol_falls_back_to_prefix(self):
        # Phase-forward: a new protocol plugs in cleanly without touching identity.py.
        self.assertEqual(
            format_bridged("reticulum", "bob", "x"),
            "[re-bob] x",
        )

    def test_preserves_body_exactly(self):
        # No trimming, no escaping — the body is user data, pass it through.
        body = "  leading and trailing  "
        self.assertIn(body, format_bridged("meshtastic", "a", body))


class TestLooksBridged(unittest.TestCase):
    def test_true_for_mt_prefix(self):
        self.assertTrue(looks_bridged("[mt-Alice] hello"))

    def test_true_for_mc_prefix(self):
        self.assertTrue(looks_bridged("[mc-NODE] hi"))

    def test_false_for_plain_text(self):
        self.assertFalse(looks_bridged("hello world"))

    def test_false_for_empty(self):
        self.assertFalse(looks_bridged(""))
        self.assertFalse(looks_bridged(None))

    def test_false_when_prefix_mid_sentence(self):
        # The loop guard only matches at the start of the text.
        self.assertFalse(looks_bridged("hey [mt-Alice] said hi"))

    def test_false_for_unknown_proto(self):
        # Only registered short codes trigger the guard — avoids false
        # positives on [xx-...] that users might type.
        self.assertFalse(looks_bridged("[xx-foo] bar"))

    def test_false_when_space_missing(self):
        # Trailing space is required — prevents "[mt-foo]inline" matching.
        self.assertFalse(looks_bridged("[mt-foo]inline"))


if __name__ == "__main__":
    unittest.main()
