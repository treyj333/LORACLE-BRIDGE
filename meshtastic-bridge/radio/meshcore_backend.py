"""MeshCoreBackend — RadioBackend implementation for MeshCore radios.

Wraps the ``meshcore`` async Python library in a background thread so it
integrates with the bridge's threading-based architecture.
"""

import asyncio
import logging
import threading
import time
from typing import Callable, Dict, Optional

from radio.events import Protocol, Transport, UnifiedMessage, UnifiedNode
from radio.backend import RadioBackend

try:
    from meshcore import MeshCore
    from meshcore.events import EventType
    MESHCORE_AVAILABLE = True
except ImportError:
    MESHCORE_AVAILABLE = False

logger = logging.getLogger("radio.meshcore")


class MeshCoreBackend(RadioBackend):
    """RadioBackend for MeshCore companion radios."""

    protocol = Protocol.MESHCORE

    def __init__(
        self,
        connection_type: str = "serial",
        serial_port: Optional[str] = None,
        tcp_host: str = "192.168.1.1",
        tcp_port: int = 4000,
        ble_address: Optional[str] = None,
        baudrate: int = 115200,
        backend_id: Optional[str] = None,
    ):
        self.backend_id = backend_id or f"mc-{connection_type}-0"
        self.transport = Transport(connection_type)
        self._connection_type = connection_type
        self._serial_port = serial_port
        self._tcp_host = tcp_host
        self._tcp_port = tcp_port
        self._ble_address = ble_address
        self._baudrate = baudrate

        self._meshcore = None
        self._callback: Optional[Callable] = None
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._contacts: Dict[str, dict] = {}
        self._subscriptions = []

    # ── Lifecycle ────────────────────────────────────────────────────────

    def connect(self) -> None:
        if not MESHCORE_AVAILABLE:
            raise ImportError(
                "meshcore library not installed. "
                "Install with: pip install meshcore>=2.2.1"
            )
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        # Wait for connection with timeout
        deadline = time.time() + 30
        while self._meshcore is None and self._running and time.time() < deadline:
            time.sleep(0.5)
        if self._meshcore is None and self._running:
            self._running = False
            raise ConnectionError("MeshCore connection timed out after 30s")

    def disconnect(self) -> None:
        self._running = False
        if self._loop and self._meshcore:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._async_disconnect(), self._loop
                ).result(timeout=10)
            except Exception as e:
                logger.warning(f"MeshCore disconnect error: {e}")
        if self._thread:
            self._thread.join(timeout=10)
        self._meshcore = None
        self._loop = None

    def is_connected(self) -> bool:
        if self._meshcore is None:
            return False
        try:
            return self._meshcore.is_connected
        except Exception:
            return False

    # ── Messaging ────────────────────────────────────────────────────────

    def send_direct_message(self, to_native_id: str, text: str) -> None:
        if not self.is_connected() or self._loop is None:
            raise ConnectionError("MeshCore radio not connected")
        future = asyncio.run_coroutine_threadsafe(
            self._meshcore.commands.send_msg(to_native_id, text),
            self._loop,
        )
        result = future.result(timeout=30)
        if hasattr(result, "type") and result.type == EventType.ERROR:
            raise RuntimeError(f"MeshCore send failed: {result.payload}")

    def send_broadcast(self, text: str, channel: int = 0) -> None:
        if not self.is_connected() or self._loop is None:
            raise ConnectionError("MeshCore radio not connected")
        future = asyncio.run_coroutine_threadsafe(
            self._meshcore.commands.send_chan_msg(channel, text),
            self._loop,
        )
        result = future.result(timeout=30)
        if hasattr(result, "type") and result.type == EventType.ERROR:
            raise RuntimeError(f"MeshCore broadcast failed: {result.payload}")

    # ── Listening ────────────────────────────────────────────────────────

    def start_listening(self, callback: Callable[[UnifiedMessage], None]) -> None:
        self._callback = callback

    def stop_listening(self) -> None:
        self._callback = None

    # ── Node / device info ───────────────────────────────────────────────

    def get_nodes(self) -> Dict[str, UnifiedNode]:
        result = {}
        for name, contact in self._contacts.items():
            pubkey = contact.get("public_key", "")
            prefix = pubkey[:12] if pubkey else name[:12]
            uid = UnifiedNode.make_id(Protocol.MESHCORE, prefix)
            lat = contact.get("adv_lat")
            lon = contact.get("adv_lon")
            has_pos = lat is not None and lon is not None and (lat != 0 or lon != 0)
            node = UnifiedNode(
                id=uid,
                protocol=Protocol.MESHCORE,
                backend_native_id=prefix,
                short_name=prefix[:6],
                long_name=contact.get("adv_name") or name,
                has_position=has_pos,
                lat=lat if has_pos else None,
                lon=lon if has_pos else None,
            )
            result[prefix] = node
        return result

    def get_node_positions(self) -> Dict[str, dict]:
        """Positions for MC contacts that advertised GPS.

        Keyed by ``backend_native_id`` (the first 12 chars of the public
        key, matching ``get_nodes``). The RadioManager re-wraps these
        with the ``mc:`` prefix when merging across backends.
        """
        import time as _time
        result: Dict[str, dict] = {}
        for name, contact in self._contacts.items():
            pubkey = contact.get("public_key", "")
            prefix = pubkey[:12] if pubkey else name[:12]
            lat = contact.get("adv_lat")
            lon = contact.get("adv_lon")
            if lat is None or lon is None:
                continue
            if lat == 0 and lon == 0:
                continue
            result[prefix] = {
                "lat": lat,
                "lon": lon,
                "alt": contact.get("adv_alt"),
                # MeshCore advertisements don't carry a first-class
                # timestamp — fall back to "now" so the UI treats these as
                # recently-heard rather than aging them out immediately.
                "last_update": _time.time(),
            }
        return result

    def get_node_meta(self) -> Dict[str, dict]:
        """Metadata for MC contacts (short_name / long_name / hops-unknown).

        Keyed by the same ``backend_native_id`` as ``get_node_positions``.
        Hops / RSSI / SNR aren't carried in contact advertisements, so those
        fields stay absent and the UI falls back to "fresh heard" heuristics.
        """
        result: Dict[str, dict] = {}
        for name, contact in self._contacts.items():
            pubkey = contact.get("public_key", "")
            prefix = pubkey[:12] if pubkey else name[:12]
            adv_name = contact.get("adv_name") or name
            result[prefix] = {
                "short_name": prefix[:6],
                "long_name": adv_name,
            }
        return result

    def get_self_info(self) -> dict:
        info = {"self_node_id": None}
        if self._meshcore is None or self._loop is None:
            return info
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._meshcore.commands.send_device_query(), self._loop
            )
            result = future.result(timeout=10)
            if hasattr(result, "payload") and isinstance(result.payload, dict):
                info.update(result.payload)
        except Exception as e:
            logger.debug(f"MeshCore device query failed: {e}")
        return info

    def get_connection_address(self) -> str:
        if self._connection_type == "ble":
            return self._ble_address or "scan"
        elif self._connection_type == "tcp":
            return f"{self._tcp_host}:{self._tcp_port}"
        return self._serial_port or "auto"

    # ── Internal: asyncio loop ───────────────────────────────────────────

    def _run_loop(self):
        """Background thread running the asyncio event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_main())
        except Exception as e:
            logger.error(f"MeshCore async loop error: {e}")
        finally:
            self._loop.close()
            self._loop = None

    async def _async_main(self):
        """Connect, subscribe to events, run until stopped."""
        try:
            if self._connection_type == "serial":
                self._meshcore = await MeshCore.create_serial(
                    self._serial_port, self._baudrate
                )
            elif self._connection_type == "tcp":
                self._meshcore = await MeshCore.create_tcp(
                    self._tcp_host, self._tcp_port
                )
            elif self._connection_type == "ble":
                self._meshcore = await MeshCore.create_ble(self._ble_address)
            else:
                raise ValueError(f"Unknown transport: {self._connection_type}")

            logger.info(f"Connected to MeshCore device ({self._connection_type})")

            # Start auto message fetching
            await self._meshcore.start_auto_message_fetching()

            # Subscribe to events
            sub_dm = self._meshcore.subscribe(
                EventType.CONTACT_MSG_RECV, self._on_contact_message
            )
            self._subscriptions.append(sub_dm)

            sub_ch = self._meshcore.subscribe(
                EventType.CHANNEL_MSG_RECV, self._on_channel_message
            )
            self._subscriptions.append(sub_ch)

            # Fetch initial contacts
            await self._refresh_contacts()

            # Run until stopped
            while self._running:
                await asyncio.sleep(5)
                try:
                    await self._refresh_contacts()
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"MeshCore connection failed: {e}")
            self._meshcore = None

    async def _async_disconnect(self):
        try:
            await self._meshcore.stop_auto_message_fetching()
        except Exception:
            pass
        for sub in self._subscriptions:
            try:
                self._meshcore.unsubscribe(sub)
            except Exception:
                pass
        self._subscriptions.clear()
        try:
            await self._meshcore.disconnect()
        except Exception:
            pass

    async def _refresh_contacts(self):
        if not self._meshcore:
            return
        result = await self._meshcore.commands.get_contacts()
        if hasattr(result, "type") and result.type == EventType.CONTACTS:
            self._contacts = result.payload or {}
        elif hasattr(result, "payload") and isinstance(result.payload, dict):
            self._contacts = result.payload

    # ── Internal: event handlers ─────────────────────────────────────────

    async def _on_contact_message(self, event):
        """Handle incoming DM from a MeshCore contact."""
        if self._callback is None:
            return
        try:
            payload = event.payload if hasattr(event, "payload") else event
            pubkey_prefix = payload.get("pubkey_prefix", "unknown")
            text = payload.get("text", "")
            sender_ts = payload.get("sender_timestamp", time.time())

            if not text or not text.strip():
                return

            display_name = self._resolve_contact_name(pubkey_prefix)
            short = pubkey_prefix[:6] if len(pubkey_prefix) > 6 else pubkey_prefix

            node = UnifiedNode(
                id=UnifiedNode.make_id(Protocol.MESHCORE, pubkey_prefix),
                protocol=Protocol.MESHCORE,
                backend_native_id=pubkey_prefix,
                short_name=short,
                long_name=display_name,
                last_heard=time.time(),
            )

            msg = UnifiedMessage(
                protocol=Protocol.MESHCORE,
                node=node,
                text=text.strip(),
                channel=0,
                is_dm=True,
                rssi=None,
                snr=None,
                timestamp=sender_ts if sender_ts else time.time(),
                raw_packet=payload,
            )
            self._callback(msg)
        except Exception as e:
            logger.error(f"Error converting MeshCore message: {e}")

    async def _on_channel_message(self, event):
        """Handle incoming channel/broadcast message."""
        if self._callback is None:
            return
        try:
            payload = event.payload if hasattr(event, "payload") else event
            pubkey_prefix = payload.get("pubkey_prefix", "unknown")
            text = payload.get("text", "")
            channel = payload.get("channel", 0)
            sender_ts = payload.get("sender_timestamp", time.time())

            if not text or not text.strip():
                return

            display_name = self._resolve_contact_name(pubkey_prefix)
            short = pubkey_prefix[:6] if len(pubkey_prefix) > 6 else pubkey_prefix

            node = UnifiedNode(
                id=UnifiedNode.make_id(Protocol.MESHCORE, pubkey_prefix),
                protocol=Protocol.MESHCORE,
                backend_native_id=pubkey_prefix,
                short_name=short,
                long_name=display_name,
                last_heard=time.time(),
            )

            msg = UnifiedMessage(
                protocol=Protocol.MESHCORE,
                node=node,
                text=text.strip(),
                channel=channel,
                is_dm=False,
                rssi=None,
                snr=None,
                timestamp=sender_ts if sender_ts else time.time(),
                raw_packet=payload,
            )
            self._callback(msg)
        except Exception as e:
            logger.error(f"Error converting MeshCore channel message: {e}")

    def _resolve_contact_name(self, pubkey_prefix: str) -> Optional[str]:
        """Look up a contact's advertised name by public key prefix."""
        for name, contact in self._contacts.items():
            pk = contact.get("public_key", "")
            if pk and pk.startswith(pubkey_prefix):
                return contact.get("adv_name") or name
        return None
