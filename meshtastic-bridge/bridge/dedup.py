"""Dedup cache for the relay — prevents the same message looping across networks.

A message is fingerprinted as (source_protocol, dest_protocol, sender,
text_hash). If we've relayed this exact message within the TTL window,
we skip it. Essential when bidirectional relay could otherwise feed back
(A→B, the echo on B looks new, B→A again, ...).

The ``looks_bridged`` prefix check in identity.py is the first line of
defence; this cache catches messages that slip through (e.g. a user on
network B manually re-types a message that originated on network A).
"""

import hashlib
import threading
import time
from typing import Dict, Tuple

DEFAULT_TTL_S = 300  # 5 minutes — matches the existing send-dedup in standalone_bridge


class RelayDedupCache:
    """Thread-safe TTL cache keyed on relay-event fingerprints."""

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_S):
        self._ttl = ttl_seconds
        self._entries: Dict[Tuple[str, str, str], float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _fingerprint(sender: str, text: str) -> str:
        """Stable 16-hex hash for (sender, trimmed text).

        Trimming text before hashing means whitespace-only differences
        don't defeat the dedup — matters when the relay prefixes the
        message with a sender tag and the ``.strip()`` contract may vary.
        """
        h = hashlib.sha1()
        h.update(sender.encode("utf-8", errors="ignore"))
        h.update(b"|")
        h.update(text.strip().encode("utf-8", errors="ignore"))
        return h.hexdigest()[:16]

    def seen(
        self,
        source_protocol: str,
        dest_protocol: str,
        sender: str,
        text: str,
    ) -> bool:
        """True iff this relay was already performed within the TTL."""
        key = (source_protocol, dest_protocol, self._fingerprint(sender, text))
        now = time.time()
        with self._lock:
            ts = self._entries.get(key)
            return ts is not None and (now - ts) < self._ttl

    def record(
        self,
        source_protocol: str,
        dest_protocol: str,
        sender: str,
        text: str,
    ) -> None:
        """Mark this relay as performed. Call after a successful send."""
        key = (source_protocol, dest_protocol, self._fingerprint(sender, text))
        with self._lock:
            self._entries[key] = time.time()

    def cleanup(self) -> int:
        """Evict stale entries. Returns number removed."""
        cutoff = time.time() - self._ttl
        with self._lock:
            stale = [k for k, ts in self._entries.items() if ts < cutoff]
            for k in stale:
                del self._entries[k]
        return len(stale)

    def size(self) -> int:
        with self._lock:
            return len(self._entries)
