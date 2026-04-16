"""Unified event and data types for the RadioBackend abstraction layer."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class Protocol(Enum):
    """Supported radio protocols."""
    MESHTASTIC = "mt"
    MESHCORE = "mc"


class Transport(Enum):
    """Physical connection transports."""
    SERIAL = "serial"
    TCP = "tcp"
    BLE = "ble"


class BackendEvent(Enum):
    """Internal lifecycle events emitted by backends."""
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"


@dataclass
class UnifiedNode:
    """Protocol-agnostic representation of a remote node/contact.

    The ``id`` field is globally unique across protocols, formatted as
    ``"<protocol_short>:<backend_native_id>"`` — for example
    ``"mt:!a3f2b8c1"`` (Meshtastic) or ``"mc:abcdef012345"`` (MeshCore).
    """
    id: str
    protocol: Protocol
    backend_native_id: str
    short_name: str = ""
    long_name: Optional[str] = None
    rssi: Optional[int] = None
    snr: Optional[float] = None
    hops_away: Optional[int] = None
    last_heard: Optional[float] = None
    has_position: bool = False
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt: Optional[float] = None

    @staticmethod
    def make_id(protocol: Protocol, native_id: str) -> str:
        """Build a globally unique node ID from protocol + backend-native id."""
        return f"{protocol.value}:{native_id}"

    @staticmethod
    def parse_id(unified_id: str):
        """Split a unified ID into (protocol_short, native_id).

        Returns:
            Tuple of (protocol_value: str, native_id: str).
            For ``"mt:!a3f2b8c1"`` → ``("mt", "!a3f2b8c1")``.
        """
        sep = unified_id.index(":")
        return unified_id[:sep], unified_id[sep + 1:]


@dataclass
class UnifiedMessage:
    """Protocol-agnostic incoming message.

    Produced by RadioBackend implementations and consumed by the bridge's
    processing pipeline (dedup, rate-limit, command dispatch, LLM call).
    """
    protocol: Protocol
    node: UnifiedNode
    text: str
    channel: int = 0
    is_dm: bool = True
    rssi: Optional[int] = None
    snr: Optional[float] = None
    timestamp: float = 0.0
    hops_traveled: Optional[int] = None
    raw_packet: Optional[Dict[str, Any]] = field(default=None, repr=False)
