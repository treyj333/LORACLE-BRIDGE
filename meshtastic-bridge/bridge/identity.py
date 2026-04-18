"""Sender identity formatting across bridged networks.

When a message from network A relays to network B, recipients on B see
it tagged with the source network: ``from meshcore (Alice): hello``.
This module owns that format — producing it AND recognising it so the
relay doesn't re-relay bridge-originated messages (loop guard).

Source-of-truth for the prefix shape — relay.py and dedup use these
helpers exclusively. If the format ever changes, change it here.
"""

import re

# Matches ``from <protocol> (<name>): `` at the start of a text.
# Designed to read naturally on-radio ("from meshcore (Alice): help!")
# so recipients on the other mesh can tell at a glance which network
# the message originated on.
_BRIDGE_PREFIX_RE = re.compile(r"^from (meshtastic|meshcore) \([^)]+\):\s")

# Protocol names written verbatim into the prefix. Short codes (mt/mc)
# kept as aliases so lookup stays flexible.
_PROTOCOL_LONG = {
    "meshtastic": "meshtastic", "mt": "meshtastic",
    "meshcore": "meshcore",     "mc": "meshcore",
}


def format_bridged(source_protocol: str, sender_display: str, text: str) -> str:
    """Wrap a relayed message with a network/sender tag.

    Args:
        source_protocol: DB-form name — ``"meshtastic"`` or ``"meshcore"``.
        sender_display: short human-friendly id (last-6 native chars,
            short_name, or custom nickname — chooser's call).
        text: the original message body (unmodified).

    Returns:
        ``"from meshtastic (Alice): original text"``.

    Unknown protocols fall back to the raw name, so a future protocol
    plugs in cleanly.
    """
    proto = _PROTOCOL_LONG.get(source_protocol, source_protocol)
    return f"from {proto} ({sender_display}): {text}"


def looks_bridged(text: str) -> bool:
    """Return True if ``text`` already carries a bridge prefix.

    Used as a loop guard: the relay skips messages that match, since they
    came from the other side of the bridge and relaying them again would
    ping-pong forever.
    """
    return bool(_BRIDGE_PREFIX_RE.match(text or ""))
