"""LORACLE GREETER — proactively DM new mesh nodes a brief welcome message.

When the bridge sees a node it has never seen before (via text packet,
position packet, or nodeDB scan), the greeter queues a one-time DM that
identifies the bridge as an offline AI assistant and points the user at
!help. Greeted nodes are persisted to disk so a restart never re-greets
anyone.

Safety guarantees:
  - Persisted one-shot per node (forever)
  - Never DMs the local node id
  - Never DMs broadcast addresses ("^all", "!ffffffff")
  - Never DMs anything that doesn't look like a concrete Meshtastic node id
  - Startup grace period: pump() is a no-op for the first 90s after the
    process started, so the initial nodeDB flood can't trigger sends
  - First-ever startup pre-seeds the greeted set from interface.nodes via
    seed_from_nodedb(), so a fresh deployment doesn't blast every node
    that was on the mesh before LORACLE arrived
  - Global rate limit: 1 send per 10 seconds (queued FIFO)
"""

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Iterable, Optional

logger = logging.getLogger("greeter")

DEFAULT_GREETING = (
    "[LORACLE] Hi! I'm an AI assistant running fully offline on this mesh. "
    "DM me anything to ask \u2014 I'll reply in chunks. Send !help for commands."
)

# Anything that's clearly not a concrete unicast node id
_BROADCAST_TOKENS = frozenset({"^all", "!ffffffff", "!FFFFFFFF", "broadcast", ""})


class GreeterService:
    def __init__(
        self,
        path: str,
        message: str = DEFAULT_GREETING,
        self_id: Optional[str] = None,
        grace_period_s: int = 90,
        send_interval_s: int = 10,
        enabled: bool = True,
    ):
        self.path = path
        self.message = message
        self.self_id = self_id
        self.grace_period_s = int(grace_period_s)
        self.send_interval_s = int(send_interval_s)
        self.enabled = bool(enabled)

        self._lock = threading.Lock()
        self._greeted: set = set()
        self._queue: deque = deque()
        self._first_seed_done: bool = False
        self._started_at: float = time.time()
        self._last_send_ts: float = 0.0
        self._sent_count: int = 0

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception as e:
            logger.warning(f"Could not create greeter dir: {e}")

        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
            greeted = data.get("greeted", []) if isinstance(data, dict) else []
            with self._lock:
                self._greeted = set(str(x) for x in greeted if x)
                # If we have a non-empty persisted set, treat the first-seed
                # safety net as already done — this is not a fresh deployment.
                if self._greeted:
                    self._first_seed_done = True
            logger.info(f"Greeter: loaded {len(self._greeted)} previously-greeted nodes")
        except Exception as e:
            logger.warning(f"Could not read greeter state: {e}")

    def _save_unlocked(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"version": 1, "greeted": sorted(self._greeted)}, f)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.warning(f"Could not write greeter state: {e}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_self_id(self, self_id: Optional[str]) -> None:
        if not self_id:
            return
        with self._lock:
            self.self_id = self_id
            # Make sure we never sit in our own queue
            self._greeted.add(self_id)
            self._queue = deque(n for n in self._queue if n != self_id)
            self._save_unlocked()
        logger.info(f"Greeter: self id set to {self_id}")

    def mark_known(self, node_id: str) -> None:
        """Silently mark a node as already-greeted without sending anything."""
        if not self._is_concrete_node(node_id):
            return
        with self._lock:
            if node_id not in self._greeted:
                self._greeted.add(node_id)
                self._save_unlocked()

    def seed_from_nodedb(self, node_ids: Iterable[str]) -> None:
        """First-ever startup safety net: silently mark every node already
        in the radio's nodeDB as known, so we don't DM the entire mesh on
        initial deployment. No-op on subsequent calls (idempotent).
        """
        with self._lock:
            if self._first_seed_done:
                return
            added = 0
            for nid in node_ids:
                if not self._is_concrete_node(nid):
                    continue
                if nid not in self._greeted:
                    self._greeted.add(nid)
                    added += 1
            self._first_seed_done = True
            if added:
                self._save_unlocked()
        if added:
            logger.info(
                f"Greeter: silently marked {added} pre-existing nodeDB nodes as known "
                "(first-start safety net)"
            )

    def maybe_greet(self, node_id: str) -> None:
        """Queue a greeting for this node if it's eligible."""
        if not self.enabled:
            return
        if not self._is_concrete_node(node_id):
            return
        with self._lock:
            if self.self_id and node_id == self.self_id:
                return
            if node_id in self._greeted:
                return
            if node_id in self._queue:
                return  # already queued
            self._queue.append(node_id)
        logger.info(f"Greeter: queued greeting for new node {node_id}")

    def pump(self, interface=None, send_fn=None) -> None:
        """Send at most one queued greeting per call.

        Args:
            interface: Legacy meshtastic interface with ``sendText()``.
            send_fn: Callable ``(node_id, text) -> None`` — preferred.
                     If both are given, *send_fn* wins.
        """
        if not self.enabled:
            return
        if interface is None and send_fn is None:
            return
        now = time.time()
        # Startup grace
        if now - self._started_at < self.grace_period_s:
            return
        # Global send-rate limit
        if now - self._last_send_ts < self.send_interval_s:
            return

        with self._lock:
            target: Optional[str] = None
            while self._queue:
                candidate = self._queue.popleft()
                # Re-check eligibility right before sending
                if not self._is_concrete_node(candidate):
                    continue
                if self.self_id and candidate == self.self_id:
                    continue
                if candidate in self._greeted:
                    continue
                target = candidate
                break
            if target is None:
                return

        # Send outside the lock so a slow radio doesn't block other callers
        try:
            if send_fn is not None:
                send_fn(target, self.message)
            else:
                interface.sendText(
                    self.message,
                    destinationId=target,
                    channelIndex=0,
                    wantAck=False,
                )
        except Exception as e:
            logger.warning(f"Greeter: send to {target} failed: {e}")
            # Re-queue once at the back so a transient failure doesn't drop the greeting
            with self._lock:
                if target not in self._greeted and target not in self._queue:
                    self._queue.append(target)
            return

        with self._lock:
            self._greeted.add(target)
            self._last_send_ts = now
            self._sent_count += 1
            self._save_unlocked()
        logger.info(f"Greeter: sent greeting to {target}")

    def stats(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "message": self.message,
                "greeted_count": len(self._greeted),
                "queued": len(self._queue),
                "sent_this_session": self._sent_count,
                "self_id": self.self_id,
                "last_send_ts": self._last_send_ts,
                "first_seed_done": self._first_seed_done,
                "grace_remaining_s": max(
                    0, int(self.grace_period_s - (time.time() - self._started_at))
                ),
            }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_concrete_node(node_id) -> bool:
        if not node_id or not isinstance(node_id, str):
            return False
        if node_id in _BROADCAST_TOKENS:
            return False
        if not node_id.startswith("!"):
            return False
        # Meshtastic concrete IDs are "!" + 8 hex chars
        if len(node_id) != 9:
            return False
        try:
            int(node_id[1:], 16)
        except ValueError:
            return False
        # ffffffff is the broadcast address
        if node_id.lower() == "!ffffffff":
            return False
        return True
