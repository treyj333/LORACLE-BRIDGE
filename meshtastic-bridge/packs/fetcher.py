"""Document fetcher — downloads docs from URLs with retry + progress."""

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests

logger = logging.getLogger("packs.fetcher")

USER_AGENT = "LORACLE-Bridge/0.6 (https://github.com/treyj333/LORACLE-BRIDGE)"
FETCH_TIMEOUT = 120
MAX_RETRIES = 3


@dataclass
class FetchResult:
    success: bool
    doc_id: str
    local_path: Optional[str] = None
    sha256: Optional[str] = None
    bytes_downloaded: int = 0
    error: Optional[str] = None


def fetch_document(
    doc: dict,
    dest_dir: str,
    progress_callback: Optional[Callable] = None,
) -> FetchResult:
    """Fetch a single document. Synchronous (runs in thread pool if needed).

    Args:
        doc: Manifest document entry dict with url, filename, id, etc.
        dest_dir: Directory to save the downloaded file.
        progress_callback: Optional fn(doc_id, bytes_now, bytes_total).
    """
    doc_id = doc["id"]
    url = doc["url"]
    filename = doc["filename"]
    dest_path = os.path.join(dest_dir, filename)

    # Skip if already downloaded
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        sha = _compute_sha256(dest_path)
        size = os.path.getsize(dest_path)
        logger.info(f"[{doc_id}] Already downloaded: {filename} ({size} bytes)")
        return FetchResult(True, doc_id, dest_path, sha, size)

    os.makedirs(dest_dir, exist_ok=True)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"[{doc_id}] Fetching {url} (attempt {attempt}/{MAX_RETRIES})")
            resp = requests.get(
                url, timeout=FETCH_TIMEOUT, stream=True,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
            )
            resp.raise_for_status()

            total = int(resp.headers.get("Content-Length", 0))
            # Warn if much larger than expected
            expected = doc.get("size_bytes_estimate", 0)
            if expected and total > expected * 2:
                logger.warning(
                    f"[{doc_id}] Content-Length {total} is >2x estimate {expected}"
                )

            hasher = hashlib.sha256()
            downloaded = 0
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(doc_id, downloaded, total)

            sha = hasher.hexdigest()
            if downloaded == 0:
                # Server returned 200 but an empty body — treat as failure so
                # we retry (and so ingestion doesn't choke on an empty file).
                logger.warning(f"[{doc_id}] Empty response body — treating as failure")
                try:
                    os.remove(dest_path)
                except OSError:
                    pass
                raise ValueError(f"{url} returned 0 bytes")
            logger.info(f"[{doc_id}] Downloaded {filename}: {downloaded} bytes, sha256={sha[:12]}...")
            return FetchResult(True, doc_id, dest_path, sha, downloaded)

        except Exception as e:
            logger.warning(f"[{doc_id}] Fetch attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)  # exponential backoff

    error_msg = f"Failed to fetch {filename} after {MAX_RETRIES} attempts"
    logger.error(f"[{doc_id}] {error_msg}")
    return FetchResult(False, doc_id, error=error_msg)


def _compute_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
