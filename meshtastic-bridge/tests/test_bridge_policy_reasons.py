"""v2.5 — verify Policy.should_relay returns a (bool, reason) tuple with
the specific reason strings the Relay Health panel surfaces.

The existing test_bridge_policy.py covers the boolean verdict; this file
is dedicated to the drop-reason vocabulary, which the selftest endpoint
and the dashboard log tail rely on being stable."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.policy import (
    AIGatedPolicy,
    AlwaysRelay,
    ChannelAllowlist,
    DisabledPolicy,
)


def _call(policy, source="meshtastic", dest="meshcore", sender="!abc",
          text="hi", channel=0, is_dm=False):
    return policy.should_relay(source, dest, sender, text, channel, is_dm)


class TestDisabledPolicyReason(unittest.TestCase):
    def test_reason_is_policy_disabled(self):
        allowed, reason = _call(DisabledPolicy())
        self.assertFalse(allowed)
        self.assertEqual(reason, "policy:disabled")

    def test_dm_also_disabled(self):
        allowed, reason = _call(DisabledPolicy(), is_dm=True)
        self.assertFalse(allowed)
        self.assertEqual(reason, "policy:disabled")


class TestAlwaysRelayReason(unittest.TestCase):
    def test_channel_allowed_empty_reason(self):
        allowed, reason = _call(AlwaysRelay(), is_dm=False)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_dm_blocked_reason(self):
        allowed, reason = _call(AlwaysRelay(), is_dm=True)
        self.assertFalse(allowed)
        self.assertEqual(reason, "policy:dm_blocked")


class TestChannelAllowlistReason(unittest.TestCase):
    def test_match_empty_reason(self):
        p = ChannelAllowlist([("meshtastic", 0)])
        allowed, reason = _call(p, source="meshtastic", channel=0)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_wildcard_empty_reason(self):
        p = ChannelAllowlist([("meshcore", None)])
        allowed, reason = _call(p, source="meshcore", channel=7)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_no_match_reason_includes_source_and_channel(self):
        p = ChannelAllowlist([("meshtastic", 0)])
        allowed, reason = _call(p, source="meshcore", channel=3)
        self.assertFalse(allowed)
        self.assertEqual(
            reason,
            "policy:no_rule_matched source=meshcore channel=3",
        )

    def test_dm_blocked_reason(self):
        p = ChannelAllowlist([("meshtastic", 0)])
        allowed, reason = _call(p, source="meshtastic", channel=0, is_dm=True)
        self.assertFalse(allowed)
        self.assertEqual(reason, "policy:dm_blocked")


class _FakeClassifier:
    def __init__(self, urgent: bool):
        self.urgent = urgent

    def is_urgent(self, text):
        return self.urgent


class TestAIGatedPolicyReason(unittest.TestCase):
    def test_base_denial_passes_through_reason(self):
        # Base is ChannelAllowlist with no matching rule → AIGatedPolicy
        # must forward the base's specific reason, not override with "ai".
        base = ChannelAllowlist([("meshtastic", 0)])
        p = AIGatedPolicy(base, classifier=_FakeClassifier(urgent=True))
        allowed, reason = _call(p, source="meshcore", channel=5)
        self.assertFalse(allowed)
        self.assertEqual(
            reason,
            "policy:no_rule_matched source=meshcore channel=5",
        )

    def test_classifier_not_urgent(self):
        p = AIGatedPolicy(AlwaysRelay(), classifier=_FakeClassifier(urgent=False))
        allowed, reason = _call(p)
        self.assertFalse(allowed)
        self.assertEqual(reason, "policy:ai_not_urgent")

    def test_classifier_urgent_empty_reason(self):
        p = AIGatedPolicy(AlwaysRelay(), classifier=_FakeClassifier(urgent=True))
        allowed, reason = _call(p)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_classifier_error_fails_open_empty_reason(self):
        class Boom:
            def is_urgent(self, text):
                raise RuntimeError("llm down")
        p = AIGatedPolicy(AlwaysRelay(), classifier=Boom())
        allowed, reason = _call(p)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_stub_mode_empty_reason(self):
        # No classifier = Phase 2 stub: should pass through with empty reason
        p = AIGatedPolicy(AlwaysRelay())
        allowed, reason = _call(p)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
