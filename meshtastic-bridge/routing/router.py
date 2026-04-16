"""Query router — resolves a query + optional override to a concrete model."""

import logging
import re
from typing import Dict, Optional, Tuple

from routing.classifier import classify
from routing.tiers import (
    Tier, TierConfig,
    ModelDisabledError, ModelNotInstalledError,
)

logger = logging.getLogger("routing.router")

# Override prefixes: !tiny, !std, !big
_PREFIX_RE = re.compile(r"^!(tiny|std|big)\s+", re.IGNORECASE)
_PREFIX_ONLY_RE = re.compile(r"^!(tiny|std|big)\s*$", re.IGNORECASE)

_PREFIX_MAP = {
    "tiny": Tier.TINY,
    "std": Tier.STANDARD,
    "big": Tier.BIG,
}

USAGE_HINT = (
    "Usage: !big <your question>. "
    "Tiers: !tiny (fast), !std (balanced), !big (deep). [End]"
)


def parse_prefix(text: str) -> Tuple[Optional[Tier], str]:
    """Extract an override prefix from the message text.

    Returns (override_tier_or_None, cleaned_query).
    """
    # Check for prefix-only (no query)
    m = _PREFIX_ONLY_RE.match(text.strip())
    if m:
        return _PREFIX_MAP[m.group(1).lower()], ""

    # Check for prefix + query
    m = _PREFIX_RE.match(text)
    if m:
        tier = _PREFIX_MAP[m.group(1).lower()]
        query = text[m.end():]
        return tier, query

    return None, text


def route(
    query: str,
    tiers: Dict[Tier, TierConfig],
    installed_models: list,
    override: Optional[Tier] = None,
    auto_routing: bool = True,
) -> Tuple[Tier, str]:
    """Resolve a query to a (tier, model_name) pair.

    Args:
        query: The user's message text (prefix already stripped).
        tiers: Current tier configurations.
        installed_models: List of model names installed in Ollama.
        override: Explicit tier override (from !prefix).
        auto_routing: Whether the classifier runs. If False, uses STANDARD.

    Returns:
        (chosen_tier, model_name)

    Raises:
        ModelDisabledError: If the resolved tier is disabled.
        ModelNotInstalledError: If the resolved model isn't installed.
    """
    # Determine tier
    if override is not None:
        chosen = override
    elif auto_routing:
        chosen = classify(query)
    else:
        chosen = Tier.STANDARD

    # Validate tier
    config = tiers.get(chosen)
    if config is None or not config.enabled:
        tier_name = chosen.value.upper()
        raise ModelDisabledError(
            f"{tier_name} tier is disabled. Enable it in CONFIG > "
            f"MODEL ROUTING, or use !std or !tiny. [End]"
        )

    # Validate model is installed
    model = config.model
    if installed_models and model not in installed_models:
        # Check partial match (e.g. "phi4:14b" matches "phi4:14b")
        if not any(m == model or m.startswith(model.split(":")[0] + ":") for m in installed_models):
            raise ModelNotInstalledError(
                f"Model '{model}' not installed. Run "
                f"`ollama pull {model}` or choose a different model. [End]"
            )

    logger.info(f"Routed to {chosen.value.upper()} → {model}")
    return chosen, model
