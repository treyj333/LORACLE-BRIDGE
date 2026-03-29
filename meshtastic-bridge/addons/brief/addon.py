"""LORACLE BRIEF addon — AI-generated situation reports from mesh traffic.

Commands:
    !brief          — Get the latest SITREP
    !brief now      — Generate a fresh SITREP immediately
    !brief history  — List available SITREPs by timestamp
"""

import logging
import threading
import time
import uuid
from typing import Dict, Optional, Tuple

from addons.base import Addon
from addons.brief.aggregator import TrafficAggregator
from addons.brief.generator import generate_sitrep, make_sitrep_id

logger = logging.getLogger(__name__)

ADDON_CLASS = None  # Set at bottom of file


class BriefAddon(Addon):
    name = "brief"
    display_name = "Brief"

    def __init__(self, bridge, config: dict = None):
        super().__init__(bridge, config)
        config = config or {}

        self.interval_minutes = config.get("interval_minutes", 60)
        self.aggregator = TrafficAggregator()
        self._scheduler_thread = None
        self._last_sitrep_time = time.time()

    def get_commands(self) -> Dict[str, Tuple[callable, str]]:
        return {
            "!brief": (self._cmd_brief, "get latest SITREP or generate new one"),
        }

    def get_dashboard_tab(self) -> Optional[dict]:
        from addons.brief.dashboard import get_tab_config
        return get_tab_config()

    def get_api_routes(self):
        from addons.brief.dashboard import get_api_routes
        return get_api_routes(self)

    def on_message(self, node_id: str, text: str, packet: dict):
        """Observe all incoming traffic and categorize events."""
        # Classify the event
        if text.startswith("!"):
            event_type = "command"
            summary = f"Command: {text.split()[0]}"
        elif any(kw in text.lower() for kw in ["alert", "emergency", "urgent", "help"]):
            event_type = "alert"
            summary = text[:100]
        else:
            event_type = "message"
            summary = text[:100]

        self.aggregator.record_event(node_id, event_type, summary, text)

    def _cmd_brief(self, node_id: str, arg: str) -> str:
        """Handle !brief commands."""
        if not arg:
            # Get latest SITREP
            latest = self.aggregator.get_latest_sitrep()
            if not latest:
                return "No SITREPs generated yet. Try !brief now"
            age_mins = int((time.time() - latest["generated_at"]) / 60)
            return f"[{age_mins}m ago] {latest['content']}"

        if arg.lower() == "now":
            return self._generate_sitrep()

        if arg.lower() == "history":
            history = self.aggregator.get_sitrep_history(limit=5)
            if not history:
                return "No SITREP history."
            lines = []
            for s in history:
                ts = time.strftime("%m/%d %H%M", time.localtime(s["generated_at"]))
                lines.append(f"{ts} — {s['node_count']} nodes, {s['message_count']} msgs")
            return "SITREPs:\n" + "\n".join(lines)

        return "Usage: !brief, !brief now, !brief history"

    def _generate_sitrep(self) -> str:
        """Generate a fresh SITREP from recent traffic."""
        now = time.time()
        period_start = self._last_sitrep_time
        period_end = now

        events = self.aggregator.get_events_since(period_start)
        summary = self.aggregator.get_event_summary(period_start)

        sitrep_id = make_sitrep_id()
        content = generate_sitrep(
            self.bridge.ollama,
            events,
            summary,
            period_start,
            period_end,
        )

        # Store the SITREP
        self.aggregator.store_sitrep(
            sitrep_id, content, period_start, period_end,
            summary.get("unique_nodes", 0),
            summary.get("total_events", 0),
        )

        self._last_sitrep_time = now
        logger.info(f"Generated SITREP {sitrep_id}: {len(content)} chars")
        return content

    def _scheduler_loop(self):
        """Background thread that generates SITREPs on schedule."""
        while self.bridge._running:
            time.sleep(60)  # Check every minute
            elapsed = time.time() - self._last_sitrep_time
            if elapsed >= self.interval_minutes * 60:
                try:
                    self._generate_sitrep()
                except Exception as e:
                    logger.warning(f"Scheduled SITREP generation failed: {e}")

                # Clean up old events
                self.aggregator.cleanup_old_events()

    def on_start(self):
        """Start the SITREP scheduler."""
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop, daemon=True
        )
        self._scheduler_thread.start()
        logger.info(
            f"Brief addon started (SITREP interval: {self.interval_minutes}m)"
        )

    def on_stop(self):
        logger.info("Brief addon stopped")


ADDON_CLASS = BriefAddon
