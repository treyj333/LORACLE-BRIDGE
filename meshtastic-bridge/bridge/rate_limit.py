"""Per-direction rate limiter for the bridge relay.

Prevents a chatty or misbehaving network from saturating the other
network with relayed traffic. Limits are enforced per
``(source_protocol, dest_protocol, channel)`` tuple using a
sliding-window counter.

Defaults are conservative for defense-tech mesh use: 30 relays per
60 seconds per direction per channel. Operators can tune via config.
When the limit is exceeded, the relay records a drop instead of a
delivery — the dedup cache isn't touched, so the same message could
still cross once the window rolls over.
"""

import threading
import time
from collections import deque
from typing import Deque, Dict, Tuple

DEFAULT_MAX_EVENTS = 30
DEFAULT_WINDOW_S = 60.0


class RelayRateLimiter:
    """Thread-safe sliding-window rate limiter keyed per direction+channel."""

    def __init__(
        self,
        max_events: int = DEFAULT_MAX_EVENTS,
        window_seconds: float = DEFAULT_WINDOW_S,
    ):
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._max = max_events
        self._window = float(window_seconds)
        self._buckets: Dict[Tuple[str, str, int], Deque[float]] = {}
        self._lock = threading.Lock()

    def allow(
        self,
        source_protocol: str,
        dest_protocol: str,
        channel: int,
    ) -> bool:
        """Return True if this event fits in the current window.

        On True, the event is recorded (counts toward future limits).
        On False, nothing is recorded — the caller should treat this
        as "rate-limited, try again later" not "permanent drop".
        """
        key = (source_protocol, dest_protocol, int(channel))
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            q = self._buckets.get(key)
            if q is None:
                q = deque()
                self._buckets[key] = q
            # Drop stale events from the front of the deque.
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self._max:
                return False
            q.append(now)
            return True

    def current(
        self,
        source_protocol: str,
        dest_protocol: str,
        channel: int,
    ) -> int:
        """How many events are counted against the current window."""
        key = (source_protocol, dest_protocol, int(channel))
        cutoff = time.monotonic() - self._window
        with self._lock:
            q = self._buckets.get(key)
            if q is None:
                return 0
            while q and q[0] < cutoff:
                q.popleft()
            return len(q)

    def snapshot(self) -> Dict[str, int]:
        """Return a plain-dict view suitable for the /api/bridge/stats UI."""
        cutoff = time.monotonic() - self._window
        with self._lock:
            result: Dict[str, int] = {}
            for (src, dst, chan), q in self._buckets.items():
                while q and q[0] < cutoff:
                    q.popleft()
                result[f"{src}->{dst}:ch{chan}"] = len(q)
            return result

    def limits(self) -> Tuple[int, float]:
        """Current (max_events, window_seconds) — for UI display."""
        return self._max, self._window
