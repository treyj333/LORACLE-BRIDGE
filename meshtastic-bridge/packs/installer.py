"""Pack installer — orchestrates fetch + ingest pipeline."""

import json
import logging
import os
import time
from typing import Callable, Optional

from packs.manifest import PackManifest
from packs.registry import get_pack_manifest
from packs.fetcher import fetch_document

logger = logging.getLogger("packs.installer")

PACKS_DIR = os.path.join(os.path.expanduser("~"), ".mesh-llm", "packs")


def install_pack(
    pack_id: str,
    rag_engine,
    db,
    progress_callback: Optional[Callable] = None,
) -> dict:
    """Install a pack: fetch docs + ingest into RAG.

    Args:
        pack_id: ID of the pack to install.
        rag_engine: The RAG engine instance for ingestion.
        db: SQLite connection for recording install state.
        progress_callback: Optional fn(event_type, data_dict) for SSE.

    Returns:
        dict with install results.
    """
    manifest = get_pack_manifest(pack_id)
    if manifest is None:
        return {"ok": False, "error": f"Pack '{pack_id}' not found"}

    pack_dir = os.path.join(PACKS_DIR, pack_id)
    os.makedirs(pack_dir, exist_ok=True)

    _emit(progress_callback, "pack_install_started", {"pack_id": pack_id, "name": manifest.name})

    success_count = 0
    fail_count = 0
    total_bytes = 0
    total_chunks = 0
    required_total = sum(1 for d in manifest.documents if d.required)
    required_success = 0

    for i, doc in enumerate(manifest.documents):
        doc_dict = {
            "id": doc.id, "url": doc.url, "filename": doc.filename,
            "size_bytes_estimate": doc.size_bytes_estimate,
        }
        _emit(progress_callback, "pack_doc_progress", {
            "pack_id": pack_id, "doc_id": doc.id,
            "phase": "fetching", "percent": 0,
            "index": i + 1, "total": len(manifest.documents),
        })

        # Fetch
        def _progress(doc_id, bytes_now, bytes_total):
            pct = int(bytes_now / bytes_total * 100) if bytes_total > 0 else 0
            _emit(progress_callback, "pack_doc_progress", {
                "pack_id": pack_id, "doc_id": doc_id,
                "phase": "fetching", "percent": pct,
                "bytes_now": bytes_now, "bytes_total": bytes_total,
            })

        result = fetch_document(doc_dict, pack_dir, progress_callback=_progress)

        if not result.success:
            fail_count += 1
            _emit(progress_callback, "pack_doc_failed", {
                "pack_id": pack_id, "doc_id": doc.id,
                "error": result.error, "required": doc.required,
            })
            continue

        total_bytes += result.bytes_downloaded

        # Ingest into RAG
        _emit(progress_callback, "pack_doc_progress", {
            "pack_id": pack_id, "doc_id": doc.id,
            "phase": "ingesting", "percent": 100,
        })

        chunks = 0
        try:
            if rag_engine:
                ingest_result = rag_engine.ingest_file(result.local_path)
                chunks = ingest_result.get("chunks", 0)
                total_chunks += chunks
        except Exception as e:
            logger.warning(f"[{doc.id}] Ingest failed: {e}")
            _emit(progress_callback, "pack_doc_failed", {
                "pack_id": pack_id, "doc_id": doc.id,
                "error": f"Ingest failed: {e}", "required": doc.required,
            })
            fail_count += 1
            continue

        success_count += 1
        if doc.required:
            required_success += 1

        # Record document in DB
        try:
            db.execute(
                """INSERT OR REPLACE INTO pack_documents
                   (pack_id, doc_id, local_path, sha256, bytes, fetched_at, ingested_at, chunk_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (pack_id, doc.id, result.local_path, result.sha256,
                 result.bytes_downloaded, time.time(), time.time(), chunks),
            )
            db.commit()
        except Exception as e:
            logger.debug(f"DB record error for {doc.id}: {e}")

        _emit(progress_callback, "pack_doc_completed", {
            "pack_id": pack_id, "doc_id": doc.id, "chunk_count": chunks,
        })

    # Check if enough required docs succeeded
    if required_total > 0 and required_success < required_total * 0.5:
        _emit(progress_callback, "pack_install_aborted", {
            "pack_id": pack_id, "reason": "Less than 50% of required documents succeeded",
        })
        return {
            "ok": False,
            "error": "Install aborted: too many required documents failed",
            "success_count": success_count,
            "fail_count": fail_count,
        }

    # Record pack install in DB
    try:
        db.execute(
            """INSERT OR REPLACE INTO installed_packs
               (pack_id, version, installed_at, install_path,
                doc_count_success, doc_count_failed, total_bytes, manifest_snapshot)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (pack_id, manifest.version, time.time(), pack_dir,
             success_count, fail_count, total_bytes,
             json.dumps(manifest.to_dict())),
        )
        db.commit()
    except Exception as e:
        logger.warning(f"DB record error for pack {pack_id}: {e}")

    _emit(progress_callback, "pack_install_completed", {
        "pack_id": pack_id, "success_count": success_count,
        "fail_count": fail_count, "total_bytes": total_bytes,
        "total_chunks": total_chunks,
    })

    logger.info(
        f"Pack installed: {manifest.name} · {success_count}/{len(manifest.documents)} docs · "
        f"{total_chunks} chunks · {total_bytes // 1024}KB"
    )
    return {
        "ok": True,
        "success_count": success_count,
        "fail_count": fail_count,
        "total_bytes": total_bytes,
        "total_chunks": total_chunks,
    }


def uninstall_pack(pack_id: str, rag_engine, db) -> dict:
    """Remove a pack and its chunks from RAG."""
    try:
        # Get document list from DB
        rows = db.execute(
            "SELECT doc_id, local_path FROM pack_documents WHERE pack_id = ?",
            (pack_id,),
        ).fetchall()

        removed_files = 0
        for row in rows:
            # Remove local file
            path = row["local_path"]
            if path and os.path.exists(path):
                os.remove(path)
                removed_files += 1
            # Remove from RAG by filename
            if rag_engine:
                try:
                    filename = os.path.basename(path) if path else None
                    if filename:
                        docs = rag_engine.list_documents()
                        for d in docs:
                            if d.get("filename") == filename:
                                rag_engine.delete_document(d["doc_id"])
                except Exception as e:
                    logger.debug(f"RAG cleanup for {row['doc_id']}: {e}")

        # Remove DB records
        db.execute("DELETE FROM pack_documents WHERE pack_id = ?", (pack_id,))
        db.execute("DELETE FROM installed_packs WHERE pack_id = ?", (pack_id,))
        db.commit()

        # Remove pack directory if empty
        pack_dir = os.path.join(PACKS_DIR, pack_id)
        if os.path.isdir(pack_dir):
            try:
                os.rmdir(pack_dir)
            except OSError:
                pass  # not empty, that's fine

        logger.info(f"Uninstalled pack: {pack_id} ({removed_files} files removed)")
        return {"ok": True, "removed_files": removed_files}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def reingest_pack(pack_id: str, rag_engine, db) -> dict:
    """Re-ingest existing local files without re-downloading."""
    rows = db.execute(
        "SELECT doc_id, local_path FROM pack_documents WHERE pack_id = ?",
        (pack_id,),
    ).fetchall()
    if not rows:
        return {"ok": False, "error": "Pack not installed or no documents found"}

    total_chunks = 0
    for row in rows:
        path = row["local_path"]
        if not path or not os.path.exists(path):
            continue
        try:
            result = rag_engine.ingest_file(path)
            chunks = result.get("chunks", 0)
            total_chunks += chunks
            db.execute(
                "UPDATE pack_documents SET ingested_at = ?, chunk_count = ? WHERE pack_id = ? AND doc_id = ?",
                (time.time(), chunks, pack_id, row["doc_id"]),
            )
        except Exception as e:
            logger.warning(f"Reingest {row['doc_id']}: {e}")
    db.commit()
    return {"ok": True, "total_chunks": total_chunks}


def get_installed_packs(db) -> list:
    """Return list of installed pack records."""
    try:
        rows = db.execute("SELECT * FROM installed_packs").fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_pack_documents(db, pack_id: str) -> list:
    """Return document records for an installed pack."""
    try:
        rows = db.execute(
            "SELECT * FROM pack_documents WHERE pack_id = ?", (pack_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _emit(callback, event_type, data):
    if callback:
        try:
            callback(event_type, data)
        except Exception:
            pass
