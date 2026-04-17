"""Sender identity formatting across bridged networks.

When a message from network A relays to network B, recipients on B see
it tagged with the source network: ``[mt-Alice] hello``. This module
owns that format — producing it AND recognising it so the relay doesn't
re-relay bridge-originated messages (loop guard).

Source-of-truth for the prefix shape — relay.py and dedup use these
helpers exclusively. If the format ever changes, change it here.
"""

import re

# Matches ``[<proto>-<name>] `` at the start of a text.
# proto must be a registered short code (mt|mc). name is any 1+ chars
# not containing ']'. Trailing space is required so "[mt-foo]" inside a
# longer sentence isn't misread as a bridge prefix.
_BRIDGE_PREFIX_RE = re.compile(r"^\[(mt|mc)-[^\]]+\]\s")

# Map DB-form protocol names to the short codes used in the prefix.
_PROTOCOL_SHORT = {"meshtastic": "mt", "meshcore": "mc"}


def format_bridged(source_protocol: str, sender_display: str, text: str) -> str:
    """Wrap a relayed message with a network/sender tag.

    Args:
        source_protocol: DB-form name — ``"meshtastic"`` or ``"meshcore"``.
        sender_display: short human-friendly id (last-6 native chars,
            short_name, or custom nickname — chooser's call).
        text: the original message body (unmodified).

    Returns:
        ``"[mt-Alice] original text"``.

    Unknown protocols fall back to their first-two-chars short form, so
    a future protocol plugs in cleanly.
    """
    short = _PROTOCOL_SHORT.get(source_protocol, source_protocol[:2])
    return f"[{short}-{sender_display}] {text}"


def looks_bridged(text: str) -> bool:
    """Return True if ``text`` already carries a bridge prefix.

    Used as a loop guard: the relay skips messages that match, since they
    came from the other side of the bridge and relaying them again would
    ping-pong forever.
    """
    return bool(_BRIDGE_PREFIX_RE.match(text or ""))
