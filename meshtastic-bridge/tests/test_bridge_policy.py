"""Tests for bridge.policy — Policy classes and bridge.config.build_policy."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.config import DEFAULT_CONFIG, build_policy
from bridge.policy import (
    AIGatedPolicy,
    AlwaysRelay,
    ChannelAllowlist,
    DisabledPolicy,
)


def _call(policy, source="meshtastic", dest="meshcore", sender="!abc",
          text="hi", channel=0, is_dm=False):
    """Helper to call should_relay with named kwargs so test lines stay short."""
    return policy.should_relay(source, dest, sender, text, channel, is_dm)


class TestDisabledPolicy(unittest.TestCase):
    def test_always_false(self):
        p = DisabledPolicy()
        self.assertFalse(_call(p))
        self.assertFalse(_call(p, is_dm=True))


class TestAlwaysRelay(unittest.TestCase):
    def test_channels_pass(self):
        self.assertTrue(_call(AlwaysRelay(), is_dm=False))

    def test_dms_blocked(self):
        self.assertFalse(_call(AlwaysRelay(), is_dm=True))


class TestChannelAllowlist(unittest.TestCase):
    def test_listed_channel_passes(self):
        p = ChannelAllowlist([("meshtastic", 0)])
        self.assertTrue(_call(p, source="meshtastic", channel=0))

    def test_unlisted_channel_blocked(self):
        p = ChannelAllowlist([("meshtastic", 0)])
        self.assertFalse(_call(p, source="meshtastic", channel=1))

    def test_wrong_source_blocked(self):
        p = ChannelAllowlist([("meshtastic", 0)])
        self.assertFalse(_call(p, source="meshcore", channel=0))

    def test_wildcard_channel(self):
        # channel=None means "any channel from this protocol"
        p = ChannelAllowlist([("meshcore", None)])
        self.assertTrue(_call(p, source="meshcore", channel=0))
        self.assertTrue(_call(p, source="meshcore", channel=5))
        self.assertFalse(_call(p, source="meshtastic", channel=0))

    def test_dm_never_relays(self):
        p = ChannelAllowlist([("meshtastic", 0)])
        self.assertFalse(_call(p, source="meshtastic", channel=0, is_dm=True))


class _FakeClassifier:
    def __init__(self, urgent: bool):
        self.urgent = urgent
        self.calls = 0

    def is_urgent(self, text):
        self.calls += 1
        return self.urgent


class TestAIGatedPolicy(unittest.TestCase):
    def test_stub_passes_through(self):
        # No classifier (Phase 2 stub): pass through the base decision.
        base = AlwaysRelay()
        p = AIGatedPolicy(base)
        self.assertTrue(_call(p))

    def test_base_nope_short_circuits(self):
        base = DisabledPolicy()
        cls = _FakeClassifier(urgent=True)
        p = AIGatedPolicy(base, classifier=cls)
        self.assertFalse(_call(p))
        # Classifier should NOT have been called — base already said no.
        self.assertEqual(cls.calls, 0)

    def test_urgent_gate_passes(self):
        p = AIGatedPolicy(AlwaysRelay(), classifier=_FakeClassifier(urgent=True))
        self.assertTrue(_call(p))

    def test_non_urgent_gate_blocks(self):
        p = AIGatedPolicy(AlwaysRelay(), classifier=_FakeClassifier(urgent=False))
        self.assertFalse(_call(p))

    def test_classifier_error_fails_open(self):
        class Boom:
            def is_urgent(self, text):
                raise RuntimeError("llm unavailable")
        p = AIGatedPolicy(AlwaysRelay(), classifier=Boom())
        # Deliberate fail-open: if the classifier errors, relay anyway
        # rather than silently drop what might be urgent traffic.
        self.assertTrue(_call(p))


class TestBuildPolicy(unittest.TestCase):
    def test_default_is_disabled(self):
        p = build_policy(DEFAULT_CONFIG)
        self.assertIsInstance(p, DisabledPolicy)

    def test_enabled_but_no_rules(self):
        p = build_policy({"enabled": True, "rules": []})
        self.assertIsInstance(p, DisabledPolicy)

    def test_enabled_all_off(self):
        p = build_policy({
            "enabled": True,
            "rules": [{"source": "meshtastic", "channel": 0, "mode": "off"}],
        })
        self.assertIsInstance(p, DisabledPolicy)

    def test_always_rule(self):
        p = build_policy({
            "enabled": True,
            "rules": [{"source": "meshtastic", "channel": 0, "mode": "always"}],
        })
        self.assertIsInstance(p, ChannelAllowlist)
        self.assertTrue(_call(p, source="meshtastic", channel=0))
        self.assertFalse(_call(p, source="meshtastic", channel=1))

    def test_ai_gated_rule_wraps(self):
        p = build_policy({
            "enabled": True,
            "rules": [{"source": "meshcore", "channel": 2, "mode": "ai-gated"}],
        })
        self.assertIsInstance(p, AIGatedPolicy)
        self.assertTrue(_call(p, source="meshcore", channel=2))

    def test_wildcard_channel_none(self):
        p = build_policy({
            "enabled": True,
            "rules": [{"source": "meshtastic", "channel": None, "mode": "always"}],
        })
        self.assertTrue(_call(p, source="meshtastic", channel=0))
        self.assertTrue(_call(p, source="meshtastic", channel=7))

    def test_malformed_rules_dropped(self):
        # Unknown source + unknown mode + bad channel type — all dropped;
        # the single valid rule should still work.
        p = build_policy({
            "enabled": True,
            "rules": [
                {"source": "reticulum", "channel": 0, "mode": "always"},
                {"source": "meshtastic", "channel": 0, "mode": "mumble"},
                {"source": "meshtastic", "channel": "abc", "mode": "always"},
                {"source": "meshtastic", "channel": 0, "mode": "always"},
            ],
        })
        self.assertTrue(_call(p, source="meshtastic", channel=0))


if __name__ == "__main__":
    unittest.main()
