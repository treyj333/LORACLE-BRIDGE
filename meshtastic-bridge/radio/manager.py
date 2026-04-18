"""RadioManager — runs multiple RadioBackend instances concurrently.

Provides a single message queue that multiplexes incoming messages from
all backends, and routes outgoing sends to the correct backend based on
the protocol prefix in the unified node ID.
"""

import logging
import queue
import threading
from typing import Callable, Dict, List, Optional

from radio.events import Protocol, UnifiedMessage, UnifiedNode
from radio.backend import RadioBackend, FeatureNotSupported

logger = logging.getLogger("radio.manager")


class RadioManager:
    """Orchestrates one or more RadioBackend instances."""

    def __init__(self):
        self._backends: Dict[str, RadioBackend] = {}
        self._message_queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._event_callbacks: List[Callable] = []

    # ── Backend lifecycle ────────────────────────────────────────────────

    def add_backend(self, backend: RadioBackend) -> None:
        """Register and connect a backend.  Starts listening immediately."""
        with self._lock:
            if backend.backend_id in self._backends:
                raise ValueError(f"Backend '{backend.backend_id}' already registered")
            self._backends[backend.backend_id] = backend
        logger.info(f"Added backend: {backend.backend_id} ({backend.protocol.value})")
        try:
            backend.connect()
            backend.start_listening(self._on_message)
        except Exception as e:
            logger.error(f"Failed to start backend {backend.backend_id}: {e}")
            with self._lock:
                self._backends.pop(backend.backend_id, None)
            raise

    def remove_backend(self, backend_id: str) -> None:
        """Disconnect and remove a backend."""
        with self._lock:
            backend = self._backends.pop(backend_id, None)
        if backend is None:
            return
        logger.info(f"Removing backend: {backend_id}")
        try:
            backend.stop_listening()
            backend.disconnect()
        except Exception as e:
            logger.warning(f"Error removing backend {backend_id}: {e}")

    def start_all(self) -> None:
        """Connect and start listening on all registered backends.

        Backends that were already added via ``add_backend`` (which connects
        eagerly) are skipped.
        """
        pass  # add_backend already connects eagerly

    def stop_all(self) -> None:
        """Stop and disconnect all backends."""
        ids = list(self._backends.keys())
        for bid in ids:
            self.remove_backend(bid)

    # ── Incoming messages ────────────────────────────────────────────────

    def _on_message(self, msg: UnifiedMessage) -> None:
        """Callback given to each backend's ``start_listening``."""
        self._message_queue.put(msg)

    def get_message(self, timeout: float = 1.0) -> Optional[UnifiedMessage]:
        """Block until a message arrives or *timeout* seconds elapse.

        Returns ``None`` on timeout.
        """
        try:
            return self._message_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    # ── Outgoing messages ────────────────────────────────────────────────

    def send(
        self,
        unified_node_id: str,
        text: str,
        channel: int = 0,
        is_dm: bool = True,
    ) -> None:
        """Route a message to the correct backend by protocol prefix.

        *unified_node_id* is ``"mt:!a3f2b8c1"`` or ``"mc:abcdef012345"``.
        If no prefix is found, the primary (first-added) backend is used.
        """
        protocol_short, native_id = self._parse_node_id(unified_node_id)
        backend = self._find_backend_for_protocol(protocol_short)
        if backend is None:
            raise ValueError(
                f"No connected backend for protocol '{protocol_short}'"
            )
        if is_dm:
            backend.send_direct_message(native_id, text)
        else:
            backend.send_broadcast(text, channel)

    # ── Protocol-optional feature dispatch ───────────────────────────────
    # These mirror the optional methods on RadioBackend.  They pick a backend
    # by unified node id (for traceroute) or by explicit backend_id (for
    # config).  If the target backend doesn't support the feature it raises
    # FeatureNotSupported, which callers surface as HTTP 501.

    def send_traceroute(self, unified_node_id: str, hop_limit: int = 7) -> None:
        """Probe the route to *unified_node_id* using the backend matching
        its protocol prefix.  Raises FeatureNotSupported on MeshCore and any
        future backend without a traceroute equivalent."""
        protocol_short, native_id = self._parse_node_id(unified_node_id)
        backend = self._find_backend_for_protocol(protocol_short)
        if backend is None:
            raise ValueError(
                f"No connected backend for protocol '{protocol_short}'"
            )
        backend.send_traceroute(native_id, hop_limit)

    def get_radio_config(self, backend_id: Optional[str] = None) -> dict:
        """Read the LoRa / radio config from a specific backend.

        If *backend_id* is None, uses the primary (first-added) backend so
        legacy callers keep working.  Always returns a dict that includes a
        ``backend_id`` / ``protocol`` so the UI knows which radio it's for.
        """
        backend = self._pick_backend(backend_id)
        data = backend.get_radio_config()
        data.setdefault("backend_id", backend.backend_id)
        data.setdefault("protocol", backend.protocol.value)
        return data

    def set_radio_config(
        self, config: dict, backend_id: Optional[str] = None,
    ) -> None:
        """Apply *config* to a specific backend.  Raises FeatureNotSupported
        if the target backend is read-only (e.g. MeshCore)."""
        backend = self._pick_backend(backend_id)
        backend.set_radio_config(config)

    def _pick_backend(self, backend_id: Optional[str]) -> RadioBackend:
        """Resolve *backend_id* to a connected backend, or fall back to the
        primary.  Raises ValueError if nothing suitable is connected."""
        with self._lock:
            if backend_id:
                b = self._backends.get(backend_id)
                if b is None or not b.is_connected():
                    raise ValueError(f"Backend '{backend_id}' not connected")
                return b
        primary = self.get_primary_backend()
        if primary is None or not primary.is_connected():
            raise ValueError("No connected backend")
        return primary

    # ── Node queries ─────────────────────────────────────────────────────

    def get_all_nodes(self) -> Dict[str, UnifiedNode]:
        """Merge nodes from all backends.  Keys are unified IDs."""
        merged = {}
        with self._lock:
            backends = list(self._backends.values())
        for backend in backends:
            try:
                for native_id, node in backend.get_nodes().items():
                    merged[node.id] = node
            except Exception as e:
                logger.debug(f"Error getting nodes from {backend.backend_id}: {e}")
        return merged

    def get_all_node_positions(self) -> Dict[str, dict]:
        """Merge GPS positions from all backends.  Keys are unified IDs."""
        merged = {}
        with self._lock:
            backends = list(self._backends.values())
        for backend in backends:
            try:
                positions = backend.get_node_positions()
                for native_id, pos in positions.items():
                    uid = UnifiedNode.make_id(backend.protocol, native_id)
                    merged[uid] = pos
            except Exception:
                pass
        return merged

    def get_all_node_meta(self) -> Dict[str, dict]:
        """Merge node metadata from all backends.  Keys are unified IDs."""
        merged = {}
        with self._lock:
            backends = list(self._backends.values())
        for backend in backends:
            try:
                meta = backend.get_node_meta()
                for native_id, m in meta.items():
                    uid = UnifiedNode.make_id(backend.protocol, native_id)
                    merged[uid] = m
            except Exception:
                pass
        return merged

    # ── Backend info ─────────────────────────────────────────────────────

    def get_backends(self) -> List[RadioBackend]:
        """Return a snapshot list of all backends (thread-safe copy)."""
        with self._lock:
            return list(self._backends.values())

    def get_backends_info(self) -> List[dict]:
        """Return serialisable info for each backend (for dashboard API)."""
        result = []
        with self._lock:
            backends = list(self._backends.values())
        for b in backends:
            try:
                info = b.get_self_info()
            except Exception:
                info = {}
            result.append({
                "id": b.backend_id,
                "protocol": b.protocol.value,
                "transport": b.transport.value,
                "connected": b.is_connected(),
                **info,
            })
        return result

    def has_connected_backend(self) -> bool:
        """Return True if at least one backend is connected."""
        with self._lock:
            return any(b.is_connected() for b in self._backends.values())

    def get_primary_backend(self) -> Optional[RadioBackend]:
        """Return the first-added backend (the 'primary')."""
        with self._lock:
            for b in self._backends.values():
                return b
        return None

    # ── Internal helpers ─────────────────────────────────────────────────

    @staticmethod
    def _parse_node_id(unified_id: str):
        """Split ``"mt:!a3f2b8c1"`` → ``("mt", "!a3f2b8c1")``."""
        if ":" not in unified_id:
            # No prefix — assume primary protocol (backward compat)
            return "", unified_id
        idx = unified_id.index(":")
        return unified_id[:idx], unified_id[idx + 1:]

    def _find_backend_for_protocol(self, protocol_short: str) -> Optional[RadioBackend]:
        """Find a connected backend matching the protocol prefix."""
        with self._lock:
            # Exact protocol match
            for b in self._backends.values():
                if b.protocol.value == protocol_short and b.is_connected():
                    return b
            # Fallback: if no prefix given, return primary
            if not protocol_short:
                for b in self._backends.values():
                    if b.is_connected():
                        return b
        return None
