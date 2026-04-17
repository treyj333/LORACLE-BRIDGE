"""Tests for Phase 2 reliability: bounded data structures and overflow TTL."""

import time
import unittest
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from standalone_bridge import StandaloneBridge, MAX_TRACKED_NODES, OVERFLOW_TTL, _parse_second_radio


def _make_bridge():
    """Create a StandaloneBridge with mocked dependencies (no real radio)."""
    with patch("standalone_bridge.OllamaClient"):
        bridge = StandaloneBridge.__new__(StandaloneBridge)
        bridge._node_positions = {}
        bridge._node_meta = {}
        bridge._overflow = {}
        bridge._running = False
    return bridge


class TestNodePositionEviction(unittest.TestCase):
    """Verify _node_positions is bounded by MAX_TRACKED_NODES."""

    def test_evicts_oldest_when_at_capacity(self):
        bridge = _make_bridge()
        # Fill to capacity with timestamped positions
        for i in range(MAX_TRACKED_NODES):
            node_id = f"!{i:08x}"
            bridge._store_node_position(node_id, 34.0 + i * 0.001, -118.0, 0, ts=float(i))

        self.assertEqual(len(bridge._node_positions), MAX_TRACKED_NODES)

        # Add one more — should evict the oldest (node 0, ts=0.0)
        bridge._store_node_position("!newnode1", 35.0, -117.0, 0, ts=float(MAX_TRACKED_NODES))
        self.assertEqual(len(bridge._node_positions), MAX_TRACKED_NODES)
        # The first node (ts=0.0) should have been evicted
        self.assertNotIn("!00000000", bridge._node_positions)
        self.assertIn("!newnode1", bridge._node_positions)

    def test_also_evicts_node_meta(self):
        bridge = _make_bridge()
        # Add a node with both position and meta
        bridge._node_meta["!00000000"] = {"hops": 2}
        for i in range(MAX_TRACKED_NODES):
            node_id = f"!{i:08x}"
            bridge._store_node_position(node_id, 34.0, -118.0, 0, ts=float(i))

        # Add one more to trigger eviction of node 0
        bridge._store_node_position("!overflow1", 35.0, -117.0, 0, ts=float(MAX_TRACKED_NODES))
        self.assertNotIn("!00000000", bridge._node_meta)

    def test_update_existing_node_does_not_evict(self):
        bridge = _make_bridge()
        bridge._store_node_position("!abc", 34.0, -118.0, 0)
        bridge._store_node_position("!abc", 34.1, -118.1, 0)  # update, not new
        self.assertEqual(len(bridge._node_positions), 1)
        self.assertAlmostEqual(bridge._node_positions["!abc"]["lat"], 34.1)


class TestOverflowTTL(unittest.TestCase):
    """Verify _overflow entries use timestamps and can be evicted."""

    def test_overflow_stores_timestamp(self):
        bridge = _make_bridge()
        bridge._overflow["!node1"] = ("remaining text here", time.time())
        text, ts = bridge._overflow["!node1"]
        self.assertEqual(text, "remaining text here")
        self.assertIsInstance(ts, float)

    def test_stale_overflow_evicted(self):
        """Simulate what the cleanup loop does for stale overflow entries."""
        bridge = _make_bridge()
        old_ts = time.time() - OVERFLOW_TTL - 10  # 10s past expiry
        bridge._overflow["!stale"] = ("old text", old_ts)
        bridge._overflow["!fresh"] = ("new text", time.time())

        # Simulate the cleanup logic from _dedup_cleanup_loop
        now = time.time()
        stale = [
            nid for nid, (_, ts) in bridge._overflow.items()
            if now - ts > OVERFLOW_TTL
        ]
        for nid in stale:
            del bridge._overflow[nid]

        self.assertNotIn("!stale", bridge._overflow)
        self.assertIn("!fresh", bridge._overflow)


class TestParseSecondRadio(unittest.TestCase):
    """Verify --second-radio spec parsing handles valid + invalid inputs."""

    def test_none_and_empty(self):
        self.assertIsNone(_parse_second_radio(None))
        self.assertIsNone(_parse_second_radio(""))

    def test_serial(self):
        cfg = _parse_second_radio("meshcore:serial:/dev/ttyUSB1")
        self.assertEqual(cfg["protocol"], "meshcore")
        self.assertEqual(cfg["transport"], "serial")
        self.assertEqual(cfg["serial_port"], "/dev/ttyUSB1")

    def test_tcp_with_port(self):
        cfg = _parse_second_radio("meshcore:tcp:192.168.1.50:4000")
        self.assertEqual(cfg["transport"], "tcp")
        self.assertEqual(cfg["tcp_host"], "192.168.1.50")
        self.assertEqual(cfg["tcp_port"], 4000)

    def test_tcp_default_port(self):
        cfg = _parse_second_radio("meshcore:tcp:mc.local")
        self.assertEqual(cfg["tcp_host"], "mc.local")
        self.assertEqual(cfg["tcp_port"], 4000)

    def test_tcp_bad_port(self):
        self.assertIsNone(_parse_second_radio("meshcore:tcp:host:notaport"))

    def test_ble_with_addr(self):
        cfg = _parse_second_radio("meshcore:ble:AA:BB:CC:DD:EE:FF")
        self.assertEqual(cfg["transport"], "ble")
        self.assertEqual(cfg["ble_address"], "AA:BB:CC:DD:EE:FF")

    def test_ble_no_addr(self):
        cfg = _parse_second_radio("meshcore:ble:")
        self.assertEqual(cfg["transport"], "ble")
        self.assertIsNone(cfg["ble_address"])

    def test_unsupported_primary_protocol(self):
        self.assertIsNone(_parse_second_radio("meshtastic:serial:/dev/ttyUSB0"))

    def test_malformed(self):
        self.assertIsNone(_parse_second_radio("meshcore"))
        self.assertIsNone(_parse_second_radio("meshcore:serial"))
        self.assertIsNone(_parse_second_radio("nonsense"))


if __name__ == "__main__":
    unittest.main()
