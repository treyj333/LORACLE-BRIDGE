"""LORACLE Bridge v2 — cross-protocol relay core.

Phase 2 of the v2 roadmap (see LORACLE_BRIDGE_V2_FSD.md at the repo root).
This package owns the *relay* layer: watch every message arriving on one
network, decide via a Policy whether it should cross to the other, apply
loop/dedup guards, and re-inject with a sender-network tag.

The actual transmit back out still goes through StandaloneBridge — this
package doesn't know how to drive a radio. It receives messages and asks
a caller-supplied ``send_fn(dest_protocol, text, channel)`` to deliver
them. That keeps protocol-specific TX code (interface.sendText,
RadioManager.send, etc.) where it already lives.
"""

from bridge.config import DEFAULT_CONFIG, build_policy
from bridge.dedup import RelayDedupCache
from bridge.identity import format_bridged, looks_bridged
from bridge.policy import (
    AIGatedPolicy,
    AlwaysRelay,
    ChannelAllowlist,
    DisabledPolicy,
    Policy,
)
from bridge.relay import Relay
from bridge.urgency import HeuristicUrgencyClassifier

__all__ = [
    "DEFAULT_CONFIG",
    "build_policy",
    "RelayDedupCache",
    "format_bridged",
    "looks_bridged",
    "AIGatedPolicy",
    "AlwaysRelay",
    "ChannelAllowlist",
    "DisabledPolicy",
    "Policy",
    "Relay",
    "HeuristicUrgencyClassifier",
]
