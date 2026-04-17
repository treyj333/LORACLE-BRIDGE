"""Bridge config parsing + policy construction.

Persisted as a JSON blob in SettingsStore under the key ``bridge_config``.
Shape::

    {
        "enabled": true,
        "rules": [
            {"source": "meshtastic", "channel": 0, "mode": "always"},
            {"source": "meshcore",   "channel": 0, "mode": "ai-gated"}
        ]
    }

Fields:
  enabled  — global kill switch; when false, ``build_policy`` returns
             DisabledPolicy regardless of ``rules``.
  rules    — list of per-channel relay rules.
  rule.source  — ``"meshtastic"`` or ``"meshcore"``.
  rule.channel — channel index, or ``null`` to wildcard every channel on that source.
  rule.mode    — ``"off"``, ``"always"``, or ``"ai-gated"``.

Phase 2 behaviour: ``"always"`` and ``"ai-gated"`` are functionally
identical because AIGatedPolicy stubs to pass-through (see policy.py).
Phase 4 will plug the urgency classifier into the ai-gated path; the
config shape won't change.
"""

from typing import Any, Dict, List, Optional, Tuple

from bridge.policy import (
    AIGatedPolicy,
    ChannelAllowlist,
    DisabledPolicy,
    Policy,
)
from bridge.urgency import HeuristicUrgencyClassifier

DEFAULT_CONFIG: Dict[str, Any] = {"enabled": False, "rules": []}

_VALID_MODES = {"off", "always", "ai-gated"}
_VALID_SOURCES = {"meshtastic", "meshcore"}


def _normalize_rules(
    rules: List[Dict[str, Any]],
) -> List[Tuple[str, Optional[int], str]]:
    """Drop malformed rules and coerce channel to int (or None)."""
    normalized: List[Tuple[str, Optional[int], str]] = []
    for r in rules or []:
        source = r.get("source")
        mode = r.get("mode", "off")
        if source not in _VALID_SOURCES or mode not in _VALID_MODES:
            continue
        chan_raw = r.get("channel", 0)
        if chan_raw is None:
            chan: Optional[int] = None
        else:
            try:
                chan = int(chan_raw)
            except (TypeError, ValueError):
                continue
        normalized.append((source, chan, mode))
    return normalized


def build_policy(cfg: Dict[str, Any]) -> Policy:
    """Construct a Policy from the config dict.

    Returns:
        DisabledPolicy if bridge is disabled, rules is empty, or every
        rule is ``mode == "off"``.
        AIGatedPolicy wrapping a ChannelAllowlist when any rule is
        ai-gated (Phase 2 stub = pass-through).
        Plain ChannelAllowlist when all active rules are ``"always"``.
    """
    if not cfg.get("enabled"):
        return DisabledPolicy()

    rules = _normalize_rules(cfg.get("rules", []))
    active = [(src, chan) for (src, chan, mode) in rules if mode != "off"]
    if not active:
        return DisabledPolicy()

    base = ChannelAllowlist(active)
    has_ai_gated = any(mode == "ai-gated" for (_, _, mode) in rules)
    if has_ai_gated:
        # v2 Phase 4 (live): the heuristic urgency classifier replaces the
        # Phase 2 pass-through stub. ai-gated rules now actually gate —
        # only messages the classifier flags urgent cross the bridge.
        # Operators can extend the vocabulary via ``extra_urgent_keywords``
        # in the rule config (per-rule extension is a Phase 4.1 item).
        extra_kw = cfg.get("urgent_keywords") or []
        classifier = HeuristicUrgencyClassifier(
            extra_urgent_keywords=list(extra_kw) if isinstance(extra_kw, list) else None
        )
        return AIGatedPolicy(base, classifier=classifier)
    return base
