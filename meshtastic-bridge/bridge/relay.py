"""Cross-protocol message relay — the v2 Phase 2 bridge core.

Observes every inbound message on either network via ``Relay.observe()``,
consults policy + dedup + loop-guard, then asks the caller-supplied
``send_fn(dest_protocol, text, channel)`` to re-broadcast on the other
network with a sender-network prefix.

Intentional boundaries:
  - The relay does NOT drive radios. It asks a send_fn. StandaloneBridge
    wires that to the right native send for each protocol.
  - DMs are never relayed in Phase 2. Crossing private conversations is
    a trust decision that belongs in a Phase 3 UI opt-in.
  - ``looks_bridged`` text is never re-relayed (loop guard #1). The
    dedup cache is loop guard #2 for messages that slip past the prefix
    check (e.g. a user manually re-typing a bridged message).
"""

import logging
import re
from typing import Callable, Dict, List, Optional

from bridge.dedup import RelayDedupCache
from bridge.identity import format_bridged, looks_bridged
from bridge.policy import Policy
from bridge.rate_limit import RelayRateLimiter

logger = logging.getLogger("bridge.relay")

# Phase 2 destination set. Add future protocols here — the relay is
# otherwise agnostic to which ones exist.
_ALL_PROTOCOLS = ("meshtastic", "meshcore")

# Phase 4: "force relay" prefixes. Messages starting with any of these
# bypass the policy (AI gate, channel allowlist — everything) and cross
# the bridge unconditionally. The prefix is stripped before relaying so
# recipients see the raw message. Trailing whitespace after the bang-
# word is consumed too.
_FORCE_RELAY_PREFIX_RE = re.compile(
    r"^!(urgent|priority|sos|mayday)\b[\s:,-]*",
    re.IGNORECASE,
)


class Relay:
    """Owns the relay decision + dedup bookkeeping.

    Typical usage inside StandaloneBridge::

        relay = Relay(
            send_fn=self._bridge_send,
            policy=build_policy(cfg),
            identity_fn=self._bridge_sender_display,
            on_relay=self._bridge_event_hook,
        )
        # In each receive path, after persistence:
        relay.observe("meshtastic", sender=sender, text=text, channel=0, is_dm=False)
    """

    def __init__(
        self,
        send_fn: Callable[[str, str, int], None],
        policy: Policy,
        dedup: Optional[RelayDedupCache] = None,
        identity_fn: Optional[Callable[[str, str], str]] = None,
        on_relay: Optional[Callable[[dict], None]] = None,
        rate_limiter: Optional[RelayRateLimiter] = None,
    ):
        self._send = send_fn
        self._policy = policy
        self._dedup = dedup if dedup is not None else RelayDedupCache()
        self._identity_fn = identity_fn
        self._on_relay = on_relay
        # Phase 5: per-direction rate limiter. Passing None disables
        # rate limiting — some tests and the Phase 2 integration path
        # opt out. Production always gets a limiter from the bridge.
        self._rate_limiter = rate_limiter
        self._relay_count = 0
        self._drop_count = 0
        self._rate_limited_count = 0
        # Per-direction breakdown so the BRIDGE dashboard can show two
        # mirrored MT↔MC columns instead of a single combined total.
        # Keys are "<source>-><dest>" using the same protocol strings the
        # rest of this module emits (e.g. "meshtastic->meshcore").
        self._relayed_by_direction: Dict[str, int] = {}
        self._dropped_by_direction: Dict[str, int] = {}

    def _bump_direction(
        self, counter: Dict[str, int], source: str, dest: str,
    ) -> None:
        """Bump the per-direction counter dict by one for the ``source->dest``
        key.  Used alongside the aggregate counters so the dashboard can show
        MT↔MC traffic side-by-side without a second pass over the event log."""
        key = f"{source}->{dest}"
        counter[key] = counter.get(key, 0) + 1

    def set_policy(self, policy: Policy) -> None:
        """Hot-swap policy (for live config reloads). Thread-safe enough —
        assignment to an attribute is atomic in CPython."""
        self._policy = policy

    def observe(
        self,
        source_protocol: str,
        sender: str,
        text: str,
        channel: int,
        is_dm: bool,
    ) -> List[str]:
        """Entry point: a message just arrived on ``source_protocol``.

        Returns the list of destination protocols the message was actually
        relayed to. May be empty when policy/dedup/loop-guard blocks it.
        """
        if not text or not text.strip():
            return []

        # Loop guard #1: already-bridged text never re-relays.
        if looks_bridged(text):
            self._drop_count += 1
            logger.debug(f"[bridge] skip already-bridged from {sender}: {text[:60]!r}")
            return []

        # Phase 4: force-relay prefix. Strip the tag and bypass policy
        # (including the AI gate). Policy still can't relay DMs — the
        # prefix only overrides the channel/urgency gate.
        force_relay = False
        m = _FORCE_RELAY_PREFIX_RE.match(text)
        if m:
            force_relay = True
            text = text[m.end():]
            if not text.strip():
                # Bang-word on its own — nothing to forward.
                self._drop_count += 1
                return []
            logger.info(
                f"[bridge] force-relay from {sender} "
                f"(prefix={m.group(1).lower()}): {text[:60]}"
            )

        delivered: List[str] = []
        for dest in _ALL_PROTOCOLS:
            if dest == source_protocol:
                continue
            # DMs still never relay, even with !urgent — bridging private
            # conversation is a trust decision, not a priority decision.
            if is_dm:
                self._drop_count += 1
                self._bump_direction(
                    self._dropped_by_direction, source_protocol, dest,
                )
                continue
            if not force_relay and not self._policy.should_relay(
                source_protocol, dest, sender, text, channel, is_dm
            ):
                self._drop_count += 1
                self._bump_direction(
                    self._dropped_by_direction, source_protocol, dest,
                )
                continue
            # Loop guard #2: within-TTL fingerprint match.
            if self._dedup.seen(source_protocol, dest, sender, text):
                self._drop_count += 1
                self._bump_direction(
                    self._dropped_by_direction, source_protocol, dest,
                )
                logger.debug(
                    f"[bridge] dedup suppressed {source_protocol}->{dest} "
                    f"from {sender}: {text[:40]!r}"
                )
                continue

            # Phase 5: rate limit. Sliding window per direction+channel.
            # Force-relay bypasses the limit — if someone is spamming
            # !urgent, let the dedup handle noise rather than dropping
            # the third urgent message of a real incident.
            if (
                self._rate_limiter is not None
                and not force_relay
                and not self._rate_limiter.allow(source_protocol, dest, channel)
            ):
                self._rate_limited_count += 1
                self._drop_count += 1
                self._bump_direction(
                    self._dropped_by_direction, source_protocol, dest,
                )
                logger.info(
                    f"[bridge] rate-limited {source_protocol}->{dest} ch{channel} "
                    f"from {sender}"
                )
                continue

            sender_display = (
                self._identity_fn(source_protocol, sender)
                if self._identity_fn is not None
                else (sender[-6:] if len(sender) > 6 else sender)
            )
            prefixed = format_bridged(source_protocol, sender_display, text)

            try:
                self._send(dest, prefixed, channel)
            except Exception as e:
                logger.warning(
                    f"[bridge] relay {source_protocol}->{dest} send failed: {e}"
                )
                continue

            self._dedup.record(source_protocol, dest, sender, text)
            self._relay_count += 1
            self._bump_direction(
                self._relayed_by_direction, source_protocol, dest,
            )
            delivered.append(dest)
            logger.info(
                f"[bridge] {source_protocol}->{dest} ch{channel} "
                f"{sender_display}: {text[:60]}"
            )
            if self._on_relay is not None:
                try:
                    self._on_relay({
                        "source": source_protocol,
                        "dest": dest,
                        "sender": sender,
                        "sender_display": sender_display,
                        "channel": channel,
                        "text": prefixed,
                    })
                except Exception as e:
                    logger.debug(f"[bridge] on_relay hook error: {e}")

        return delivered

    def stats(self) -> dict:
        """Counters for the BRIDGE UI tab (Phase 3+).

        Includes a ``by_direction`` block so the dashboard can show MT↔MC
        traffic in mirrored columns.  Keys are ``"<src>-><dst>"`` using the
        same protocol strings the relay emits on events — the UI shortens
        them to ``mt->mc`` / ``mc->mt`` for display.  Always populates both
        canonical directions (even at zero) so a silent direction still has
        a column to show — otherwise the UI would look asymmetric."""
        directions = ["meshtastic->meshcore", "meshcore->meshtastic"]
        by_direction = {}
        for d in directions:
            by_direction[d] = {
                "relayed": self._relayed_by_direction.get(d, 0),
                "dropped": self._dropped_by_direction.get(d, 0),
            }
        s = {
            "relayed": self._relay_count,
            "dropped": self._drop_count,
            "rate_limited": self._rate_limited_count,
            "dedup_size": self._dedup.size(),
            "by_direction": by_direction,
        }
        if self._rate_limiter is not None:
            max_events, window = self._rate_limiter.limits()
            s["rate_limit_max"] = max_events
            s["rate_limit_window_s"] = window
            s["rate_limit_current"] = self._rate_limiter.snapshot()
        return s
