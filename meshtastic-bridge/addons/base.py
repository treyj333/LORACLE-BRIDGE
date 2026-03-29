"""Base class for LORACLE BRIDGE addons."""

from typing import Callable, Dict, List, Optional, Tuple


class Addon:
    """Base addon interface. Subclass this to create a bridge addon.

    Addons can register:
    - Commands (!cmd) that mesh nodes can invoke
    - Dashboard tabs with HTML/JS/API routes
    - Message observers that see all incoming traffic
    """

    name: str = ""           # Machine name, e.g. "dead_drop"
    display_name: str = ""   # Human name, e.g. "Dead Drop"

    def __init__(self, bridge, config: dict = None):
        """Initialize with a reference to the bridge and optional config.

        Args:
            bridge: The StandaloneBridge instance.
            config: Addon-specific configuration dict.
        """
        self.bridge = bridge
        self.config = config or {}

    def get_commands(self) -> Dict[str, Tuple[Callable, str]]:
        """Return commands this addon provides.

        Returns:
            Dict mapping command string (e.g. "!drop") to
            (handler_callable, help_text) tuple.

            Handler signature: (node_id: str, arg: str) -> Optional[str]
        """
        return {}

    def get_dashboard_tab(self) -> Optional[dict]:
        """Return dashboard tab configuration.

        Returns:
            Dict with keys:
                id: str      — Tab ID (e.g. "dead_drop")
                label: str   — Tab button label (e.g. "Dead Drop")
                html: str    — HTML content for the tab section
                js: str      — JavaScript for the tab (polling, actions, etc.)
            Or None if this addon has no dashboard tab.
        """
        return None

    def get_api_routes(self) -> List[Tuple[str, str, Callable]]:
        """Return Flask API routes this addon provides.

        Returns:
            List of (method, path, handler) tuples.
            Example: [("GET", "/api/dead_drop/pending", self._api_pending)]
        """
        return []

    def on_message(self, node_id: str, text: str, packet: dict):
        """Called for every incoming text message (after dedup/rate-limit).

        Use this to observe traffic without intercepting it.
        The bridge will still process the message normally.

        Args:
            node_id: Sender's mesh node ID.
            text: Message text content.
            packet: Full Meshtastic packet dict.
        """
        pass

    def on_start(self):
        """Called after the bridge is fully initialized and running."""
        pass

    def on_stop(self):
        """Called during bridge shutdown."""
        pass
