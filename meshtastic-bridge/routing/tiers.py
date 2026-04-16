"""Model tier definitions and RAM-based defaults for LORACLE Bridge."""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional


class Tier(Enum):
    TINY = "tiny"
    STANDARD = "std"
    BIG = "big"


@dataclass
class TierConfig:
    tier: Tier
    model: str
    enabled: bool


# Settings keys
SETTINGS_KEYS = {
    Tier.TINY: ("tier.tiny.model", "tier.tiny.enabled"),
    Tier.STANDARD: ("tier.standard.model", "tier.standard.enabled"),
    Tier.BIG: ("tier.big.model", "tier.big.enabled"),
}

ROUTING_AUTO_KEY = "routing.auto_enabled"
ROUTING_SHOW_TAG_KEY = "routing.show_tier_tag"


def default_tiers_for_ram(ram_gb: float) -> Dict[Tier, TierConfig]:
    """Return default tier configs based on available system RAM."""
    if ram_gb >= 16:
        return {
            Tier.TINY: TierConfig(Tier.TINY, "gemma3:4b", True),
            Tier.STANDARD: TierConfig(Tier.STANDARD, "qwen3:8b", True),
            Tier.BIG: TierConfig(Tier.BIG, "phi4:14b", True),
        }
    elif ram_gb >= 8:
        return {
            Tier.TINY: TierConfig(Tier.TINY, "gemma3:4b", True),
            Tier.STANDARD: TierConfig(Tier.STANDARD, "qwen3:8b", True),
            Tier.BIG: TierConfig(Tier.BIG, "qwen3:8b", False),
        }
    else:
        return {
            Tier.TINY: TierConfig(Tier.TINY, "gemma3:4b", True),
            Tier.STANDARD: TierConfig(Tier.STANDARD, "gemma3:4b", True),
            Tier.BIG: TierConfig(Tier.BIG, "gemma3:4b", False),
        }


def load_tiers(settings_store) -> Dict[Tier, TierConfig]:
    """Load tier configs from settings, falling back to RAM-based defaults."""
    from ollama_client import get_system_ram_gb
    defaults = default_tiers_for_ram(get_system_ram_gb())
    result = {}
    for tier in Tier:
        model_key, enabled_key = SETTINGS_KEYS[tier]
        default = defaults[tier]
        model = settings_store.get(model_key, default.model)
        enabled = settings_store.get(enabled_key, default.enabled)
        result[tier] = TierConfig(tier, model, bool(enabled))
    return result


def save_tiers(settings_store, tiers: Dict[Tier, TierConfig]) -> None:
    """Persist tier configs to settings."""
    for tier, config in tiers.items():
        model_key, enabled_key = SETTINGS_KEYS[tier]
        settings_store.set(model_key, config.model)
        settings_store.set(enabled_key, config.enabled)


class ModelNotConfiguredError(Exception):
    """Raised when a tier's model is missing or disabled."""


class ModelDisabledError(ModelNotConfiguredError):
    pass


class ModelNotInstalledError(ModelNotConfiguredError):
    pass
