"""Tests for Relay !urgent / !priority / !sos / !mayday force-relay bypass.

The force prefix should:
1. Strip the prefix from relayed text (recipients see just the content).
2. Bypass the policy — even if policy.should_relay() returns False,
   force-prefixed messages cross.
3. Still be blocked for DMs (bridging private convos is a trust
   decision, not a priority decision).
4. Still pass through the dedup cache.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.policy import DisabledPolicy
from bridge.relay import Relay


class _Capture:
    """Collect send calls; always succeed."""

    def __init__(self):
        self.sends = []

    def __call__(self, dest, text, channel):
        self.sends.append((dest, text, channel))


class TestForceRelayPrefix(unittest.TestCase):
    def _make(self):
        cap = _Capture()
        # Use DisabledPolicy to prove the bypass overrides policy.
        relay = Relay(send_fn=cap, policy=DisabledPolicy())
        return relay, cap

    def test_urgent_bypasses_disabled_policy(self):
        relay, cap = self._make()
        relay.observe("meshtastic", "!abc", "!urgent building on fire",
                      channel=0, is_dm=False)
        self.assertEqual(len(cap.sends), 1)
        # Prefix should have been stripped
        self.assertIn("building on fire", cap.sends[0][1])
        self.assertNotIn("!urgent", cap.sends[0][1])

    def test_priority_bypasses(self):
        relay, cap = self._make()
        relay.observe("meshtastic", "!abc", "!priority incoming weather",
                      channel=0, is_dm=False)
        self.assertEqual(len(cap.sends), 1)
        self.assertNotIn("!priority", cap.sends[0][1])

    def test_sos_bypasses(self):
        relay, cap = self._make()
        relay.observe("meshtastic", "!abc", "!SOS — injured team member",
                      channel=0, is_dm=False)
        self.assertEqual(len(cap.sends), 1)
        self.assertIn("injured team member", cap.sends[0][1])

    def test_mayday_bypasses(self):
        relay, cap = self._make()
        relay.observe("meshtastic", "!abc", "!mayday losing altitude fast",
                      channel=0, is_dm=False)
        self.assertEqual(len(cap.sends), 1)
        self.assertIn("losing altitude fast", cap.sends[0][1])

    def test_case_insensitive(self):
        relay, cap = self._make()
        relay.observe("meshtastic", "!abc", "!URGENT all-caps test",
                      channel=0, is_dm=False)
        self.assertEqual(len(cap.sends), 1)
        self.assertIn("all-caps test", cap.sends[0][1])

    def test_prefix_with_colon_or_comma_stripped(self):
        relay, cap = self._make()
        relay.observe("meshtastic", "!abc", "!urgent: serious thing",
                      channel=0, is_dm=False)
        self.assertIn("serious thing", cap.sends[0][1])
        self.assertNotIn(":", cap.sends[0][1].split("]")[-1][:5])

    def test_bang_word_alone_drops(self):
        relay, cap = self._make()
        # Just "!urgent" with no content — nothing to forward
        relay.observe("meshtastic", "!abc", "!urgent", channel=0, is_dm=False)
        self.assertEqual(cap.sends, [])

    def test_dm_not_relayed_even_with_urgent(self):
        relay, cap = self._make()
        relay.observe("meshtastic", "!abc", "!urgent private msg",
                      channel=0, is_dm=True)
        self.assertEqual(cap.sends, [])

    def test_plain_text_still_blocked_by_disabled_policy(self):
        # Sanity: without the prefix, DisabledPolicy should still block.
        relay, cap = self._make()
        relay.observe("meshtastic", "!abc", "just some text",
                      channel=0, is_dm=False)
        self.assertEqual(cap.sends, [])

    def test_force_relay_dedup_still_applies(self):
        relay, cap = self._make()
        relay.observe("meshtastic", "!abc", "!urgent same msg",
                      channel=0, is_dm=False)
        relay.observe("meshtastic", "!abc", "!urgent same msg",
                      channel=0, is_dm=False)
        # Dedup hash is (sender, stripped-text) — second call hits cache.
        self.assertEqual(len(cap.sends), 1)

    def test_non_prefix_bang_word_not_triggered(self):
        # "!unrelated" shouldn't match the bypass list.
        relay, cap = self._make()
        relay.observe("meshtastic", "!abc", "!unrelated hello",
                      channel=0, is_dm=False)
        self.assertEqual(cap.sends, [])

    def test_mid_sentence_prefix_ignored(self):
        # Only prefixes at the start count.
        relay, cap = self._make()
        relay.observe("meshtastic", "!abc", "heading south, !urgent later",
                      channel=0, is_dm=False)
        self.assertEqual(cap.sends, [])


if __name__ == "__main__":
    unittest.main()
