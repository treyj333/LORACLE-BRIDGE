"""Bridge relay policy — decides which messages cross between networks.

A Policy sees every candidate relay (an incoming message + a candidate
destination protocol) and answers: should this actually relay? Yes/no.

Phase 2 implementations:
  - DisabledPolicy   — nothing crosses (bridge off)
  - AlwaysRelay      — every channel broadcast crosses (Akita-parity)
  - ChannelAllowlist — crosses iff (source_protocol, channel) is listed
  - AIGatedPolicy    — wraps another policy; in Phase 4 will consult the
                       LLM urgency classifier. Phase 2 is a pass-through
                       stub so the plumbing is in place.

DMs never cross by default — bridging private conversations is a trust
decision that belongs in Phase 3's UI, not in policy defaults.
"""

from abc import ABC, abstractmethod
from typing import Iterable, Optional, Set, Tuple


class Policy(ABC):
    """Abstract relay decision maker.

    v2.5: ``should_relay`` returns a ``(allowed, reason)`` tuple so the
    Relay Health panel can surface exactly why a message was dropped.
    ``reason`` is an empty string on allow, populated on deny.
    """

    @abstractmethod
    def should_relay(
        self,
        source_protocol: str,
        dest_protocol: str,
        sender: str,
        text: str,
        channel: int,
        is_dm: bool,
    ) -> Tuple[bool, str]:
        ...


class DisabledPolicy(Policy):
    """Bridge is off. Nothing crosses."""

    def should_relay(self, *args, **kwargs) -> Tuple[bool, str]:
        return (False, "policy:disabled")


class AlwaysRelay(Policy):
    """Relay every channel broadcast. Never relays DMs."""

    def should_relay(
        self,
        source_protocol: str,
        dest_protocol: str,
        sender: str,
        text: str,
        channel: int,
        is_dm: bool,
    ) -> Tuple[bool, str]:
        if is_dm:
            return (False, "policy:dm_blocked")
        return (True, "")


class ChannelAllowlist(Policy):
    """Relay only channel broadcasts listed in ``allowlist``.

    ``allowlist`` is an iterable of ``(source_protocol, channel)`` tuples.
    Passing ``channel=None`` wildcards to every channel from that protocol
    (useful for "relay everything this radio says").
    """

    def __init__(self, allowlist: Iterable[Tuple[str, Optional[int]]]):
        self._explicit: Set[Tuple[str, int]] = set()
        self._protocol_wild: Set[str] = set()
        for proto, chan in allowlist:
            if chan is None:
                self._protocol_wild.add(proto)
            else:
                self._explicit.add((proto, int(chan)))

    def should_relay(
        self,
        source_protocol: str,
        dest_protocol: str,
        sender: str,
        text: str,
        channel: int,
        is_dm: bool,
    ) -> Tuple[bool, str]:
        if is_dm:
            return (False, "policy:dm_blocked")
        if source_protocol in self._protocol_wild:
            return (True, "")
        if (source_protocol, channel) in self._explicit:
            return (True, "")
        return (
            False,
            f"policy:no_rule_matched source={source_protocol} channel={channel}",
        )


class AIGatedPolicy(Policy):
    """Wraps a base policy and applies LLM-based gating on top.

    Phase 2 behaviour (stub): delegates straight to the base. Phase 4
    plugs in the real urgency classifier — when ``classifier`` is
    provided it's consulted after the base policy votes yes.
    """

    def __init__(self, base_policy: Policy, classifier=None):
        self._base = base_policy
        self._classifier = classifier

    def should_relay(
        self,
        source_protocol: str,
        dest_protocol: str,
        sender: str,
        text: str,
        channel: int,
        is_dm: bool,
    ) -> Tuple[bool, str]:
        allowed, reason = self._base.should_relay(
            source_protocol, dest_protocol, sender, text, channel, is_dm
        )
        if not allowed:
            return (False, reason)
        if self._classifier is None:
            return (True, "")  # Phase 2 stub
        # Phase 4: classifier returns True iff the text is urgent enough
        # to cross the bridge.
        try:
            if self._classifier.is_urgent(text):
                return (True, "")
            return (False, "policy:ai_not_urgent")
        except Exception:
            # Fail-open — if the classifier errors, default to relaying
            # rather than silently dropping traffic. This is a deliberate
            # choice; the alternative (fail-closed) risks losing urgent
            # messages when the LLM service is having a bad day.
            return (True, "")
