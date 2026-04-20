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
from radio.backend import RadioBackend, FeatureNotSupported


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

    def test_traceroute_default_raises_feature_not_supported(self):
        """Backends that don't override send_traceroute must raise
        FeatureNotSupported so dashboards can return 501 gracefully."""
        mgr = RadioManager()
        mgr.add_backend(_MockBackend("mc-0", Protocol.MESHCORE))
        with self.assertRaises(FeatureNotSupported):
            mgr.send_traceroute("mc:abc123")

    def test_traceroute_dispatches_to_right_backend(self):
        """Traceroute should land on the backend whose protocol matches the
        unified ID, and pass the native id through untouched."""
        calls = []

        class _TracingMock(_MockBackend):
            def send_traceroute(self, to_native_id, hop_limit=7):
                calls.append((self.backend_id, to_native_id, hop_limit))

        mgr = RadioManager()
        mgr.add_backend(_TracingMock("mt-0", Protocol.MESHTASTIC))
        mgr.add_backend(_MockBackend("mc-0", Protocol.MESHCORE))
        mgr.send_traceroute("mt:!abc123", hop_limit=5)
        self.assertEqual(calls, [("mt-0", "!abc123", 5)])

    def test_radio_config_routes_to_backend_id(self):
        """get_radio_config(backend_id=...) picks the right backend, and
        the result carries ``backend_id`` + ``protocol`` for the UI."""

        class _ConfigurableMock(_MockBackend):
            def get_radio_config(self):
                return {"region": 3, "tx_power": 20}

        mgr = RadioManager()
        mgr.add_backend(_ConfigurableMock("mt-0", Protocol.MESHTASTIC))
        mgr.add_backend(_MockBackend("mc-0", Protocol.MESHCORE))
        data = mgr.get_radio_config(backend_id="mt-0")
        self.assertEqual(data["region"], 3)
        self.assertEqual(data["backend_id"], "mt-0")
        self.assertEqual(data["protocol"], "mt")
        # Default MC backend has no config surface
        with self.assertRaises(FeatureNotSupported):
            mgr.get_radio_config(backend_id="mc-0")

    def test_set_radio_config_read_only_raises(self):
        """set_radio_config on a read-only backend surfaces FeatureNotSupported."""
        mgr = RadioManager()
        mgr.add_backend(_MockBackend("mc-0", Protocol.MESHCORE))
        with self.assertRaises(FeatureNotSupported):
            mgr.set_radio_config({"tx_power": 22}, backend_id="mc-0")


class TestProtocolEnum(unittest.TestCase):
    def test_values(self):
        self.assertEqual(Protocol.MESHTASTIC.value, "mt")
        self.assertEqual(Protocol.MESHCORE.value, "mc")

    def test_transport_values(self):
        self.assertEqual(Transport.SERIAL.value, "serial")
        self.assertEqual(Transport.TCP.value, "tcp")
        self.assertEqual(Transport.BLE.value, "ble")


class TestMeshCoreChannelDiscovery(unittest.TestCase):
    """_discover_channels should enumerate the MC device's channel slots
    and pick whichever one is named "Public". Default to slot 0 when no
    Public-named slot is found."""

    def _make_backend(self):
        # Import inside the test to keep the module-level import graph
        # clean for environments without the meshcore lib. MESHCORE_AVAILABLE
        # at import time covers that case.
        from radio.meshcore_backend import MeshCoreBackend
        b = MeshCoreBackend(connection_type="serial", serial_port="/dev/null")
        b._meshcore = MagicMock()
        return b

    @staticmethod
    def _make_get_channel_stub(slot_names):
        """Return an async callable that yields CHANNEL_INFO events for
        named slots and ERROR for any index past the end. slot_names is
        a list of (idx, name) pairs — any idx not listed returns ERROR."""
        # Import locally so tests skip cleanly in environments without the lib.
        try:
            from meshcore.events import EventType
        except ImportError:
            return None
        known = {idx: name for idx, name in slot_names}

        async def _get_channel(idx):
            evt = MagicMock()
            if idx in known:
                evt.type = EventType.CHANNEL_INFO
                evt.payload = {"channel_name": known[idx], "channel_idx": idx, "channel_hash": "ff"}
            else:
                evt.type = EventType.ERROR
                evt.payload = None
            return evt
        return _get_channel

    def test_discover_picks_public_named_slot(self):
        stub = self._make_get_channel_stub([(0, "General"), (1, "Public"), (2, "Secondary")])
        if stub is None:
            self.skipTest("meshcore lib not installed")
        b = self._make_backend()
        b._meshcore.commands.get_channel = stub
        import asyncio
        asyncio.run(b._discover_channels())
        self.assertEqual(b.get_public_channel_index(), 1)
        names = [(c["idx"], c["name"]) for c in b.get_channel_table()]
        self.assertEqual(names, [(0, "General"), (1, "Public"), (2, "Secondary")])

    def test_discover_case_insensitive_match(self):
        stub = self._make_get_channel_stub([(0, "private"), (3, "public channel")])
        if stub is None:
            self.skipTest("meshcore lib not installed")
        b = self._make_backend()
        b._meshcore.commands.get_channel = stub
        import asyncio
        asyncio.run(b._discover_channels())
        # Match "public channel" at slot 3 (lowercase, two words).
        self.assertEqual(b.get_public_channel_index(), 3)

    def test_discover_no_public_defaults_to_zero(self):
        stub = self._make_get_channel_stub([(0, "General"), (1, "Admin")])
        if stub is None:
            self.skipTest("meshcore lib not installed")
        b = self._make_backend()
        b._meshcore.commands.get_channel = stub
        import asyncio
        asyncio.run(b._discover_channels())
        # No slot named Public — fallback to 0 and keep the table populated
        # so the operator's warning log can list what IS on the device.
        self.assertEqual(b.get_public_channel_index(), 0)
        self.assertEqual(len(b.get_channel_table()), 2)

    def test_discover_empty_table_defaults_to_zero(self):
        stub = self._make_get_channel_stub([])  # no slots — first get_channel returns ERROR
        if stub is None:
            self.skipTest("meshcore lib not installed")
        b = self._make_backend()
        b._meshcore.commands.get_channel = stub
        import asyncio
        asyncio.run(b._discover_channels())
        self.assertEqual(b.get_public_channel_index(), 0)
        self.assertEqual(b.get_channel_table(), [])

    def _run_send_broadcast(self, backend, text, channel):
        """Run send_broadcast with asyncio.run_coroutine_threadsafe patched
        to run the coroutine synchronously in a throwaway loop. The
        production path needs a live loop in a background thread; for
        unit tests we just want to assert the coroutine was called with
        the expected channel index, so skip the threading."""
        import asyncio

        def _fake_threadsafe(coro, loop):
            class _ImmediateFuture:
                def result(self, timeout=None):
                    new_loop = asyncio.new_event_loop()
                    try:
                        return new_loop.run_until_complete(coro)
                    finally:
                        new_loop.close()
            return _ImmediateFuture()

        with patch("radio.meshcore_backend.asyncio.run_coroutine_threadsafe", _fake_threadsafe):
            backend.send_broadcast(text, channel=channel)

    def test_send_broadcast_remaps_zero_to_public_slot(self):
        """send_broadcast(channel=0) should substitute the discovered
        public slot so bridge-sourced relays reach the operator's
        actual Public channel, not whatever's in slot 0."""
        try:
            from meshcore.events import EventType
        except ImportError:
            self.skipTest("meshcore lib not installed")
        b = self._make_backend()
        b._public_channel_index = 3
        b._loop = MagicMock()  # not actually used thanks to the patch
        with patch.object(type(b), "is_connected", return_value=True):
            recorded = {"channel": None}

            async def _fake_send_chan_msg(chan, text):
                recorded["channel"] = chan
                evt = MagicMock()
                evt.type = EventType.OK
                return evt

            b._meshcore.commands.send_chan_msg = _fake_send_chan_msg
            self._run_send_broadcast(b, "hello world", 0)
            self.assertEqual(recorded["channel"], 3)  # remapped

    def test_send_broadcast_passes_explicit_nonzero_channel_through(self):
        """Explicit non-zero channel callers bypass the remap — preserves
        the ability to send on a specific named channel by index."""
        try:
            from meshcore.events import EventType
        except ImportError:
            self.skipTest("meshcore lib not installed")
        b = self._make_backend()
        b._public_channel_index = 3  # remap target
        b._loop = MagicMock()
        with patch.object(type(b), "is_connected", return_value=True):
            recorded = {"channel": None}

            async def _fake(chan, text):
                recorded["channel"] = chan
                evt = MagicMock()
                evt.type = EventType.OK
                return evt

            b._meshcore.commands.send_chan_msg = _fake
            self._run_send_broadcast(b, "hello", 5)
            self.assertEqual(recorded["channel"], 5)  # untouched


if __name__ == "__main__":
    unittest.main()
