#!/usr/bin/env python3
"""Download OpenStreetMap tiles for offline use.

Downloads tiles for a geographic bounding box at specified zoom levels.
Tiles are saved to ~/.mesh-llm/tiles/{z}/{x}/{y}.png

Usage:
    python download_tiles.py                    # CONUS zoom 0-12 (~1.5GB)
    python download_tiles.py --max-zoom 10      # CONUS zoom 0-10 (~100MB)
    python download_tiles.py --bbox 33,-118,34,-117 --max-zoom 14  # LA area
"""

import argparse
import math
import os
import random
import sys
import time
import urllib.request

TILE_DIR = os.path.expanduser("~/.mesh-llm/tiles")
SERVERS = ["a", "b", "c"]
USER_AGENT = "LORACLE-Bridge/1.0 (offline tile cache)"


def deg2num(lat_deg, lon_deg, zoom):
    """Convert lat/lon to tile numbers."""
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return xtile, ytile


def count_tiles(min_lat, min_lon, max_lat, max_lon, min_zoom, max_zoom):
    """Count total tiles in the bounding box."""
    total = 0
    for z in range(min_zoom, max_zoom + 1):
        x_min, y_max = deg2num(min_lat, min_lon, z)
        x_max, y_min = deg2num(max_lat, max_lon, z)
        total += (x_max - x_min + 1) * (y_max - y_min + 1)
    return total


def download_tiles(min_lat, min_lon, max_lat, max_lon, min_zoom=0, max_zoom=12):
    """Download tiles for the given bounding box."""
    total = count_tiles(min_lat, min_lon, max_lat, max_lon, min_zoom, max_zoom)
    print(f"Downloading {total:,} tiles (zoom {min_zoom}-{max_zoom})...")
    print(f"Bounding box: ({min_lat}, {min_lon}) to ({max_lat}, {max_lon})")
    print(f"Tile directory: {TILE_DIR}")
    print()

    downloaded = 0
    skipped = 0
    failed = 0

    for z in range(min_zoom, max_zoom + 1):
        x_min, y_max = deg2num(min_lat, min_lon, z)
        x_max, y_min = deg2num(max_lat, max_lon, z)
        level_total = (x_max - x_min + 1) * (y_max - y_min + 1)
        print(f"Zoom {z:2d}: {level_total:,} tiles...", end=" ", flush=True)
        level_dl = 0

        for x in range(x_min, x_max + 1):
            for y in range(y_min, y_max + 1):
                tile_path = os.path.join(TILE_DIR, str(z), str(x), f"{y}.png")
                if os.path.exists(tile_path):
                    skipped += 1
                    continue

                os.makedirs(os.path.dirname(tile_path), exist_ok=True)
                server = random.choice(SERVERS)
                url = f"https://{server}.tile.openstreetmap.org/{z}/{x}/{y}.png"

                for attempt in range(3):
                    try:
                        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            with open(tile_path, "wb") as f:
                                f.write(resp.read())
                        downloaded += 1
                        level_dl += 1
                        break
                    except Exception:
                        if attempt == 2:
                            failed += 1
                        else:
                            time.sleep(0.5)

                # Rate limit: ~2 requests/sec to be respectful to OSM
                time.sleep(0.5)

                # Progress
                done = downloaded + skipped + failed
                if done % 100 == 0:
                    pct = done / total * 100
                    print(f"\r  Progress: {done:,}/{total:,} ({pct:.1f}%) "
                          f"[dl:{downloaded:,} skip:{skipped:,} fail:{failed}]",
                          end="", flush=True)

        print(f" +{level_dl} new")

    print()
    print(f"Done! Downloaded: {downloaded:,} | Skipped (cached): {skipped:,} | Failed: {failed}")

    # Estimate size
    try:
        size = sum(
            os.path.getsize(os.path.join(dp, f))
            for dp, _, filenames in os.walk(TILE_DIR)
            for f in filenames
        )
        print(f"Total tile cache size: {size / 1024 / 1024:.0f} MB")
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Download map tiles for offline use")
    parser.add_argument("--bbox", type=str, default="24.5,-125,49.5,-66.5",
                        help="Bounding box: min_lat,min_lon,max_lat,max_lon (default: CONUS)")
    parser.add_argument("--min-zoom", type=int, default=0, help="Minimum zoom level (default: 0)")
    parser.add_argument("--max-zoom", type=int, default=12,
                        help="Maximum zoom level (default: 12, ~mile detail)")
    args = parser.parse_args()

    parts = [float(x) for x in args.bbox.split(",")]
    if len(parts) != 4:
        print("Error: bbox must be min_lat,min_lon,max_lat,max_lon")
        sys.exit(1)

    min_lat, min_lon, max_lat, max_lon = parts

    total = count_tiles(min_lat, min_lon, max_lat, max_lon, args.min_zoom, args.max_zoom)
    est_mb = total * 15 / 1024  # ~15KB avg per tile
    print(f"Estimated download: {total:,} tiles (~{est_mb:.0f} MB)")
    print(f"At 2 tiles/sec, this will take ~{total / 2 / 60:.0f} minutes")
    print()

    confirm = input("Continue? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        sys.exit(0)

    download_tiles(min_lat, min_lon, max_lat, max_lon, args.min_zoom, args.max_zoom)


if __name__ == "__main__":
    main()
