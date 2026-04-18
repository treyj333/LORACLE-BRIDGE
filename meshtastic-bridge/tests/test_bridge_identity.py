"""Tests for bridge.identity — sender prefix formatting + loop-guard recognition."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.identity import format_bridged, looks_bridged


class TestFormatBridged(unittest.TestCase):
    def test_meshtastic_long_name(self):
        self.assertEqual(
            format_bridged("meshtastic", "Alice", "hello"),
            "from meshtastic (Alice): hello",
        )

    def test_meshcore_long_name(self):
        self.assertEqual(
            format_bridged("meshcore", "NODE-01", "hi"),
            "from meshcore (NODE-01): hi",
        )

    def test_short_code_alias_resolves(self):
        # Callers that happen to pass "mt" / "mc" still get the long form.
        self.assertEqual(
            format_bridged("mt", "a", "x"),
            "from meshtastic (a): x",
        )
        self.assertEqual(
            format_bridged("mc", "a", "x"),
            "from meshcore (a): x",
        )

    def test_unknown_protocol_falls_back_verbatim(self):
        # Phase-forward: a new protocol plugs in cleanly without touching identity.py.
        self.assertEqual(
            format_bridged("reticulum", "bob", "x"),
            "from reticulum (bob): x",
        )

    def test_preserves_body_exactly(self):
        # No trimming, no escaping — the body is user data, pass it through.
        body = "  leading and trailing  "
        self.assertIn(body, format_bridged("meshtastic", "a", body))


class TestLooksBridged(unittest.TestCase):
    def test_true_for_meshtastic_prefix(self):
        self.assertTrue(looks_bridged("from meshtastic (Alice): hello"))

    def test_true_for_meshcore_prefix(self):
        self.assertTrue(looks_bridged("from meshcore (NODE): hi"))

    def test_false_for_plain_text(self):
        self.assertFalse(looks_bridged("hello world"))

    def test_false_for_empty(self):
        self.assertFalse(looks_bridged(""))
        self.assertFalse(looks_bridged(None))

    def test_false_when_prefix_mid_sentence(self):
        # The loop guard only matches at the start of the text.
        self.assertFalse(looks_bridged("hey from meshtastic (Alice): said hi"))

    def test_false_for_unknown_proto(self):
        # Only registered protocol names trigger the guard — avoids false
        # positives on "from X (...)" that users might type.
        self.assertFalse(looks_bridged("from reticulum (foo): bar"))

    def test_false_when_colon_space_missing(self):
        # Trailing ": " is required — prevents "from meshtastic (foo)hi" matching.
        self.assertFalse(looks_bridged("from meshtastic (foo)hi"))

    def test_false_when_parens_missing(self):
        # The name MUST be parenthesised — plain "from meshtastic Alice: hi"
        # could easily be user prose and shouldn't trip the loop guard.
        self.assertFalse(looks_bridged("from meshtastic Alice: hi"))


if __name__ == "__main__":
    unittest.main()
