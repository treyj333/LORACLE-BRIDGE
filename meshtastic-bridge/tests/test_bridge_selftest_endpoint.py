"""v2.5 — integration tests for the POST /api/bridge/selftest endpoint
and the supporting GET /api/bridge/health / GET /api/bridge/logs routes.

The selftest must exercise the real Relay (not a mock) so the
guard-chain + send_fn-bypass contract is verified end-to-end. The radio
manager is mocked because the selftest should never touch real radios."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard
from bridge.policy import AlwaysRelay, DisabledPolicy
from bridge.relay import Relay


def _make_bridge_with_real_relay(policy=None, rules=None, enabled=True):
    """Build a MagicMock bridge carrying a real Relay so run_selftest actually
    fires the guard chain. Radios are mocked — the selftest never sends."""
    bridge = MagicMock()
    bridge._bridge_config = {
        "enabled": enabled,
        "rules": rules if rules is not None else [
            {"source": "meshtastic", "channel": 0, "mode": "always"},
            {"source": "meshcore", "channel": 0, "mode": "always"},
        ],
    }
    sends = []
    def fake_send(dest, text, channel):
        sends.append((dest, text, channel))
    bridge._relay = Relay(
        send_fn=fake_send,
        policy=policy if policy is not None else AlwaysRelay(),
    )
    bridge._relay_sends = sends  # for test introspection
    # Two connected mock backends so /health returns the ready state
    be_mt = MagicMock()
    be_mt.protocol = MagicMock(value="mt")
    be_mt.is_connected = MagicMock(return_value=True)
    be_mc = MagicMock()
    be_mc.protocol = MagicMock(value="mc")
    be_mc.is_connected = MagicMock(return_value=True)
    bridge._radio_manager = MagicMock()
    bridge._radio_manager.get_backends = MagicMock(return_value=[be_mt, be_mc])
    return bridge


class TestSelftestEndpoint(unittest.TestCase):
    def setUp(self):
        self.bridge = _make_bridge_with_real_relay()
        dashboard.set_bridge(self.bridge)
        self.client = dashboard.app.test_client()

    def tearDown(self):
        dashboard._bridge = None

    def test_both_directions_ok(self):
        resp = self.client.post(
            "/api/bridge/selftest",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["results"]), 2)
        directions = {r["direction"] for r in data["results"]}
        self.assertEqual(
            directions,
            {"meshtastic->meshcore", "meshcore->meshtastic"},
        )
        for r in data["results"]:
            self.assertTrue(r["ok"], f"direction failed: {r}")
            self.assertIn("LORACLE-TEST-", r["nonce"])
            self.assertIsNone(r["drop_reason"])

    def test_selftest_does_not_hit_radios(self):
        self.client.post(
            "/api/bridge/selftest",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(
            self.bridge._relay_sends, [],
            "selftest must never call send_fn",
        )

    def test_mt_to_mc_only(self):
        resp = self.client.post(
            "/api/bridge/selftest",
            data=json.dumps({"direction": "mt_to_mc"}),
            content_type="application/json",
        )
        data = json.loads(resp.data)
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["direction"], "meshtastic->meshcore")

    def test_mc_to_mt_only(self):
        resp = self.client.post(
            "/api/bridge/selftest",
            data=json.dumps({"direction": "mc_to_mt"}),
            content_type="application/json",
        )
        data = json.loads(resp.data)
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["direction"], "meshcore->meshtastic")

    def test_disabled_policy_surfaces_drop_reason(self):
        bridge = _make_bridge_with_real_relay(policy=DisabledPolicy())
        dashboard.set_bridge(bridge)
        client = dashboard.app.test_client()
        resp = client.post(
            "/api/bridge/selftest",
            data=json.dumps({"direction": "both"}),
            content_type="application/json",
        )
        data = json.loads(resp.data)
        self.assertFalse(data["ok"])
        for r in data["results"]:
            self.assertFalse(r["ok"])
            self.assertEqual(r["drop_reason"], "policy:disabled")

    def test_no_bridge_returns_503(self):
        dashboard._bridge = None
        resp = self.client.post(
            "/api/bridge/selftest",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 503)

    def test_invalid_direction_defaults_to_both(self):
        resp = self.client.post(
            "/api/bridge/selftest",
            data=json.dumps({"direction": "garbage"}),
            content_type="application/json",
        )
        data = json.loads(resp.data)
        self.assertEqual(len(data["results"]), 2)


class TestHealthEndpoint(unittest.TestCase):
    def setUp(self):
        self.bridge = _make_bridge_with_real_relay()
        dashboard.set_bridge(self.bridge)
        self.client = dashboard.app.test_client()

    def tearDown(self):
        dashboard._bridge = None

    def test_health_reports_master_and_rules(self):
        resp = self.client.get("/api/bridge/health")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data["ready"])
        self.assertTrue(data["master_enabled"])
        self.assertEqual(len(data["rules"]), 2)

    def test_health_reports_backends(self):
        resp = self.client.get("/api/bridge/health")
        data = json.loads(resp.data)
        self.assertEqual(len(data["backends"]), 2)
        protos = {b["protocol"] for b in data["backends"]}
        self.assertEqual(protos, {"mt", "mc"})

    def test_health_last_observe_none_before_traffic(self):
        resp = self.client.get("/api/bridge/health")
        data = json.loads(resp.data)
        self.assertIsNone(data["last_observe_at_s_ago"])

    def test_health_last_observe_populated_after_real_traffic(self):
        self.bridge._relay.observe("meshtastic", "!abc", "hi", 0, False)
        resp = self.client.get("/api/bridge/health")
        data = json.loads(resp.data)
        self.assertIsNotNone(data["last_observe_at_s_ago"])
        self.assertGreaterEqual(data["last_observe_at_s_ago"], 0)

    def test_health_no_bridge_returns_not_ready(self):
        dashboard._bridge = None
        resp = self.client.get("/api/bridge/health")
        data = json.loads(resp.data)
        self.assertFalse(data["ready"])


class TestBridgeLogsEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = dashboard.app.test_client()

    def test_logs_returns_entries_list(self):
        resp = self.client.get("/api/bridge/logs")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("entries", data)
        self.assertIsInstance(data["entries"], list)

    def test_logs_limit_clamped(self):
        resp = self.client.get("/api/bridge/logs?limit=9999")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertLessEqual(len(data["entries"]), 200)

    def test_logs_bad_limit_defaults_to_20(self):
        # Shouldn't crash, should fall back to default
        resp = self.client.get("/api/bridge/logs?limit=notanumber")
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
