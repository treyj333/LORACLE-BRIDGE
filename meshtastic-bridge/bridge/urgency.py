"""Urgency classifier for AI-gated relay.

Purpose-built heuristic for deciding whether a message should cross the
bridge when a channel is configured in ``ai-gated`` mode. Runs as a
keyword + structure heuristic (no LLM call in the hot path) so normal
relays don't pay model latency.

The vocabulary leans toward defense-tech / emergency mesh traffic:
medical, evacuation, threats, distress. Extend via the optional
``extra_urgent_keywords`` argument — those are word-boundary matched
alongside the built-ins.

LLM-backed urgency scoring is out of scope for v2 Phase 4 (deferred to
v2.1). When we add it, it'll sit alongside this class so callers can
choose heuristic (fast) vs LLM (smart).
"""

import re
from typing import List, Optional

# Word-boundary-matched patterns. Keep these broad; false positives on
# the fail-open side are preferable to dropping genuine urgent traffic.
_URGENT_PATTERNS = [
    # Distress / help
    r"\bemergency\b", r"\bhelp\b", r"\burgent\b", r"\bmayday\b",
    r"\bsos\b", r"\bdistress\b", r"\bpanic\b",
    r"\bbreaking\b", r"\bimmediate\b", r"\basap\b",
    r"\balert\b", r"\bwarning\b", r"\bcritical\b",
    # Medical / casualty
    r"\bcasualty\b", r"\bcasualties\b", r"\binjured\b", r"\bwounded\b",
    r"\bmedic\b", r"\bmedevac\b", r"\bevac\b", r"\bevacuation\b",
    r"\bbleeding\b", r"\bunconscious\b", r"\bunresponsive\b",
    # Fire / disaster
    r"\bfire\b", r"\bsmoke\b", r"\bsmoking\b",
    r"\bflood(s|ing|ed)?\b",
    r"\bearthquake\b", r"\btsunami\b", r"\btornado\b",
    # Threat / incident
    r"\bshot(s)?\b", r"\bshooting\b",
    r"\battack(s|ed|ing|er|ers)?\b",
    r"\bhostile\b", r"\bthreat(s|ened|ening)?\b",
    # Stuck / lost
    r"\bstranded\b", r"\btrapped\b", r"\blost\b", r"\bmissing\b",
    r"\bdown\b", r"\bcrashed\b", r"\bcrash\b",
]
_URGENT_RE = re.compile("|".join(_URGENT_PATTERNS), re.IGNORECASE)

# Common acks / greetings that should NEVER be relayed as urgent even
# if they somehow accumulate uppercase / exclamations.
_CHATTER_EXACT = {
    "hi", "hello", "hey", "yo", "sup", "gm", "gn", "lol", "haha",
    "ok", "okay", "k", "kk", "thanks", "thank you", "ty", "thx",
    "roger", "copy", "wilco", "over", "out", "ack", "nack",
}


class HeuristicUrgencyClassifier:
    """Fast keyword-based urgency classifier.

    Not a language model — trades some nuance for sub-millisecond
    decisions. Fail-open by design: when in doubt, relay. The Relay's
    dedup cache prevents noisy near-duplicates from saturating the
    other network even if classification is over-permissive.
    """

    def __init__(self, extra_urgent_keywords: Optional[List[str]] = None):
        if extra_urgent_keywords:
            extra = [rf"\b{re.escape(k.lower())}\b" for k in extra_urgent_keywords]
            self._extra_re: Optional[re.Pattern] = re.compile(
                "|".join(extra), re.IGNORECASE
            )
        else:
            self._extra_re = None

    def is_urgent(self, text: str) -> bool:
        if not text:
            return False
        t = text.strip()

        # Trivial acks / greetings → never urgent. Strip trailing
        # punctuation before the lookup so "hi!" matches "hi".
        cleaned = re.sub(r"[.,!?]+$", "", t.lower())
        if cleaned in _CHATTER_EXACT:
            return False

        # Built-in urgent vocabulary
        if _URGENT_RE.search(t):
            return True
        # Caller-supplied extra keywords
        if self._extra_re is not None and self._extra_re.search(t):
            return True

        # Weak structural signal: multiple exclamations with
        # meaningful uppercase (not just an abbreviation).
        if (
            t.count("!") >= 2
            and sum(1 for c in t if c.isupper()) >= 3
        ):
            return True

        return False
