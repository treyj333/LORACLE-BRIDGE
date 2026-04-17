"""Integration-style tests for bridge.relay — observe() with a mock send_fn."""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.policy import AlwaysRelay, ChannelAllowlist, DisabledPolicy
from bridge.relay import Relay


class TestRelayObserve(unittest.TestCase):
    def _make_relay(self, policy=None, identity_fn=None, on_relay=None):
        sends = []
        def fake_send(dest, text, channel):
            sends.append((dest, text, channel))
        relay = Relay(
            send_fn=fake_send,
            policy=policy if policy is not None else AlwaysRelay(),
            identity_fn=identity_fn,
            on_relay=on_relay,
        )
        return relay, sends

    def test_relays_to_other_protocol(self):
        relay, sends = self._make_relay()
        delivered = relay.observe("meshtastic", "!abc", "hello", channel=0, is_dm=False)
        self.assertEqual(delivered, ["meshcore"])
        self.assertEqual(len(sends), 1)
        dest, text, channel = sends[0]
        self.assertEqual(dest, "meshcore")
        self.assertEqual(channel, 0)
        self.assertTrue(text.startswith("[mt-"))
        self.assertIn("hello", text)

    def test_never_loops_to_source(self):
        relay, sends = self._make_relay()
        relay.observe("meshcore", "abcdef", "yo", channel=0, is_dm=False)
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][0], "meshtastic")  # meshcore→meshtastic, not back

    def test_dm_not_relayed(self):
        relay, sends = self._make_relay()
        delivered = relay.observe("meshtastic", "!abc", "secret", channel=0, is_dm=True)
        self.assertEqual(delivered, [])
        self.assertEqual(sends, [])

    def test_empty_text_not_relayed(self):
        relay, sends = self._make_relay()
        relay.observe("meshtastic", "!abc", "", channel=0, is_dm=False)
        relay.observe("meshtastic", "!abc", "   ", channel=0, is_dm=False)
        self.assertEqual(sends, [])

    def test_already_bridged_text_not_relayed(self):
        # Loop-guard #1: text with a bridge prefix is not re-relayed.
        relay, sends = self._make_relay()
        relay.observe(
            "meshtastic", "!abc",
            "[mc-NODE] already bridged",
            channel=0, is_dm=False,
        )
        self.assertEqual(sends, [])

    def test_policy_disabled_blocks(self):
        relay, sends = self._make_relay(policy=DisabledPolicy())
        relay.observe("meshtastic", "!abc", "hello", channel=0, is_dm=False)
        self.assertEqual(sends, [])

    def test_channel_allowlist(self):
        relay, sends = self._make_relay(
            policy=ChannelAllowlist([("meshtastic", 0)])
        )
        # Allowed
        relay.observe("meshtastic", "!abc", "yes", channel=0, is_dm=False)
        # Blocked (wrong channel)
        relay.observe("meshtastic", "!abc", "no", channel=1, is_dm=False)
        self.assertEqual(len(sends), 1)
        self.assertIn("yes", sends[0][1])

    def test_dedup_within_ttl(self):
        # Loop-guard #2: same (sender, text) within TTL doesn't re-relay.
        relay, sends = self._make_relay()
        relay.observe("meshtastic", "!abc", "hello", channel=0, is_dm=False)
        relay.observe("meshtastic", "!abc", "hello", channel=0, is_dm=False)
        self.assertEqual(len(sends), 1)
        # But a different message from same sender should go through
        relay.observe("meshtastic", "!abc", "different", channel=0, is_dm=False)
        self.assertEqual(len(sends), 2)

    def test_identity_fn_used(self):
        relay, sends = self._make_relay(
            identity_fn=lambda proto, sender: "CUSTOM-" + sender[-3:]
        )
        relay.observe("meshtastic", "!abc12345", "hi", channel=0, is_dm=False)
        self.assertIn("[mt-CUSTOM-345]", sends[0][1])

    def test_default_identity_uses_last_6(self):
        relay, sends = self._make_relay()
        relay.observe("meshtastic", "!abc12345", "hi", channel=0, is_dm=False)
        self.assertIn("c12345", sends[0][1])  # last 6 chars of !abc12345

    def test_send_failure_does_not_break_loop(self):
        # A failing send must not crash observe(); counters and dedup
        # should not count the failure as delivered.
        calls = []
        def flaky_send(dest, text, channel):
            calls.append((dest, text))
            if len(calls) == 1:
                raise RuntimeError("radio angry")
        relay = Relay(send_fn=flaky_send, policy=AlwaysRelay())
        delivered = relay.observe("meshtastic", "!abc", "hi", 0, False)
        self.assertEqual(delivered, [])
        # Same message again: dedup should NOT block because we never
        # recorded a successful relay.
        delivered2 = relay.observe("meshtastic", "!abc", "hi", 0, False)
        self.assertEqual(delivered2, ["meshcore"])

    def test_on_relay_hook_fires(self):
        events = []
        relay, sends = self._make_relay(on_relay=lambda evt: events.append(evt))
        relay.observe("meshtastic", "!abc", "hi", channel=0, is_dm=False)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "meshtastic")
        self.assertEqual(events[0]["dest"], "meshcore")
        self.assertEqual(events[0]["sender"], "!abc")

    def test_stats_counts(self):
        relay, _ = self._make_relay(policy=DisabledPolicy())
        relay.observe("meshtastic", "!abc", "hi", 0, False)
        s = relay.stats()
        self.assertEqual(s["relayed"], 0)
        self.assertEqual(s["dropped"], 1)

    def test_set_policy_hot_swap(self):
        relay, sends = self._make_relay(policy=DisabledPolicy())
        relay.observe("meshtastic", "!abc", "hi", 0, False)
        self.assertEqual(sends, [])
        relay.set_policy(AlwaysRelay())
        relay.observe("meshtastic", "!abc", "hi2", 0, False)
        self.assertEqual(len(sends), 1)


if __name__ == "__main__":
    unittest.main()
