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
        self.assertTrue(text.startswith("from meshtastic ("))
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
            "from meshcore (NODE): already bridged",
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
        self.assertIn("from meshtastic (CUSTOM-345):", sends[0][1])

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

    def test_stats_by_direction_always_present(self):
        """A fresh relay must expose both canonical direction keys at zero so
        the BRIDGE UI can render symmetric MT↔MC columns from first paint —
        without them, the column for a silent direction would collapse."""
        relay, _ = self._make_relay()
        s = relay.stats()
        self.assertIn("by_direction", s)
        self.assertEqual(
            set(s["by_direction"].keys()),
            {"meshtastic->meshcore", "meshcore->meshtastic"},
        )
        for d, counts in s["by_direction"].items():
            self.assertEqual(
                counts,
                {"relayed": 0, "dropped": 0, "rate_limited": 0},
                msg=d,
            )

    def test_stats_by_direction_splits_relay_and_drop(self):
        """One MT→MC relay + one MC→MT policy-drop should land in the right
        buckets, and the aggregate totals should still add up."""
        # Allowlist only meshtastic's ch0 — so MT traffic on ch0 relays and
        # MC traffic gets policy-dropped regardless of channel.
        relay, _ = self._make_relay(policy=ChannelAllowlist([("meshtastic", 0)]))
        relay.observe("meshtastic", "!abc", "hi", 0, False)       # MT→MC relayed
        relay.observe("meshcore", "abcdef", "nope", 0, False)     # MC→MT dropped (policy)
        s = relay.stats()
        mt_mc = s["by_direction"]["meshtastic->meshcore"]
        mc_mt = s["by_direction"]["meshcore->meshtastic"]
        self.assertEqual(mt_mc["relayed"], 1)
        self.assertEqual(mt_mc["dropped"], 0)
        self.assertEqual(mc_mt["relayed"], 0)
        self.assertEqual(mc_mt["dropped"], 1)
        self.assertEqual(s["relayed"], 1)
        self.assertEqual(s["dropped"], 1)


class TestRelaySelftestAndDropReasons(unittest.TestCase):
    """v2.5 — the selftest path must exercise the full guard chain without
    hitting radios, and every drop site must populate
    ``_last_drop_reason_by_direction`` so the Health panel can diagnose."""

    def _make_relay(self, policy=None, on_relay=None, rate_limiter=None):
        sends = []
        def fake_send(dest, text, channel):
            sends.append((dest, text, channel))
        relay = Relay(
            send_fn=fake_send,
            policy=policy if policy is not None else AlwaysRelay(),
            on_relay=on_relay,
            rate_limiter=rate_limiter,
        )
        return relay, sends

    def test_is_selftest_skips_send_fn(self):
        relay, sends = self._make_relay()
        delivered = relay.observe(
            "meshtastic", "__selftest__", "probe", 0, False,
            is_selftest=True,
        )
        self.assertEqual(delivered, ["meshcore"])
        self.assertEqual(sends, [], "send_fn must not fire for selftest")

    def test_is_selftest_still_calls_on_relay_with_flag(self):
        events = []
        relay, _ = self._make_relay(on_relay=lambda e: events.append(e))
        relay.observe(
            "meshtastic", "__selftest__", "probe", 0, False,
            is_selftest=True,
        )
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["is_selftest"])

    def test_is_selftest_does_not_bump_last_observe_at(self):
        relay, _ = self._make_relay()
        before = relay._last_observe_at
        relay.observe(
            "meshtastic", "__selftest__", "probe", 0, False,
            is_selftest=True,
        )
        self.assertEqual(
            relay._last_observe_at, before,
            "selftest traffic must not update the wiring-alive indicator",
        )

    def test_real_observe_bumps_last_observe_at(self):
        relay, _ = self._make_relay()
        self.assertEqual(relay._last_observe_at, 0.0)
        relay.observe("meshtastic", "!abc", "hi", 0, False)
        self.assertGreater(relay._last_observe_at, 0.0)

    def test_drop_reason_policy_disabled(self):
        relay, _ = self._make_relay(policy=DisabledPolicy())
        relay.observe("meshtastic", "!abc", "hi", 0, False)
        reasons = relay._last_drop_reason_by_direction
        self.assertEqual(
            reasons.get("meshtastic->meshcore"), "policy:disabled",
        )

    def test_drop_reason_dm_blocked(self):
        relay, _ = self._make_relay()
        relay.observe("meshtastic", "!abc", "secret", 0, True)
        reasons = relay._last_drop_reason_by_direction
        self.assertEqual(
            reasons.get("meshtastic->meshcore"), "dm_not_relayed",
        )

    def test_drop_reason_dedup(self):
        relay, _ = self._make_relay()
        relay.observe("meshtastic", "!abc", "hi", 0, False)  # first succeeds
        relay.observe("meshtastic", "!abc", "hi", 0, False)  # dedup-drops
        reasons = relay._last_drop_reason_by_direction
        self.assertEqual(reasons.get("meshtastic->meshcore"), "dedup")

    def test_drop_reason_loop_guard_uses_wildcard_key(self):
        relay, _ = self._make_relay()
        relay.observe(
            "meshtastic", "!abc",
            "from meshcore (X): prev bridged", 0, False,
        )
        reasons = relay._last_drop_reason_by_direction
        self.assertEqual(reasons.get("meshtastic->*"), "loop_guard:already_bridged")

    def test_drop_reason_empty_text_uses_wildcard_key(self):
        relay, _ = self._make_relay()
        relay.observe("meshtastic", "!abc", "", 0, False)
        reasons = relay._last_drop_reason_by_direction
        self.assertEqual(reasons.get("meshtastic->*"), "empty_text")

    def test_drop_reason_channel_allowlist(self):
        relay, _ = self._make_relay(policy=ChannelAllowlist([("meshtastic", 0)]))
        relay.observe("meshtastic", "!abc", "hi", 99, False)
        reasons = relay._last_drop_reason_by_direction
        self.assertEqual(
            reasons.get("meshtastic->meshcore"),
            "policy:no_rule_matched source=meshtastic channel=99",
        )

    def test_drop_reason_send_error(self):
        def boom_send(dest, text, channel):
            raise RuntimeError("radio angry")
        relay = Relay(send_fn=boom_send, policy=AlwaysRelay())
        relay.observe("meshtastic", "!abc", "hi", 0, False)
        reasons = relay._last_drop_reason_by_direction
        self.assertIn("meshtastic->meshcore", reasons)
        self.assertTrue(
            reasons["meshtastic->meshcore"].startswith("send_error:RuntimeError"),
            f"unexpected reason: {reasons['meshtastic->meshcore']}",
        )

    def test_run_selftest_ok_both_directions(self):
        relay, sends = self._make_relay()
        mt_result = relay.run_selftest("meshtastic")
        mc_result = relay.run_selftest("meshcore")
        self.assertTrue(mt_result["ok"])
        self.assertEqual(mt_result["delivered"], ["meshcore"])
        self.assertEqual(mt_result["direction"], "meshtastic->meshcore")
        self.assertIsNone(mt_result["drop_reason"])
        self.assertTrue(mt_result["nonce"].startswith("LORACLE-TEST-"))
        self.assertGreaterEqual(mt_result["elapsed_ms"], 0)

        self.assertTrue(mc_result["ok"])
        self.assertEqual(mc_result["delivered"], ["meshtastic"])
        self.assertEqual(mc_result["direction"], "meshcore->meshtastic")
        # No real sends should have happened
        self.assertEqual(sends, [])

    def test_run_selftest_reports_drop_reason_on_disabled(self):
        relay, sends = self._make_relay(policy=DisabledPolicy())
        result = relay.run_selftest("meshtastic")
        self.assertFalse(result["ok"])
        self.assertEqual(result["delivered"], [])
        self.assertEqual(result["drop_reason"], "policy:disabled")
        self.assertEqual(sends, [])

    def test_run_selftest_rejects_unknown_protocol(self):
        relay, _ = self._make_relay()
        result = relay.run_selftest("reticulum")
        self.assertFalse(result["ok"])
        self.assertIn("unknown_source_protocol", result["drop_reason"])

    def test_stats_exposes_last_drop_reason_by_direction(self):
        relay, _ = self._make_relay(policy=DisabledPolicy())
        relay.observe("meshtastic", "!abc", "hi", 0, False)
        s = relay.stats()
        self.assertIn("last_drop_reason_by_direction", s)
        self.assertEqual(
            s["last_drop_reason_by_direction"]["meshtastic->meshcore"],
            "policy:disabled",
        )

    def test_stats_last_observe_at_s_ago_none_before_first_observe(self):
        relay, _ = self._make_relay()
        s = relay.stats()
        self.assertIsNone(s["last_observe_at_s_ago"])

    def test_stats_last_observe_at_s_ago_populated_after_observe(self):
        relay, _ = self._make_relay()
        relay.observe("meshtastic", "!abc", "hi", 0, False)
        s = relay.stats()
        self.assertIsNotNone(s["last_observe_at_s_ago"])
        self.assertGreaterEqual(s["last_observe_at_s_ago"], 0)

    def test_stats_has_uptime(self):
        relay, _ = self._make_relay()
        s = relay.stats()
        self.assertIn("uptime_s", s)
        self.assertGreaterEqual(s["uptime_s"], 0)


if __name__ == "__main__":
    unittest.main()
