"""LORACLE COVERAGE LOGGER — record (time, node, lat, lon, rssi, snr) samples
to a JSONL file for offline mesh coverage / dead-zone analysis.

Append-only by design — no schema, no migrations, easy to nuke and easy to
scp off the bridge. Each line is one sample dict.

Throttling: skip records that are within MIN_TIME_DELTA_S seconds *and*
MIN_DIST_M meters of the most recent record for the same node, so a node
sitting still doesn't flood the file.
"""

import json
import logging
import math
import os
import threading
import time
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("coverage")

# Throttle: don't record a new sample for the same node unless EITHER
# this much time has passed OR they've moved this far.
MIN_TIME_DELTA_S = 5.0
MIN_DIST_M = 10.0


class CoverageLogger:
    """Append-only coverage sample logger backed by a JSONL file."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        # In-memory last-sample cache for throttling: node_id -> (ts, lat, lon)
        self._last: Dict[str, Tuple[float, float, float]] = {}
        # Make sure the parent dir exists.
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception as e:
            logger.warning(f"Could not create coverage log dir: {e}")

    def record(
        self,
        node_id: str,
        lat: float,
        lon: float,
        rssi: Optional[int] = None,
        snr: Optional[float] = None,
        ts: Optional[float] = None,
    ) -> bool:
        """Record one coverage sample. Returns True if written, False if throttled."""
        if not node_id or lat is None or lon is None:
            return False
        if ts is None:
            ts = time.time()

        with self._lock:
            prev = self._last.get(node_id)
            if prev is not None:
                prev_ts, prev_lat, prev_lon = prev
                dt = ts - prev_ts
                dm = _haversine_m(prev_lat, prev_lon, lat, lon)
                if dt < MIN_TIME_DELTA_S and dm < MIN_DIST_M:
                    return False  # throttled — too close in time AND space

            sample = {
                "ts": round(ts, 2),
                "node": node_id,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
            }
            if rssi is not None:
                sample["rssi"] = int(rssi)
            if snr is not None:
                sample["snr"] = round(float(snr), 2)

            try:
                with open(self.path, "a") as f:
                    f.write(json.dumps(sample) + "\n")
                self._last[node_id] = (ts, lat, lon)
                return True
            except Exception as e:
                logger.warning(f"Could not append coverage sample: {e}")
                return False

    def clear(self) -> int:
        """Truncate the coverage log file. Returns the number of samples removed."""
        with self._lock:
            removed = 0
            try:
                if os.path.exists(self.path):
                    # Count first so we can report it back
                    try:
                        with open(self.path, "r") as f:
                            removed = sum(1 for line in f if line.strip())
                    except Exception:
                        removed = 0
                    # Truncate
                    open(self.path, "w").close()
                # Also clear the in-memory throttle cache so next sample is logged
                self._last.clear()
                logger.info(f"Coverage log cleared ({removed} samples removed)")
                return removed
            except Exception as e:
                logger.warning(f"Could not clear coverage log: {e}")
                return 0

    def read_all(self, limit: Optional[int] = None) -> List[dict]:
        """Read all samples from the JSONL file. Optionally cap to last N."""
        if not os.path.exists(self.path):
            return []
        out: List[dict] = []
        try:
            with open(self.path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning(f"Could not read coverage log: {e}")
            return []
        if limit is not None and len(out) > limit:
            return out[-limit:]
        return out

    def stats(self) -> dict:
        """Return basic stats about the coverage log: count, time range, bbox, nodes."""
        samples = self.read_all()
        if not samples:
            return {
                "count": 0,
                "nodes": 0,
                "time_start": None,
                "time_end": None,
                "bbox": None,
                "path": self.path,
            }
        nodes = set()
        ts_min = float("inf")
        ts_max = 0.0
        lat_min = lat_max = samples[0].get("lat", 0.0)
        lon_min = lon_max = samples[0].get("lon", 0.0)
        for s in samples:
            nodes.add(s.get("node", ""))
            t = s.get("ts", 0.0)
            if t < ts_min:
                ts_min = t
            if t > ts_max:
                ts_max = t
            la = s.get("lat", lat_min)
            lo = s.get("lon", lon_min)
            if la < lat_min: lat_min = la
            if la > lat_max: lat_max = la
            if lo < lon_min: lon_min = lo
            if lo > lon_max: lon_max = lo
        return {
            "count": len(samples),
            "nodes": len(nodes),
            "time_start": ts_min,
            "time_end": ts_max,
            "bbox": [lat_min, lon_min, lat_max, lon_max],
            "path": self.path,
        }


# ---------------------------------------------------------------------------
# Geometry helper (kept module-private; doesn't justify importing the addon)
# ---------------------------------------------------------------------------

_EARTH_RADIUS_M = 6_371_008.8


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS_M * c
