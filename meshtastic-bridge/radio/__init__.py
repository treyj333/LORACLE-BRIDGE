"""RadioBackend abstraction layer for multi-protocol radio support.

Provides a unified interface so the bridge core can talk to Meshtastic,
MeshCore, or both simultaneously without protocol-specific code.
"""

from radio.events import (
    BackendEvent,
    Protocol,
    Transport,
    UnifiedMessage,
    UnifiedNode,
)
from radio.backend import RadioBackend
from radio.manager import RadioManager

__all__ = [
    "BackendEvent",
    "Protocol",
    "Transport",
    "UnifiedMessage",
    "UnifiedNode",
    "RadioBackend",
    "RadioManager",
]
