"""Abstract RadioBackend interface.

All radio protocol implementations (Meshtastic, MeshCore) implement this
interface.  The bridge core and RadioManager interact exclusively through
these methods — never through protocol-specific APIs.
"""

from abc import ABC, abstractmethod
from typing import Callable, Dict, List, Optional

from radio.events import Protocol, Transport, UnifiedMessage, UnifiedNode


class FeatureNotSupported(NotImplementedError):
    """Raised by a backend when a protocol-optional feature isn't available.

    Callers (RadioManager, dashboard handlers) should catch this and return
    a user-visible "not supported on this protocol" response — e.g. HTTP 501.
    Using a subclass of NotImplementedError keeps existing catch-alls happy
    while letting the dashboard distinguish "feature not wired on this radio"
    from a real crash.
    """


class RadioBackend(ABC):
    """Base class for radio protocol backends.

    Subclasses set ``protocol`` as a class attribute and ``backend_id`` in
    ``__init__``.  The ``backend_id`` must be unique across all running
    backends (e.g. ``"mt-serial-0"``).
    """

    protocol: Protocol
    backend_id: str
    transport: Transport

    # ── Lifecycle ────────────────────────────────────────────────────────

    @abstractmethod
    def connect(self) -> None:
        """Open the radio connection.  May start background threads."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection and release resources."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if the radio link is alive right now."""

    # ── Messaging ────────────────────────────────────────────────────────

    @abstractmethod
    def send_direct_message(self, to_native_id: str, text: str) -> None:
        """Send a DM to *to_native_id* (backend-local format, no prefix).

        Raises on failure.
        """

    @abstractmethod
    def send_broadcast(self, text: str, channel: int = 0) -> None:
        """Send a broadcast/channel message.  Raises on failure."""

    # ── Listening ────────────────────────────────────────────────────────

    @abstractmethod
    def start_listening(self, callback: Callable[[UnifiedMessage], None]) -> None:
        """Begin delivering incoming messages via *callback*.

        The callback is invoked from a background thread.  Implementations
        must ensure thread safety when calling it.
        """

    @abstractmethod
    def stop_listening(self) -> None:
        """Stop delivering messages.  Safe to call multiple times."""

    # ── Node / device info ───────────────────────────────────────────────

    @abstractmethod
    def get_nodes(self) -> Dict[str, UnifiedNode]:
        """Return a dict of ``{native_id: UnifiedNode}`` for all known nodes."""

    @abstractmethod
    def get_self_info(self) -> dict:
        """Return device metadata: firmware, model, own node id, etc."""

    # ── Optional extras ──────────────────────────────────────────────────

    def scan_ble_devices(self, timeout: float = 10.0) -> list:
        """Scan for BLE devices (optional, only BLE-capable backends)."""
        return []

    def get_node_positions(self) -> Dict[str, dict]:
        """Return ``{native_id: {lat, lon, alt, last_update}}`` for nodes
        with known GPS positions.  Default: empty dict."""
        return {}

    def get_node_meta(self) -> Dict[str, dict]:
        """Return ``{native_id: {hops, ...}}`` metadata.  Default: empty."""
        return {}

    # ── Protocol-optional features ───────────────────────────────────────
    # These are NOT abstract on purpose: each backend can opt in.  The default
    # raises FeatureNotSupported so the dashboard can distinguish "this radio
    # doesn't do that" from a genuine error.

    def send_traceroute(self, to_native_id: str, hop_limit: int = 7) -> None:
        """Probe the mesh path to *to_native_id*.  Raises FeatureNotSupported
        if this protocol has no traceroute equivalent."""
        raise FeatureNotSupported(
            f"{self.protocol.value}: traceroute not supported by this radio"
        )

    def get_radio_config(self) -> dict:
        """Return the LoRa/radio config as a serialisable dict.  Raises
        FeatureNotSupported if read-out isn't available."""
        raise FeatureNotSupported(
            f"{self.protocol.value}: reading radio config not supported by this radio"
        )

    def set_radio_config(self, config: dict) -> None:
        """Apply *config* to the radio (subset of keys).  Raises
        FeatureNotSupported if the backend can't write config."""
        raise FeatureNotSupported(
            f"{self.protocol.value}: writing radio config not supported by this radio"
        )
