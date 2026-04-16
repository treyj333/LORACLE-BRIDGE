"""Pack registry — discovers available packs from bundled manifests."""

import glob
import logging
import os
from typing import Dict, List, Optional

from packs.manifest import PackManifest

logger = logging.getLogger("packs.registry")

_BUNDLED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bundled")


def list_available_packs() -> List[dict]:
    """Return summaries of all packs LORACLE knows about."""
    packs = []
    for path in sorted(glob.glob(os.path.join(_BUNDLED_DIR, "*.json"))):
        try:
            m = PackManifest.from_file(path)
            packs.append({
                "id": m.id,
                "name": m.name,
                "version": m.version,
                "description": m.description,
                "categories": m.categories,
                "doc_count": len(m.documents),
                "estimated_size_mb": m.estimated_size_mb,
            })
        except Exception as e:
            logger.warning(f"Failed to load pack manifest {path}: {e}")
    return packs


def get_pack_manifest(pack_id: str) -> Optional[PackManifest]:
    """Load a specific pack manifest by ID."""
    for path in glob.glob(os.path.join(_BUNDLED_DIR, "*.json")):
        try:
            m = PackManifest.from_file(path)
            if m.id == pack_id:
                return m
        except Exception:
            continue
    return None
