"""LORACLE NAVIGATION addon — bearing and distance over the mesh.

Commands:
    !nav <lat>,<lon>       — bearing and distance from your last GPS fix
    !navigate <lat>,<lon>  — alias for !nav

Coordinates only in v1. Deterministic Python template, no LLM call,
no geocoder, no waypoints — by design, so a reply fits in a single
LoRa packet and there's nothing to hallucinate.
"""

import logging
import math
import time
from typing import Dict, Optional, Tuple

from addons.base import Addon

logger = logging.getLogger(__name__)

ADDON_CLASS = None  # Set at bottom of file

# Earth mean radius (km), WGS-84 mean
_EARTH_RADIUS_KM = 6371.0088

# 16-point compass rose
_COMPASS_POINTS = (
    "N", "NNE", "NE", "ENE",
    "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW",
    "W", "WNW", "NW", "NNW",
)


class NavigationAddon(Addon):
    name = "navigation"
    display_name = "Navigation"

    def __init__(self, bridge, config: dict = None):
        super().__init__(bridge, config)
        config = config or {}
        # Max age (seconds) before a node's GPS fix is considered stale.
        # Stale fixes still produce a result, but the reply flags the age.
        self.max_fix_age_s = int(config.get("max_fix_age_s", 600))

    def get_commands(self) -> Dict[str, Tuple[callable, str]]:
        return {
            "!nav": (self._cmd_nav, "<lat>,<lon> - bearing and distance"),
            "!navigate": (self._cmd_nav, "<lat>,<lon> - bearing and distance"),
        }

    # ------------------------------------------------------------------
    # Command handler
    # ------------------------------------------------------------------

    def _cmd_nav(self, node_id: str, arg: str) -> str:
        if not arg or not arg.strip():
            return "Usage: !nav <lat>,<lon>  e.g. !nav 34.0522,-118.2437"

        # 1. Caller's last known position
        pos = self.bridge._node_positions.get(node_id)
        if not pos:
            return (
                "No GPS fix for your node yet. Enable GPS on your "
                "radio and wait for a position broadcast."
            )

        try:
            user_lat = float(pos["lat"])
            user_lon = float(pos["lon"])
        except (KeyError, TypeError, ValueError):
            return "Your stored GPS fix is malformed. Try again later."

        fix_age_s = max(0, int(time.time() - float(pos.get("last_update", 0))))

        # 2. Destination
        dest = self._parse_coords(arg)
        if dest is None:
            return "Bad coords. Use: !nav <lat>,<lon>  e.g. !nav 34.05,-118.24"
        dest_lat, dest_lon = dest

        # 3. Math
        distance_km = self._haversine_km(user_lat, user_lon, dest_lat, dest_lon)
        bearing_deg = self._initial_bearing(user_lat, user_lon, dest_lat, dest_lon)
        compass = self._compass_point(bearing_deg)

        # 4. Format
        return self._format_response(
            user_lat=user_lat,
            user_lon=user_lon,
            dest_lat=dest_lat,
            dest_lon=dest_lon,
            distance_km=distance_km,
            bearing_deg=bearing_deg,
            compass=compass,
            fix_age_s=fix_age_s,
        )

    # ------------------------------------------------------------------
    # Helpers (staticmethods so they're easy to unit-test from a REPL)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_coords(arg: str) -> Optional[Tuple[float, float]]:
        """Parse 'lat,lon' (with or without spaces). Return (lat, lon) or None."""
        if "," not in arg:
            return None
        parts = arg.strip().split(",")
        if len(parts) < 2:
            return None
        try:
            lat = float(parts[0].strip())
            lon = float(parts[1].strip().split()[0])  # tolerate trailing junk
        except ValueError:
            return None
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            return None
        return lat, lon

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Great-circle distance between two points, in kilometers."""
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return _EARTH_RADIUS_KM * c

    @staticmethod
    def _initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Forward azimuth from point 1 to point 2, in degrees [0, 360)."""
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dlam = math.radians(lon2 - lon1)
        x = math.sin(dlam) * math.cos(phi2)
        y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
        theta = math.atan2(x, y)
        return (math.degrees(theta) + 360.0) % 360.0

    @staticmethod
    def _compass_point(deg: float) -> str:
        """Map a bearing (0..360) to one of 16 compass points."""
        idx = int((deg / 22.5) + 0.5) % 16
        return _COMPASS_POINTS[idx]

    @staticmethod
    def _format_age(seconds: int) -> str:
        """Compact age string: 12s, 4m, 2h, 3d."""
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        if seconds < 86400:
            return f"{seconds // 3600}h"
        return f"{seconds // 86400}d"

    def _format_response(
        self,
        user_lat: float,
        user_lon: float,
        dest_lat: float,
        dest_lon: float,
        distance_km: float,
        bearing_deg: float,
        compass: str,
        fix_age_s: int,
    ) -> str:
        distance_mi = distance_km * 0.621371
        age_str = self._format_age(fix_age_s)
        stale_warn = "  STALE" if fix_age_s > self.max_fix_age_s else ""
        return (
            "NAV\n"
            f"Hdg: {int(round(bearing_deg)):03d}° {compass}\n"
            f"Dist: {distance_km:.2f} km / {distance_mi:.2f} mi\n"
            f"From: {user_lat:.4f},{user_lon:.4f}\n"
            f"To:   {dest_lat:.4f},{dest_lon:.4f}\n"
            f"GPS age: {age_str}{stale_warn}"
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self):
        logger.info("Navigation addon started (max fix age: %ds)", self.max_fix_age_s)

    def on_stop(self):
        logger.info("Navigation addon stopped")


ADDON_CLASS = NavigationAddon
