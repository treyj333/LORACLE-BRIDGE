"""LORACLE BRIDGE addon system — pluggable extensions for the mesh bridge.

Addons register commands, dashboard tabs, API routes, and message observers
without modifying the core bridge code.
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Registry of available addon classes (name -> module path)
ADDON_REGISTRY = {
    "dead_drop": "addons.dead_drop.addon",
    "brief": "addons.brief.addon",
    "triage": "addons.triage.addon",
}


def load_addons(bridge, enabled: Optional[Dict[str, dict]] = None) -> list:
    """Load and initialize enabled addons.

    Args:
        bridge: The StandaloneBridge instance.
        enabled: Dict of addon_name -> config dict. If None, tries all addons.

    Returns:
        List of initialized Addon instances.
    """
    if enabled is None:
        enabled = {name: {} for name in ADDON_REGISTRY}

    loaded = []
    for name, config in enabled.items():
        if name not in ADDON_REGISTRY:
            logger.warning(f"Unknown addon: {name}")
            continue

        module_path = ADDON_REGISTRY[name]
        try:
            import importlib
            mod = importlib.import_module(module_path)
            addon_cls = getattr(mod, "ADDON_CLASS")
            addon = addon_cls(bridge, config)
            loaded.append(addon)
            logger.info(f"Addon loaded: {addon.display_name}")
        except ImportError as e:
            logger.warning(f"Addon '{name}' not available (missing deps): {e}")
        except Exception as e:
            logger.warning(f"Addon '{name}' failed to load: {e}")

    return loaded
