"""End-to-end integration test for the LORACLE v2 bridge — no hardware.

Simulates a cross-protocol scenario with two mock send paths and the
full relay pipeline (policy + dedup + rate-limit + loop-guard + force-
relay + audit log + addon hook). Verifies that a message on one side
lands on the other with the right prefix, doesn't loop back, and that
the audit log captures what crossed.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.config import build_policy
from bridge.dedup import RelayDedupCache
from bridge.rate_limit import RelayRateLimiter
from bridge.relay import Relay
from db.bridge_events import BridgeEventStore
from db.schema import init_db


class FakeRadio:
    """Captures sends + lets the test simulate receiving messages."""

    def __init__(self, protocol):
        self.protocol = protocol
        self.sent = []

    def receive(self, relay, sender, text, channel=0, is_dm=False):
        relay.observe(self.protocol, sender, text, channel, is_dm)


class MockAddon:
    name = "mock_addon"

    def __init__(self):
        self.observed = []

    def on_bridged_message(self, event):
        self.observed.append(event)


class TestBridgeIntegration(unittest.TestCase):
    def _build(self, cfg):
        db = init_db(":memory:")
        store = BridgeEventStore(db)
        addon = MockAddon()
        mt_radio = FakeRadio("meshtastic")
        mc_radio = FakeRadio("meshcore")

        def send_fn(dest, text, channel):
            # Capture the outbound send per destination.
            target = mc_radio if dest == "meshcore" else mt_radio
            target.sent.append({
                "dest": dest, "text": text, "channel": channel,
            })

        def on_relay(event):
            store.insert(
                source_protocol=event["source"],
                dest_protocol=event["dest"],
                channel=event["channel"],
                sender=event["sender"],
                sender_display=event.get("sender_display"),
                text=event["text"],
                outcome="relayed",
            )
            addon.on_bridged_message(event)

        relay = Relay(
            send_fn=send_fn,
            policy=build_policy(cfg),
            dedup=RelayDedupCache(ttl_seconds=60),
            rate_limiter=RelayRateLimiter(max_events=100, window_seconds=60),
            on_relay=on_relay,
        )
        return relay, store, addon, mt_radio, mc_radio

    def test_always_rule_relays_both_directions(self):
        cfg = {
            "enabled": True,
            "rules": [
                {"source": "meshtastic", "channel": 0, "mode": "always"},
                {"source": "meshcore", "channel": 0, "mode": "always"},
            ],
        }
        relay, store, addon, mt, mc = self._build(cfg)

        # MT → MC
        mt.receive(relay, "!alice", "hello from meshtastic", channel=0, is_dm=False)
        self.assertEqual(len(mc.sent), 1)
        self.assertEqual(len(mt.sent), 0)
        self.assertIn("from meshtastic (", mc.sent[0]["text"])
        self.assertIn("hello from meshtastic", mc.sent[0]["text"])

        # MC → MT
        mc.receive(relay, "abcdef012345", "hello from meshcore", channel=0, is_dm=False)
        self.assertEqual(len(mt.sent), 1)
        self.assertIn("from meshcore (", mt.sent[0]["text"])

        # Audit log captures both
        rows = store.recent()
        self.assertEqual(len(rows), 2)
        outcomes = {r["outcome"] for r in rows}
        self.assertEqual(outcomes, {"relayed"})

        # Addon observed both
        self.assertEqual(len(addon.observed), 2)
        directions = {(e["source"], e["dest"]) for e in addon.observed}
        self.assertEqual(
            directions, {("meshtastic", "meshcore"), ("meshcore", "meshtastic")}
        )

    def test_echo_back_does_not_loop(self):
        # If the MeshCore side echoes back the same message (e.g. a
        # gateway relaying), the loop-guard + dedup should prevent a
        # second round trip to Meshtastic.
        cfg = {
            "enabled": True,
            "rules": [
                {"source": "meshtastic", "channel": 0, "mode": "always"},
                {"source": "meshcore", "channel": 0, "mode": "always"},
            ],
        }
        relay, store, addon, mt, mc = self._build(cfg)

        mt.receive(relay, "!alice", "outbound", channel=0, is_dm=False)
        # The MC side receives the bridged message verbatim (simulating
        # echo) and tries to relay again:
        echoed = mc.sent[0]["text"]
        mc.receive(relay, "gateway", echoed, channel=0, is_dm=False)

        # MT must NOT see a second send — loop guard + dedup caught it.
        self.assertEqual(len(mt.sent), 0)

    def test_ai_gated_drops_chatter_relays_urgent(self):
        cfg = {
            "enabled": True,
            "rules": [
                {"source": "meshtastic", "channel": 0, "mode": "ai-gated"},
            ],
        }
        relay, store, addon, mt, mc = self._build(cfg)

        # Chatter — dropped
        mt.receive(relay, "!alice", "hi everyone", channel=0, is_dm=False)
        self.assertEqual(len(mc.sent), 0)

        # Urgent — relayed
        mt.receive(relay, "!alice", "EMERGENCY — fire at building 4",
                   channel=0, is_dm=False)
        self.assertEqual(len(mc.sent), 1)

    def test_urgent_prefix_bypasses_disabled_config(self):
        # No rules → DisabledPolicy. !urgent should still cross for
        # channel messages (DM still blocked by is_dm check).
        cfg = {"enabled": False, "rules": []}
        relay, store, addon, mt, mc = self._build(cfg)

        mt.receive(relay, "!alice", "boring update", channel=0, is_dm=False)
        self.assertEqual(len(mc.sent), 0)

        mt.receive(relay, "!alice", "!urgent need medic",
                   channel=0, is_dm=False)
        self.assertEqual(len(mc.sent), 1)
        self.assertIn("need medic", mc.sent[0]["text"])
        self.assertNotIn("!urgent", mc.sent[0]["text"])

    def test_dm_never_crosses(self):
        cfg = {
            "enabled": True,
            "rules": [
                {"source": "meshtastic", "channel": 0, "mode": "always"},
            ],
        }
        relay, store, addon, mt, mc = self._build(cfg)

        mt.receive(relay, "!alice", "secret convo", channel=0, is_dm=True)
        self.assertEqual(mc.sent, [])
        mt.receive(relay, "!alice", "!urgent secret", channel=0, is_dm=True)
        self.assertEqual(mc.sent, [])  # even !urgent can't cross DMs


if __name__ == "__main__":
    unittest.main()
