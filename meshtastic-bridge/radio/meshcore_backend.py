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


def _looks_like_hex(s: str) -> bool:
    """Cheap guard: does this string look like a valid pubkey prefix?
    MC pubkeys are even-length hex; the lib blows up with ValueError if
    you try to DM "unknown" or any non-hex value. We validate before
    sending so operators get a clear error instead of a cryptic one."""
    if not s or len(s) % 2 != 0:
        return False
    try:
        bytes.fromhex(s)
        return True
    except ValueError:
        return False


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
        # Device-query cache. get_self_info() is called on every /api/state
        # poll (every ~2s from the dashboard); without a cache the MC radio
        # was getting a send_device_query() round trip each poll, and every
        # timeout/drop left an asyncio Task pending in the event loop which
        # later logged "Task was destroyed but it is pending!" on GC.
        self._self_info_cache: Dict[str, object] = {"self_node_id": None}
        self._self_info_last_fetch: float = 0.0
        self._self_info_ttl: float = 30.0
        # Channel-table discovery — populated at connect-time by
        # _discover_channels(). The bridge uses self._public_channel_index
        # to route outgoing public-channel broadcasts to whichever slot the
        # MC device has named "Public" (case-insensitive), because the
        # meshcore send_chan_msg(idx, text) API takes a raw slot index,
        # not a semantic channel identifier. Without this, bridge-sourced
        # MT→MC relays all went to slot 0 regardless of whether slot 0
        # was the operator's public channel or a personal/unconfigured one.
        self._channel_table: list = []  # [{idx, name, hash}]
        self._public_channel_index: Optional[int] = None  # None = not yet discovered

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
        # Validate the target is a real pubkey prefix before handing it
        # to the MC lib — which otherwise raises a cryptic
        # ValueError("Invalid public key hex string: <whatever>").
        # Non-hex targets come from channel-broadcast pseudo-ids like
        # "anon:0:tn03" that shouldn't be DM'd at all (channel senders
        # are anonymous in the MC protocol).
        if not _looks_like_hex(to_native_id):
            raise ValueError(
                f"Cannot DM MeshCore node — '{to_native_id}' is not a valid pubkey "
                "(channel-broadcast senders are anonymous and cannot receive DMs). "
                "Wait for the sender to advertise their contact card, or send to "
                "the public channel instead."
            )
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
        # Public-channel remap: when the caller asks for channel 0 (the
        # bridge's default for "public"), substitute the slot index the
        # device actually has named "Public". Without this, bridge-sourced
        # MT→MC relays went to whatever happened to be in slot 0 on the
        # device — often a personal or unconfigured channel — and never
        # reached the public mesh. Explicit non-zero channel callers
        # (e.g. a dashboard send to a specific named channel) bypass the
        # remap and get the literal index they passed.
        resolved = channel
        if channel == 0 and self._public_channel_index is not None:
            resolved = self._public_channel_index
        if resolved != channel:
            logger.info(
                f"MC send_broadcast start: ch=0 → slot {resolved} (Public) len={len(text)}"
            )
        else:
            logger.info(
                f"MC send_broadcast start: ch={resolved} len={len(text)}"
            )
        future = asyncio.run_coroutine_threadsafe(
            self._meshcore.commands.send_chan_msg(resolved, text),
            self._loop,
        )
        result = future.result(timeout=30)
        # Loud per-send logging so we can tell the difference between "radio
        # returned OK and we relied on it" vs "lib returned an error we
        # silently upgraded to a string." An OK result lets send_broadcast
        # return successfully; anything else raises.
        res_type = getattr(result, "type", None)
        if res_type == EventType.ERROR:
            logger.warning(
                f"MC send_broadcast ERROR from lib: {getattr(result, 'payload', None)!r}"
            )
            raise RuntimeError(f"MeshCore broadcast failed: {result.payload}")
        logger.info(f"MC send_broadcast ok: result_type={res_type} slot={resolved}")

    # ── Protocol-optional features ───────────────────────────────────────
    # MeshCore has no traceroute or LoRa-config-write surface yet; only a
    # read-only device query.  send_traceroute + set_radio_config fall back
    # to the base class (FeatureNotSupported → dashboard returns 501).

    def get_radio_config(self) -> dict:
        """Read-only view of whatever the MeshCore device_query returns.

        MeshCore's firmware doesn't expose LoRa config like Meshtastic does,
        so we hand the dashboard what the device *does* share (firmware ver,
        hw model, etc.) under a ``read_only`` flag so the UI can grey out
        write fields.  Still better than a 501 — shows MC isn't a dead zone.
        """
        if not self.is_connected() or self._loop is None:
            raise ConnectionError("MeshCore radio not connected")
        info = self.get_self_info() or {}
        return {"read_only": True, "device": info}

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
        """Return cached device info (hw / fw / self_node_id).

        Cached for ``self._self_info_ttl`` seconds (default 30s). The
        dashboard polls ``/api/state`` every ~2s; without the cache, every
        poll fired a ``send_device_query()`` round trip at the MC radio,
        and any timeout left an asyncio Task pending in the event loop
        (surfaced later as "Task was destroyed but it is pending!" when
        GC collected the coroutine). We also cancel the underlying future
        on timeout so the task is torn down cleanly instead of lingering.
        """
        if self._meshcore is None or self._loop is None:
            return dict(self._self_info_cache)
        now = time.time()
        if (now - self._self_info_last_fetch) < self._self_info_ttl:
            return dict(self._self_info_cache)
        future = None
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._meshcore.commands.send_device_query(), self._loop
            )
            result = future.result(timeout=5)
            if hasattr(result, "payload") and isinstance(result.payload, dict):
                # Merge in place so callers see the latest fields without
                # overwriting any we've cached from a prior successful run.
                self._self_info_cache.update(result.payload)
            self._self_info_last_fetch = now
        except Exception as e:
            # Timeout or any other error — cancel the still-running
            # coroutine so it doesn't linger as a pending Task on the
            # event loop (which is what produces the GC warning).
            if future is not None and not future.done():
                try:
                    future.cancel()
                except Exception:
                    pass
            # Push the next retry out by the full TTL instead of hammering
            # the radio on every 2s dashboard poll when it's misbehaving.
            self._self_info_last_fetch = now
            logger.debug(f"MeshCore device query failed: {e}")
        return dict(self._self_info_cache)

    def get_connection_address(self) -> str:
        if self._connection_type == "ble":
            return self._ble_address or "scan"
        elif self._connection_type == "tcp":
            return f"{self._tcp_host}:{self._tcp_port}"
        return self._serial_port or "auto"

    # ── Channel-table introspection ──────────────────────────────────────

    def get_public_channel_index(self) -> Optional[int]:
        """Return the slot index of the MC device's Public channel.

        Discovered at connect-time by _discover_channels(). None means
        discovery hasn't run yet (backend not fully connected); 0 is the
        fallback used when no channel named "Public" was found on the
        device. The bridge's send_broadcast() uses this value to remap
        incoming channel=0 requests to the operator's actual public slot.
        """
        return self._public_channel_index

    def get_channel_table(self) -> list:
        """Copy of the discovered channel table (list of {idx, name, hash})."""
        return list(self._channel_table)

    # ── Internal: asyncio loop ───────────────────────────────────────────

    async def _discover_channels(self) -> None:
        """Enumerate the MC device's channel table and pick the Public slot.

        MeshCore's firmware exposes up to 16 channel slots, each with a
        name + PSK derived from the name hash (or an explicit secret). The
        ``meshcore.commands.get_channel(idx)`` call returns a CHANNEL_INFO
        event with ``{channel_idx, channel_name, channel_secret,
        channel_hash}`` — see meshcore/reader.py:475-494. We iterate slots
        0..15, collect the named ones, and pick the slot whose name
        matches "public" (case-insensitive, also "public channel" /
        "public ch"). That slot index becomes self._public_channel_index
        and gets substituted for channel=0 in outgoing bridge broadcasts.

        Fallback: if no Public-named slot exists, we keep the 0 default
        but log every discovered slot so the operator can see exactly
        what's on the device and rename one.
        """
        if not self._meshcore:
            self._public_channel_index = 0
            return

        slots = []
        # Scan the full 16-slot range (MC channel tables are bounded at
        # 16 slots per firmware). Don't break on ERROR / empty — MC
        # allows sparse tables (e.g. slot 0 configured, slot 1 empty,
        # slot 2 configured), so a single ERROR doesn't mean "end of
        # table." 16 * ~3s timeout upper bound is ~48s worst case, but
        # typical devices respond in <50ms each so this runs in well
        # under a second.
        for idx in range(0, 16):
            try:
                result = await asyncio.wait_for(
                    self._meshcore.commands.get_channel(idx), timeout=3.0
                )
            except asyncio.TimeoutError:
                logger.debug(f"[mc] get_channel({idx}) timed out — skipping")
                continue
            except Exception as e:
                logger.debug(f"[mc] get_channel({idx}) raised {e!r} — skipping")
                continue
            if not hasattr(result, "type"):
                continue
            if result.type == EventType.ERROR:
                continue
            payload = getattr(result, "payload", None) or {}
            name = str(payload.get("channel_name") or "").strip()
            # Skip empty slots (unnamed / unused).
            if not name:
                continue
            slots.append({
                "idx": idx,
                "name": name,
                "hash": payload.get("channel_hash"),
            })

        self._channel_table = slots

        # Match anything that reads as "public" — common variants include
        # "Public", "public", "Public Channel", "Public Ch", and "#public"
        # (leading-# triggers MC's auto-hash-key behavior).
        def _is_public(name: str) -> bool:
            n = name.lower().strip().lstrip("#").strip()
            return n == "public" or n.startswith("public ") or n == "public channel"

        match = next((s for s in slots if _is_public(s["name"])), None)
        if match is not None:
            self._public_channel_index = match["idx"]
            logger.info(
                f"[mc] public channel resolved: idx={match['idx']} name={match['name']!r} "
                f"(table: {[(s['idx'], s['name']) for s in slots]})"
            )
        else:
            self._public_channel_index = 0
            if slots:
                logger.warning(
                    f"[mc] no channel named 'Public' found on the device — defaulting "
                    f"outbound public-channel sends to slot 0. Configured slots: "
                    f"{[(s['idx'], s['name']) for s in slots]}. Rename one of these to "
                    "'Public' on the MC device (or set_channel via the companion app) "
                    "so cross-protocol relay can reach the public channel."
                )
            else:
                logger.warning(
                    "[mc] channel table is empty — defaulting to slot 0. The MC "
                    "device has no configured channels; configure at least one "
                    "named 'Public' for cross-protocol relay to work."
                )

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

            # Enumerate the device's channel table once at connect-time.
            # Without this we'd blindly pass channel=0 to send_chan_msg,
            # which only works if slot 0 happens to be the operator's
            # public channel. See _discover_channels() docstring.
            try:
                await self._discover_channels()
            except Exception as e:
                logger.warning(f"[mc] channel discovery failed: {e} — falling back to channel index 0")
                self._public_channel_index = 0

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
            pk_raw = str(payload.get("pubkey_prefix") or "").strip()
            if not _looks_like_hex(pk_raw):
                # DM without a valid sender pubkey — the MC protocol
                # guarantees one on CONTACT_MSG_RECV, so this only
                # happens on malformed/truncated frames. Log and drop
                # rather than synthesizing a fake "unknown" contact.
                logger.warning(
                    f"[mc] DM received with invalid pubkey_prefix={pk_raw!r} — dropping"
                )
                return
            pubkey_prefix = pk_raw
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
        """Handle incoming channel/broadcast message.

        MC channel broadcasts are ANONYMOUS by design — the firmware's
        CHANNEL_MSG_RECV payload carries no sender pubkey (see
        meshcore/reader.py:280-294, fields are channel_idx / text /
        sender_timestamp but NO pubkey_prefix). Most MC companion apps
        embed the sender's name in the text as ``"<Name>: <text>"`` by
        convention — we parse that here for display + relay tagging.
        The resulting sender id is a deterministic pseudo-id
        (``mc:anon:<slug>``) so repeated messages from the same named
        sender group into one "thread" but DM attempts fail cleanly
        (send_direct_message validates hex — see that method).
        Previously: payload.get("pubkey_prefix", "unknown") fell back to
        the literal string "unknown", producing the ID "mc:unknown"
        that the UI surfaced as a contact. Users who tried to DM it
        got ValueError("Invalid public key hex string: unknown") from
        the MC lib, and AI auto-reply silently failed the same way.
        """
        if self._callback is None:
            return
        try:
            payload = event.payload if hasattr(event, "payload") else event
            text = payload.get("text", "")
            channel = payload.get("channel", 0)
            sender_ts = payload.get("sender_timestamp", time.time())

            if not text or not text.strip():
                return

            # Only trust pubkey_prefix if it's actually a non-empty hex
            # string. MC lib doesn't set it on channel messages; if some
            # future firmware does, gracefully accept the hex value.
            pk_prefix_raw = str(payload.get("pubkey_prefix") or "").strip()
            pk_prefix = pk_prefix_raw if _looks_like_hex(pk_prefix_raw) else ""

            display_name: Optional[str] = None
            text_stripped = text.strip()

            if pk_prefix:
                display_name = self._resolve_contact_name(pk_prefix)
                short = pk_prefix[:6]
                native_id = pk_prefix
            else:
                # Parse "Name: text" convention (e.g. "TN03: hello").
                # Name must be <= 16 chars of letters/digits/_-/space.
                # Match at beginning; strip the prefix from the visible
                # text for relay tagging so downstream consumers don't
                # re-prefix the sender name.
                import re
                m = re.match(r"^([A-Za-z0-9_\- ]{1,16})\s*:\s*(.+)$", text_stripped)
                if m:
                    display_name = m.group(1).strip()
                    text_stripped = m.group(2).strip() or text_stripped
                # Pseudo-id so repeated anon messages from the same named
                # sender aggregate, but the id never matches valid hex so
                # DM attempts fail fast (send_direct_message guard).
                slug = re.sub(r"[^a-z0-9]+", "-", (display_name or "").lower()).strip("-") or "anon"
                native_id = f"anon:{channel}:{slug}"
                short = display_name or "anon"

            node = UnifiedNode(
                id=UnifiedNode.make_id(Protocol.MESHCORE, native_id),
                protocol=Protocol.MESHCORE,
                backend_native_id=native_id,
                short_name=short[:6],
                long_name=display_name,
                last_heard=time.time(),
            )

            msg = UnifiedMessage(
                protocol=Protocol.MESHCORE,
                node=node,
                text=text_stripped,
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
