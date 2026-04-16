"""Tests for the radio/ abstraction layer."""

import queue
import sys
import os
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radio.events import Protocol, Transport, UnifiedMessage, UnifiedNode, BackendEvent
from radio.manager import RadioManager
from radio.backend import RadioBackend


class TestUnifiedNode(unittest.TestCase):
    def test_make_id(self):
        uid = UnifiedNode.make_id(Protocol.MESHTASTIC, "!a3f2b8c1")
        self.assertEqual(uid, "mt:!a3f2b8c1")

    def test_make_id_meshcore(self):
        uid = UnifiedNode.make_id(Protocol.MESHCORE, "abcdef012345")
        self.assertEqual(uid, "mc:abcdef012345")

    def test_parse_id(self):
        proto, native = UnifiedNode.parse_id("mt:!a3f2b8c1")
        self.assertEqual(proto, "mt")
        self.assertEqual(native, "!a3f2b8c1")

    def test_parse_id_meshcore(self):
        proto, native = UnifiedNode.parse_id("mc:abcdef012345")
        self.assertEqual(proto, "mc")
        self.assertEqual(native, "abcdef012345")

    def test_default_fields(self):
        node = UnifiedNode(
            id="mt:!test",
            protocol=Protocol.MESHTASTIC,
            backend_native_id="!test",
        )
        self.assertIsNone(node.rssi)
        self.assertIsNone(node.snr)
        self.assertIsNone(node.hops_away)
        self.assertFalse(node.has_position)


class TestUnifiedMessage(unittest.TestCase):
    def test_create_message(self):
        node = UnifiedNode(
            id="mt:!abc",
            protocol=Protocol.MESHTASTIC,
            backend_native_id="!abc",
        )
        msg = UnifiedMessage(
            protocol=Protocol.MESHTASTIC,
            node=node,
            text="Hello world",
            channel=0,
            is_dm=True,
        )
        self.assertEqual(msg.text, "Hello world")
        self.assertTrue(msg.is_dm)
        self.assertIsNone(msg.rssi)

    def test_meshcore_message_no_rssi(self):
        node = UnifiedNode(
            id="mc:abc123",
            protocol=Protocol.MESHCORE,
            backend_native_id="abc123",
        )
        msg = UnifiedMessage(
            protocol=Protocol.MESHCORE,
            node=node,
            text="Test",
            rssi=None,
            snr=None,
        )
        self.assertIsNone(msg.rssi)
        self.assertIsNone(msg.snr)


class _MockBackend(RadioBackend):
    """Minimal concrete backend for testing RadioManager."""
    protocol = Protocol.MESHTASTIC

    def __init__(self, backend_id="mock-0", protocol=Protocol.MESHTASTIC):
        self.backend_id = backend_id
        self.protocol = protocol
        self.transport = Transport.SERIAL
        self._connected = True
        self._callback = None
        self._sent = []

    def connect(self):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def is_connected(self):
        return self._connected

    def send_direct_message(self, to_native_id, text):
        self._sent.append(("dm", to_native_id, text))

    def send_broadcast(self, text, channel=0):
        self._sent.append(("broadcast", channel, text))

    def start_listening(self, callback):
        self._callback = callback

    def stop_listening(self):
        self._callback = None

    def get_nodes(self):
        return {}

    def get_self_info(self):
        return {"self_node_id": "!mock"}

    def inject_message(self, msg):
        if self._callback:
            self._callback(msg)


class TestRadioManager(unittest.TestCase):
    def test_add_backend(self):
        mgr = RadioManager()
        backend = _MockBackend("mt-serial-0")
        mgr.add_backend(backend)
        self.assertEqual(len(mgr.get_backends()), 1)

    def test_remove_backend(self):
        mgr = RadioManager()
        backend = _MockBackend("mt-serial-0")
        mgr.add_backend(backend)
        mgr.remove_backend("mt-serial-0")
        self.assertEqual(len(mgr.get_backends()), 0)
        self.assertFalse(backend._connected)

    def test_send_routes_by_protocol(self):
        mgr = RadioManager()
        mt = _MockBackend("mt-0", Protocol.MESHTASTIC)
        mc = _MockBackend("mc-0", Protocol.MESHCORE)
        mgr.add_backend(mt)
        mgr.add_backend(mc)

        mgr.send("mt:!node1", "hello", is_dm=True)
        mgr.send("mc:abc123", "world", is_dm=True)

        self.assertEqual(len(mt._sent), 1)
        self.assertEqual(mt._sent[0], ("dm", "!node1", "hello"))
        self.assertEqual(len(mc._sent), 1)
        self.assertEqual(mc._sent[0], ("dm", "abc123", "world"))

    def test_send_unknown_protocol_raises(self):
        mgr = RadioManager()
        mt = _MockBackend("mt-0")
        mgr.add_backend(mt)
        with self.assertRaises(ValueError):
            mgr.send("xx:unknown", "text")

    def test_message_queue(self):
        mgr = RadioManager()
        backend = _MockBackend("mt-0")
        mgr.add_backend(backend)

        node = UnifiedNode(
            id="mt:!test", protocol=Protocol.MESHTASTIC,
            backend_native_id="!test",
        )
        msg = UnifiedMessage(
            protocol=Protocol.MESHTASTIC, node=node,
            text="test message",
        )
        backend.inject_message(msg)

        result = mgr.get_message(timeout=1.0)
        self.assertIsNotNone(result)
        self.assertEqual(result.text, "test message")

    def test_get_message_timeout(self):
        mgr = RadioManager()
        result = mgr.get_message(timeout=0.1)
        self.assertIsNone(result)

    def test_has_connected_backend(self):
        mgr = RadioManager()
        self.assertFalse(mgr.has_connected_backend())
        backend = _MockBackend("mt-0")
        mgr.add_backend(backend)
        self.assertTrue(mgr.has_connected_backend())
        backend._connected = False
        self.assertFalse(mgr.has_connected_backend())

    def test_get_backends_info(self):
        mgr = RadioManager()
        mt = _MockBackend("mt-0", Protocol.MESHTASTIC)
        mgr.add_backend(mt)
        info = mgr.get_backends_info()
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0]["protocol"], "mt")
        self.assertEqual(info[0]["transport"], "serial")
        self.assertTrue(info[0]["connected"])

    def test_broadcast_routing(self):
        mgr = RadioManager()
        mt = _MockBackend("mt-0")
        mgr.add_backend(mt)
        mgr.send("mt:broadcast", "hello all", channel=2, is_dm=False)
        self.assertEqual(mt._sent[0], ("broadcast", 2, "hello all"))

    def test_fallback_no_prefix(self):
        """Node IDs without protocol prefix should route to primary backend."""
        mgr = RadioManager()
        mt = _MockBackend("mt-0")
        mgr.add_backend(mt)
        mgr.send("!abc123", "fallback test", is_dm=True)
        self.assertEqual(len(mt._sent), 1)
        self.assertEqual(mt._sent[0], ("dm", "!abc123", "fallback test"))


class TestProtocolEnum(unittest.TestCase):
    def test_values(self):
        self.assertEqual(Protocol.MESHTASTIC.value, "mt")
        self.assertEqual(Protocol.MESHCORE.value, "mc")

    def test_transport_values(self):
        self.assertEqual(Transport.SERIAL.value, "serial")
        self.assertEqual(Transport.TCP.value, "tcp")
        self.assertEqual(Transport.BLE.value, "ble")


if __name__ == "__main__":
    unittest.main()
