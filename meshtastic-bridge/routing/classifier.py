"""Hybrid length + keyword classifier for model tier routing.

Pure function — no LLM calls, no disk I/O, no state.
Target runtime: <1ms on commodity hardware.
"""

import re
from routing.tiers import Tier

CLASSIFIER_VERSION = "1.0"

# ── BIG keywords (word-boundary matched, case-insensitive) ───────────────────

_BIG_PATTERNS = [
    r"\bexplain\b", r"\bhow does\b", r"\bwhy does\b", r"\bcompare\b",
    r"\bdifference\b", r"\bwalk me through\b", r"\bstep by step\b",
    r"\banalyze\b", r"\banalysis\b", r"\bbreakdown\b",
    r"\bpros and cons\b", r"\bwhat are the\b", r"\bhow would i\b",
    r"\btriage\b", r"\bdiagnose\b", r"\btreatment\b", r"\bemergency\b",
    r"\bdebug\b", r"\btroubleshoot\b", r"\barchitecture\b", r"\bdesign\b",
    r"\bin detail\b", r"\bthoroughly\b", r"\bcomprehensive\b",
]
_BIG_RE = re.compile("|".join(_BIG_PATTERNS), re.IGNORECASE)

# ── TINY start patterns ──────────────────────────────────────────────────────

_TINY_STARTS = [
    r"^what is\b", r"^what time\b", r"^what day\b",
    r"^when is\b", r"^when does\b",
    r"^who is\b", r"^who was\b",
    r"^where is\b",
]
_TINY_START_RE = re.compile("|".join(_TINY_STARTS), re.IGNORECASE)

_TINY_EXACT = {"yes", "no", "ok", "okay", "thanks", "thank you", "ty", "thx",
               "hi", "hello", "hey", "yo", "sup", "gm", "gn", "lol", "haha"}


def classify(query: str) -> Tier:
    """Classify a user query into a Tier based on length + keywords.

    Rules (first match wins):
    1. Trivial length (<=20 chars) → TINY
    2. BIG keywords → BIG
    3. TINY start patterns or exact matches → TINY
    4. Length thresholds: <=160 → STANDARD, >160 → BIG
    5. Multiple question marks → BIG
    """
    q = query.strip()
    q_lower = q.lower()

    # 1. Trivial length
    if len(q) <= 20:
        # But check for BIG keywords even in short queries
        if _BIG_RE.search(q):
            return Tier.BIG
        return Tier.TINY

    # 2. BIG keywords
    if _BIG_RE.search(q):
        return Tier.BIG

    # 3. TINY patterns
    if _TINY_START_RE.search(q_lower):
        return Tier.TINY
    # Exact match (after stripping punctuation)
    cleaned = re.sub(r"[?!.,]+$", "", q_lower).strip()
    if cleaned in _TINY_EXACT:
        return Tier.TINY

    # 4. Length thresholds
    if len(q) > 160:
        return Tier.BIG

    # 5. Multiple question marks = compound question
    if q.count("?") >= 2:
        return Tier.BIG

    # 6. Multiple "and" conjunctions (3+)
    if q_lower.count(" and ") >= 3:
        return Tier.BIG

    return Tier.STANDARD
