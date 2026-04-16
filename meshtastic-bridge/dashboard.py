"""LORACLE BRIDGE — Control Panel Dashboard.

Full-featured single-page web UI with live monitoring, controls,
debug tools, and a built-in user guide. Served as a single HTML
page with inline CSS/JS — no external assets or build step.
"""

import json
import logging
import os
import threading
import time
from collections import deque

import requests as requests_lib
from flask import Flask, jsonify, Response, request
from werkzeug.utils import secure_filename

logger = logging.getLogger("dashboard")

# ─── Shared state (bridge writes, dashboard reads) ───────────────────────────

_state = {
    "connected": False,
    "connection_type": "",
    "connection_address": "",
    "ble_available": False,
    "model": "",
    "ollama_url": "",
    "uptime_start": 0,
    "message_count": 0,
    "node_count": 0,
    "known_nodes": [],
    "rag_enabled": False,
}

_messages = deque(maxlen=100)

_metrics = {
    "total_llm_time": 0.0,
    "total_llm_calls": 0,
    "total_chunks_sent": 0,
}

_bridge = None

# SSE event subscribers — list of queue.Queue instances, one per connected client
_sse_subscribers = []


def _emit_sse(event_type: str, data: dict):
    """Push an event to all connected SSE clients."""
    import queue as _q
    payload = json.dumps({"type": event_type, **data})
    dead = []
    for i, q in enumerate(_sse_subscribers):
        try:
            q.put_nowait(payload)
        except _q.Full:
            dead.append(i)
    for i in reversed(dead):
        _sse_subscribers.pop(i)


# ─── Log capture ─────────────────────────────────────────────────────────────

class DashboardLogHandler(logging.Handler):
    """Captures log records into a ring buffer for the debug UI."""

    def __init__(self, maxlen=500):
        super().__init__()
        self._records = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            with self._lock:
                self._records.append({
                    "ts": record.created,
                    "level": record.levelname,
                    "name": record.name,
                    "message": self.format(record),
                })
        except Exception:
            pass

    def get_records(self, level=None, limit=200):
        with self._lock:
            records = list(self._records)
        if level:
            records = [r for r in records if r["level"] == level.upper()]
        return records[-limit:]


_log_handler = DashboardLogHandler()
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))


# ─── Public API (called by the bridge) ───────────────────────────────────────

def update_state(**kwargs):
    """Called by the bridge to update dashboard state."""
    _state.update(kwargs)


def record_message(direction, node_id, text, chunks=0, llm_time=0, **kwargs):
    """Called by the bridge to record a message event.

    Extra kwargs (rag_chunks, rag_docs, model, direction label, etc.)
    are stored in the message record for the Inference activity log.
    """
    msg = {
        "ts": time.time(),
        "dir": direction,
        "node": node_id,
        "text": text[:2000],
        "chunks": chunks,
        "llm_time": round(llm_time, 1),
    }
    msg.update(kwargs)
    _messages.append(msg)
    if direction == "out" and llm_time > 0:
        _metrics["total_llm_time"] += llm_time
        _metrics["total_llm_calls"] += 1
        _metrics["total_chunks_sent"] += chunks
    # Emit SSE event for messenger
    _emit_sse("thread_updated", {"contact_id": node_id, "direction": direction})


def set_bridge(bridge):
    """Store a reference to the bridge so control endpoints can call back."""
    global _bridge
    _bridge = bridge
    logger.info("Dashboard bridge reference set")


# ─── Addon tab/route registration ────────────────────────────────────────────

_addon_tabs = []  # List of {id, label, html, js} dicts


def register_addon_tab(tab_config: dict):
    """Register a dashboard tab from an addon.

    Args:
        tab_config: Dict with keys: id, label, html, js
    """
    _addon_tabs.append(tab_config)
    logger.info(f"Dashboard tab registered: {tab_config.get('label', tab_config.get('id'))}")


def register_addon_api_route(method: str, path: str, handler):
    """Register an API route from an addon.

    Args:
        method: HTTP method (GET, POST, DELETE, etc.)
        path: URL path (e.g. "/api/dead_drop/pending")
        handler: Flask view function
    """
    endpoint = path.replace("/", "_").strip("_")
    app.add_url_rule(path, endpoint=endpoint, view_func=handler, methods=[method])
    logger.info(f"API route registered: {method} {path}")


def _inject_addon_tabs(html: str) -> str:
    """Inject addon sections into CONFIG tab at serve time."""
    if not _addon_tabs:
        return html

    addon_sections = ""
    addon_js = ""
    for tab in _addon_tabs:
        label_upper = tab["label"].upper()
        addon_sections += (
            f'<details class="lo-section" id="addon-{tab["id"]}">\n'
            f'  <summary class="lo-section-head">{label_upper}</summary>\n'
            f'  <div class="lo-section-body">\n'
            f'    {tab["html"]}\n'
            f'  </div>\n'
            f'</details>\n'
        )
        addon_js += f'\n// --- Addon: {tab["label"]} ---\n{tab["js"]}\n'

    html = html.replace("<!-- ADDON_SECTIONS -->", addon_sections + "<!-- ADDON_SECTIONS -->")
    html = html.replace("</script>\n</body>", addon_js + "\n</script>\n</body>")

    return html


# ─── Flask app ───────────────────────────────────────────────────────────────

app = Flask(__name__)

# ─── Offline tile serving ───────────────────────────────────────────────────

_TILE_DIR = os.path.expanduser("~/.mesh-llm/tiles")


@app.route("/tiles/<int:z>/<int:x>/<int:y>.png")
def serve_tile(z, x, y):
    """Serve map tiles from local cache, falling back to online."""
    tile_path = os.path.join(_TILE_DIR, str(z), str(x), f"{y}.png")
    if os.path.exists(tile_path):
        return Response(open(tile_path, "rb").read(), mimetype="image/png")
    # Fallback: proxy from OSM (and cache for offline use)
    try:
        import random
        server = random.choice(["a", "b", "c"])
        url = f"https://{server}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        resp = requests_lib.get(url, timeout=5, headers={"User-Agent": "LORACLE-Bridge/1.0"})
        if resp.status_code == 200:
            os.makedirs(os.path.dirname(tile_path), exist_ok=True)
            with open(tile_path, "wb") as f:
                f.write(resp.content)
            return Response(resp.content, mimetype="image/png")
    except Exception:
        pass
    return Response(b"", status=404)


@app.route("/")
def index():
    html = _inject_addon_tabs(DASHBOARD_HTML)
    return Response(html, mimetype="text/html")


@app.route("/api/state")
def api_state():
    state = dict(_state)
    state["uptime"] = int(time.time() - state["uptime_start"]) if state["uptime_start"] else 0
    state["messages"] = list(_messages)
    # Performance metrics
    calls = _metrics["total_llm_calls"]
    state["avg_llm_time"] = round(_metrics["total_llm_time"] / calls, 1) if calls > 0 else 0
    state["avg_chunks"] = round(_metrics["total_chunks_sent"] / calls, 1) if calls > 0 else 0
    state["total_llm_calls"] = calls
    # Node positions for map — read live from bridge, not from stale _state
    if _bridge and hasattr(_bridge, "_node_positions"):
        state["node_positions"] = _bridge._node_positions
        state["node_positions_count"] = len(_bridge._node_positions)
        state["node_meta"] = getattr(_bridge, "_node_meta", {}) or {}
        # Merge known_nodes from _known_nodes set + node_positions keys
        # so nodes discovered via nodeDB/position show up immediately
        all_nodes = set(getattr(_bridge, "_known_nodes", set()))
        all_nodes.update(_bridge._node_positions.keys())
        state["known_nodes"] = sorted(all_nodes)
        state["node_count"] = len(all_nodes)
        try:
            nodedb = getattr(_bridge.interface, "nodes", None) if _bridge.interface else None
            state["nodedb_size"] = len(nodedb) if nodedb else 0
        except Exception:
            state["nodedb_size"] = 0
    else:
        state["node_positions"] = {}
        state["node_positions_count"] = 0
        state["node_meta"] = {}
        state["nodedb_size"] = 0
    # Greeter status (auto-greet new nodes feature)
    if _bridge and hasattr(_bridge, "greeter"):
        try:
            state["greeter"] = _bridge.greeter.stats()
        except Exception:
            state["greeter"] = {}
    else:
        state["greeter"] = {}
    # Radio backends info
    if _bridge and hasattr(_bridge, "_radio_manager"):
        try:
            state["backends"] = _bridge._radio_manager.get_backends_info()
        except Exception:
            state["backends"] = []
    else:
        state["backends"] = []
    # AI replies toggle
    state["ai_replies_enabled"] = getattr(_bridge, "_ai_replies_enabled", True) if _bridge else True
    # Total unread from DB
    if _bridge and hasattr(_bridge, "_contact_store"):
        try:
            state["total_unread"] = _bridge._contact_store.total_unread()
        except Exception:
            state["total_unread"] = 0
    else:
        state["total_unread"] = 0
    return jsonify(state)


@app.route("/api/coverage/stats")
def api_coverage_stats():
    """Return summary stats about the coverage log."""
    if _bridge is None or not hasattr(_bridge, "coverage"):
        return jsonify({"count": 0, "nodes": 0, "time_start": None, "time_end": None, "bbox": None})
    return jsonify(_bridge.coverage.stats())


@app.route("/api/coverage/clear", methods=["POST"])
def api_coverage_clear():
    """Truncate the coverage log file. Returns the number of samples removed."""
    if _bridge is None or not hasattr(_bridge, "coverage"):
        return jsonify({"ok": False, "error": "Bridge not initialized"}), 503
    removed = _bridge.coverage.clear()
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/coverage/samples")
def api_coverage_samples():
    """Return raw coverage samples. Optional ?limit=N (default 5000)."""
    if _bridge is None or not hasattr(_bridge, "coverage"):
        return jsonify({"samples": []})
    try:
        limit = min(int(request.args.get("limit", 5000)), 10000)
    except ValueError:
        limit = 5000
    samples = _bridge.coverage.read_all(limit=limit)
    return jsonify({"samples": samples, "count": len(samples)})


@app.route("/api/models", methods=["GET"])
def api_models():
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    models = _bridge.ollama.list_models()
    return jsonify({"models": models, "current": _bridge.ollama.model})


@app.route("/api/model", methods=["POST"])
def api_set_model():
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    data = request.get_json(silent=True) or {}
    model = data.get("model", "")
    if not model:
        return jsonify({"ok": False, "error": "No model specified"}), 400
    success = _bridge.ollama.set_model(model)
    if success:
        update_state(model=_bridge.ollama.model)
        return jsonify({"ok": True, "model": _bridge.ollama.model})
    available = _bridge.ollama.list_models()
    return jsonify({"ok": False, "error": f"Model '{model}' not found", "available": available}), 404


@app.route("/api/clear-history", methods=["POST"])
def api_clear_history():
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    data = request.get_json(silent=True) or {}
    node_id = data.get("node_id")
    if node_id:
        _bridge.ollama.clear_history(node_id)
        return jsonify({"ok": True, "cleared": 1})
    # Clear all
    count = _bridge.ollama.clear_all_history()
    return jsonify({"ok": True, "cleared": count})


@app.route("/api/rag/toggle", methods=["POST"])
def api_rag_toggle():
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    if not hasattr(_bridge, "rag_engine") or _bridge.rag_engine is None:
        return jsonify({"ok": False, "error": "RAG not available (start with --rag)"}), 400
    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled", not _bridge.rag_enabled)
    _bridge.rag_enabled = enabled
    update_state(rag_enabled=enabled)
    return jsonify({"ok": True, "rag_enabled": enabled})


@app.route("/api/rag/stats", methods=["GET"])
def api_rag_stats():
    if _bridge is None or not hasattr(_bridge, "rag_engine") or _bridge.rag_engine is None:
        return jsonify({"available": False})
    try:
        stats = _bridge.rag_engine.get_stats()
        docs = _bridge.rag_engine.list_documents()
        return jsonify({"available": True, "stats": stats, "documents": docs})
    except Exception as e:
        return jsonify({"available": False, "error": str(e)})


@app.route("/api/rag/ingest-url", methods=["POST"])
def api_rag_ingest_url():
    """Fetch a URL, extract text, save to CONTEXT FILES, and ingest into RAG."""
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    if not hasattr(_bridge, "rag_engine") or _bridge.rag_engine is None:
        return jsonify({"error": "RAG not available"}), 400

    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"error": "Invalid URL. Must start with http:// or https://"}), 400

    try:
        import re as _re
        from urllib.parse import urlparse
        from rag.extractors import _strip_html, _clean_text

        # Fetch the URL
        resp = requests_lib.get(
            url, timeout=30,
            headers={"User-Agent": "LORACLE-Bridge/1.0"},
        )
        resp.raise_for_status()

        # Cap response size (5MB)
        content = resp.text[:5_000_000]

        # Extract text
        text = _strip_html(content)
        text = _clean_text(text)

        if not text or len(text.strip()) < 50:
            return jsonify({"error": "No meaningful text extracted from URL"}), 400

        # Generate filename from URL
        parsed = urlparse(url)
        slug = parsed.netloc + parsed.path
        slug = _re.sub(r"[^\w\-.]", "_", slug).strip("_")[:80]
        if not slug:
            slug = "web_page"
        filename = f"{slug}.txt"

        # Save to CONTEXT FILES directory
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        context_dir = os.path.join(project_root, "CONTEXT FILES")
        os.makedirs(context_dir, exist_ok=True)
        save_path = os.path.join(context_dir, filename)

        # Check for duplicate
        if _bridge.rag_engine.is_ingested(filename):
            return jsonify({"error": f"Already ingested: {filename}"}), 409

        with open(save_path, "w", encoding="utf-8") as f:
            f.write(text)

        # Ingest into RAG
        result = _bridge.rag_engine.ingest_file(save_path)

        return jsonify({
            "ok": True,
            "filename": result["filename"],
            "chunks": result["chunks"],
            "doc_id": result["doc_id"],
        })

    except requests_lib.Timeout:
        return jsonify({"error": "URL fetch timed out (30s)"}), 408
    except requests_lib.ConnectionError:
        return jsonify({"error": "Could not connect to URL"}), 502
    except requests_lib.HTTPError as e:
        return jsonify({"error": f"HTTP error: {e.response.status_code}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rag/delete", methods=["POST"])
def api_rag_delete():
    """Delete a document from the RAG knowledge base."""
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    if not hasattr(_bridge, "rag_engine") or _bridge.rag_engine is None:
        return jsonify({"error": "RAG not available"}), 400

    data = request.get_json(silent=True) or {}
    doc_id = data.get("doc_id", "").strip()
    if not doc_id:
        return jsonify({"error": "Missing doc_id"}), 400

    try:
        deleted = _bridge.rag_engine.delete_document(doc_id)
        return jsonify({"ok": True, "deleted": deleted})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system-prompt", methods=["GET"])
def api_get_system_prompt():
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    return jsonify({"prompt": _bridge.ollama.system_prompt})


@app.route("/api/system-prompt", methods=["POST"])
def api_set_system_prompt():
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "")
    if not prompt:
        return jsonify({"ok": False, "error": "Empty prompt"}), 400
    _bridge.ollama.system_prompt = prompt
    return jsonify({"ok": True})


@app.route("/api/config", methods=["GET"])
def api_get_config():
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    return jsonify({
        "max_response_length": _bridge.ollama.max_response_length,
        "compression_enabled": _bridge.compression_enabled,
        "connection_type": _bridge.connection_type,
        "ollama_url": _bridge.ollama.base_url,
    })


@app.route("/api/config", methods=["POST"])
def api_set_config():
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    data = request.get_json(silent=True) or {}
    updated = {}
    if "max_response_length" in data:
        val = int(data["max_response_length"])
        _bridge.ollama.max_response_length = val
        updated["max_response_length"] = val
    if "compression_enabled" in data:
        val = bool(data["compression_enabled"])
        _bridge.compression_enabled = val
        updated["compression_enabled"] = val
    return jsonify({"ok": True, "updated": updated})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Direct LLM chat from the dashboard — bypasses the radio."""
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "Empty message"}), 400
    try:
        # Use "dashboard" as the node ID for conversation history
        context_messages = None
        if _bridge.rag_enabled and _bridge.rag_engine:
            try:
                context_messages = _bridge.rag_engine.build_context_messages(message)
            except Exception:
                pass
        response = _bridge.ollama.chat("dashboard", message, context_messages=context_messages)
        return jsonify({"ok": True, "response": response})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/logs", methods=["GET"])
def api_logs():
    level = request.args.get("level", None)
    limit = min(int(request.args.get("limit", 200)), 10000)
    records = _log_handler.get_records(level=level, limit=limit)
    return jsonify({"logs": records})


@app.route("/api/debug", methods=["GET"])
def api_debug():
    info = {
        "thread_count": threading.active_count(),
        "queue_size": 0,
        "dedup_cache_size": 0,
        "known_nodes": list(_state.get("known_nodes", [])),
    }
    if _bridge is not None:
        try:
            info["queue_size"] = _bridge._request_queue.qsize()
        except Exception:
            pass
    # Try to get dedup cache size
    try:
        from standalone_bridge import get_dedup_cache_size
        info["dedup_cache_size"] = get_dedup_cache_size()
    except Exception:
        pass
    return jsonify(info)


# ─── BLE / Connection management endpoints ─────────────────────────────────

@app.route("/api/ble/available", methods=["GET"])
def api_ble_available():
    try:
        from standalone_bridge import BLE_AVAILABLE
        return jsonify({"available": BLE_AVAILABLE})
    except Exception:
        return jsonify({"available": False})


@app.route("/api/ble/scan", methods=["GET"])
def api_ble_scan():
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    try:
        from standalone_bridge import BLE_AVAILABLE
        if not BLE_AVAILABLE:
            return jsonify({"error": "BLE not available. Requires Python 3.11+ with bleak."}), 400
    except ImportError:
        return jsonify({"error": "BLE not available"}), 400

    timeout = float(request.args.get("timeout", 10))
    devices = _bridge.scan_ble_devices(timeout=timeout)
    return jsonify({"devices": devices})


@app.route("/api/ble/last-device", methods=["GET"])
def api_ble_last_device():
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    device = _bridge.load_last_ble_device()
    return jsonify({"device": device})


@app.route("/api/connection/switch", methods=["POST"])
def api_connection_switch():
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    data = request.get_json(silent=True) or {}
    conn_type = data.get("type", "").strip()
    if conn_type not in ("serial", "tcp", "ble"):
        return jsonify({"ok": False, "error": "Invalid type. Use: serial, tcp, ble"}), 400

    address = data.get("address", "").strip() or None
    host = data.get("host", "").strip() or None
    port = data.get("port")
    if port:
        port = int(port)

    result = _bridge.switch_connection(conn_type, address=address, host=host, port=port)
    return jsonify(result)


@app.route("/api/connection/disconnect", methods=["POST"])
def api_connection_disconnect():
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    _bridge.disconnect_radio()
    return jsonify({"ok": True})


@app.route("/api/radios", methods=["GET"])
def api_radios():
    """Return info about all active radio backends."""
    if _bridge is None or not hasattr(_bridge, "_radio_manager"):
        return jsonify({"backends": []})
    return jsonify({"backends": _bridge._radio_manager.get_backends_info()})


@app.route("/api/ai-replies", methods=["GET"])
def api_ai_replies_get():
    if _bridge is None:
        return jsonify({"enabled": True})
    return jsonify({"enabled": getattr(_bridge, "_ai_replies_enabled", True)})


@app.route("/api/ai-replies", methods=["POST"])
def api_ai_replies_set():
    """Toggle global AI auto-reply on/off."""
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled", True)
    _bridge._ai_replies_enabled = bool(enabled)
    # Persist
    try:
        from standalone_bridge import load_settings, save_settings
        s = load_settings()
        s["ai_replies"] = bool(enabled)
        save_settings(s)
    except Exception:
        pass
    return jsonify({"ok": True, "enabled": _bridge._ai_replies_enabled})


# ─── Thread / Contact endpoints ─────────────────────────────────────────────

@app.route("/api/threads", methods=["GET"])
def api_threads():
    """List all contacts with summary info for the messenger sidebar."""
    if _bridge is None or not hasattr(_bridge, "_contact_store"):
        return jsonify({"threads": []})
    try:
        threads = _bridge._contact_store.list_with_preview()
        return jsonify({"threads": threads})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/threads/<path:thread_id>", methods=["GET"])
def api_thread_detail(thread_id):
    """Get contact details + recent messages."""
    if _bridge is None or not hasattr(_bridge, "_contact_store"):
        return jsonify({"error": "Not initialized"}), 503
    contact = _bridge._contact_store.get(thread_id)
    if contact is None:
        return jsonify({"error": "Contact not found"}), 404
    messages = _bridge._message_store.get_thread(thread_id, limit=50)
    return jsonify({"contact": contact, "messages": messages})


@app.route("/api/threads/<path:thread_id>/messages", methods=["GET"])
def api_thread_messages(thread_id):
    """Paginated message history."""
    if _bridge is None or not hasattr(_bridge, "_message_store"):
        return jsonify({"error": "Not initialized"}), 503
    limit = min(int(request.args.get("limit", 50)), 500)
    offset = int(request.args.get("offset", 0))
    messages = _bridge._message_store.get_thread(thread_id, limit=limit, offset=offset)
    total = _bridge._message_store.count_by_contact(thread_id)
    return jsonify({
        "messages": messages,
        "has_more": offset + limit < total,
        "total": total,
    })


@app.route("/api/threads/<path:thread_id>/open", methods=["POST"])
def api_thread_open(thread_id):
    """Mark thread as viewed — resets unread count."""
    if _bridge is None or not hasattr(_bridge, "_contact_store"):
        return jsonify({"error": "Not initialized"}), 503
    _bridge._contact_store.reset_unread(thread_id)
    return jsonify({"ok": True})


@app.route("/api/threads/<path:thread_id>/close", methods=["POST"])
def api_thread_close(thread_id):
    """Thread no longer in view."""
    return jsonify({"ok": True})


@app.route("/api/threads/<path:thread_id>/send", methods=["POST"])
def api_thread_send(thread_id):
    """Send a manual message to a contact."""
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Empty message"}), 400
    contact = _bridge._contact_store.get(thread_id)
    if contact is None:
        return jsonify({"error": "Contact not found"}), 404
    try:
        _bridge._radio_manager.send(thread_id, text, is_dm=True)
        msg_id = _bridge._message_store.insert(
            contact_id=thread_id, direction="out", author="human",
            text=text, protocol=contact["protocol"],
        )
        record_message("out", thread_id, text)
        return jsonify({"ok": True, "msg_id": msg_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/threads/<path:thread_id>/ai-toggle", methods=["POST"])
def api_thread_ai_toggle(thread_id):
    """Cycle ai_enabled: NULL→0→1→NULL."""
    if _bridge is None or not hasattr(_bridge, "_contact_store"):
        return jsonify({"error": "Not initialized"}), 503
    new_val = _bridge._contact_store.cycle_ai_enabled(thread_id)
    effective = _bridge._contact_store.get_effective_ai(
        thread_id, _bridge._ai_replies_enabled
    )
    return jsonify({"ok": True, "ai_enabled": new_val, "effective": effective})


@app.route("/api/events", methods=["GET"])
def api_events():
    """Server-sent events stream for realtime messenger updates."""
    import queue as _q
    client_q = _q.Queue(maxsize=50)
    _sse_subscribers.append(client_q)
    def generate():
        try:
            while True:
                try:
                    payload = client_q.get(timeout=15)
                    yield f"data: {payload}\n\n"
                except _q.Empty:
                    yield f"data: {json.dumps({'type': 'heartbeat', 'ts': time.time()})}\n\n"
        except GeneratorExit:
            pass
        finally:
            try:
                _sse_subscribers.remove(client_q)
            except ValueError:
                pass
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/db/stats", methods=["GET"])
def api_db_stats():
    """Database statistics."""
    if _bridge is None or not hasattr(_bridge, "_contact_store"):
        return jsonify({"contacts": 0, "messages": 0, "db_size_bytes": 0})
    import os as _os
    try:
        db_path = _os.path.join(_os.path.expanduser("~"), ".mesh-llm", "loracle.db")
        size = _os.path.getsize(db_path) if _os.path.exists(db_path) else 0
    except Exception:
        size = 0
    return jsonify({
        "contacts": _bridge._contact_store.count(),
        "messages": _bridge._message_store.count_total(),
        "db_size_bytes": size,
    })


@app.route("/api/db/prune", methods=["POST"])
def api_db_prune():
    """Run message retention pruning."""
    if _bridge is None or not hasattr(_bridge, "_message_store"):
        return jsonify({"error": "Not initialized"}), 503
    pruned = _bridge._message_store.prune()
    return jsonify({"ok": True, "pruned": pruned})


@app.route("/api/db/clear-messages", methods=["POST"])
def api_db_clear_messages():
    """Delete all messages (keeps contacts)."""
    if _bridge is None or not hasattr(_bridge, "_message_store"):
        return jsonify({"error": "Not initialized"}), 503
    deleted = _bridge._message_store.delete_all()
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/routing/config", methods=["GET"])
def api_routing_config():
    """Get routing configuration."""
    if _bridge is None or not hasattr(_bridge, "_settings_store"):
        return jsonify({"auto_enabled": True, "show_tier_tag": True, "tiers": {}})
    try:
        from routing.tiers import load_tiers, ROUTING_AUTO_KEY, ROUTING_SHOW_TAG_KEY, Tier
        ss = _bridge._settings_store
        tiers = load_tiers(ss)
        return jsonify({
            "auto_enabled": ss.get(ROUTING_AUTO_KEY, True),
            "show_tier_tag": ss.get(ROUTING_SHOW_TAG_KEY, True),
            "tiers": {t.value: {"model": c.model, "enabled": c.enabled} for t, c in tiers.items()},
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/routing/config", methods=["POST"])
def api_routing_config_set():
    """Update routing configuration."""
    if _bridge is None or not hasattr(_bridge, "_settings_store"):
        return jsonify({"error": "Not initialized"}), 503
    data = request.get_json(silent=True) or {}
    ss = _bridge._settings_store
    try:
        from routing.tiers import ROUTING_AUTO_KEY, ROUTING_SHOW_TAG_KEY, Tier, TierConfig, save_tiers, load_tiers
        if "auto_enabled" in data:
            ss.set(ROUTING_AUTO_KEY, bool(data["auto_enabled"]))
        if "show_tier_tag" in data:
            ss.set(ROUTING_SHOW_TAG_KEY, bool(data["show_tier_tag"]))
        if "tiers" in data:
            current = load_tiers(ss)
            for tier_key, tier_data in data["tiers"].items():
                tier = Tier(tier_key)
                if "model" in tier_data:
                    current[tier] = TierConfig(tier, tier_data["model"], current[tier].enabled)
                if "enabled" in tier_data:
                    current[tier] = TierConfig(tier, current[tier].model, bool(tier_data["enabled"]))
            save_tiers(ss, current)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/routing/classify", methods=["POST"])
def api_routing_classify():
    """Test the classifier on a query (no LLM call)."""
    data = request.get_json(silent=True) or {}
    query = data.get("query", "")
    if not query:
        return jsonify({"tier": "std"})
    try:
        from routing.classifier import classify
        from routing.tiers import load_tiers, Tier
        tier = classify(query)
        model = "?"
        if _bridge and hasattr(_bridge, "_settings_store"):
            tiers = load_tiers(_bridge._settings_store)
            if tier in tiers:
                model = tiers[tier].model
        return jsonify({"tier": tier.value, "model": model})
    except Exception as e:
        return jsonify({"tier": "std", "error": str(e)})


# ─── Pack endpoints ──────────────────────────────────────────────────────────

@app.route("/api/packs", methods=["GET"])
def api_packs():
    """List available packs with install status."""
    from packs.registry import list_available_packs
    from packs.installer import get_installed_packs
    available = list_available_packs()
    installed = {}
    if _bridge and hasattr(_bridge, "_db"):
        for p in get_installed_packs(_bridge._db):
            installed[p["pack_id"]] = p
    for p in available:
        inst = installed.get(p["id"])
        p["installed"] = inst is not None
        if inst:
            p["installed_at"] = inst.get("installed_at")
            p["doc_count_success"] = inst.get("doc_count_success", 0)
            p["doc_count_failed"] = inst.get("doc_count_failed", 0)
            p["total_bytes"] = inst.get("total_bytes", 0)
    return jsonify({"packs": available})


@app.route("/api/packs/<pack_id>", methods=["GET"])
def api_pack_detail(pack_id):
    """Full manifest + install status."""
    from packs.registry import get_pack_manifest
    from packs.installer import get_installed_packs, get_pack_documents
    manifest = get_pack_manifest(pack_id)
    if manifest is None:
        return jsonify({"error": "Pack not found"}), 404
    result = manifest.to_dict()
    result["installed"] = False
    if _bridge and hasattr(_bridge, "_db"):
        for p in get_installed_packs(_bridge._db):
            if p["pack_id"] == pack_id:
                result["installed"] = True
                result["installed_at"] = p.get("installed_at")
                result["doc_count_success"] = p.get("doc_count_success", 0)
                result["doc_count_failed"] = p.get("doc_count_failed", 0)
                result["total_bytes"] = p.get("total_bytes", 0)
                result["installed_docs"] = get_pack_documents(_bridge._db, pack_id)
    return jsonify(result)


@app.route("/api/packs/<pack_id>/install", methods=["POST"])
def api_pack_install(pack_id):
    """Start pack install (runs in background thread)."""
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    rag = getattr(_bridge, "rag_engine", None)
    db = getattr(_bridge, "_db", None)
    if db is None:
        return jsonify({"error": "Database not initialized"}), 503

    import threading
    from packs.installer import install_pack as _install

    def _run():
        def _progress(event_type, data):
            _emit_sse(event_type, data)
        _install(pack_id, rag, db, progress_callback=_progress)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True, "status": "installing"})


@app.route("/api/packs/<pack_id>/uninstall", methods=["POST"])
def api_pack_uninstall(pack_id):
    """Remove pack + its chunks from RAG."""
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    from packs.installer import uninstall_pack
    result = uninstall_pack(pack_id, getattr(_bridge, "rag_engine", None), _bridge._db)
    return jsonify(result)


@app.route("/api/packs/<pack_id>/reingest", methods=["POST"])
def api_pack_reingest(pack_id):
    """Re-ingest existing local files."""
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    from packs.installer import reingest_pack
    result = reingest_pack(pack_id, getattr(_bridge, "rag_engine", None), _bridge._db)
    return jsonify(result)


@app.route("/api/send-mesh", methods=["POST"])
def api_send_mesh():
    """Send a manual message to the mesh from the dashboard."""
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    if not _bridge.interface or not _bridge._is_interface_alive():
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Empty message"}), 400
    node_id = data.get("node_id", "").strip()
    try:
        channel = max(0, min(int(data.get("channel", 0)), 7))
    except (TypeError, ValueError):
        channel = 0
    try:
        from meshtastic import BROADCAST_ADDR
        is_broadcast = (not node_id or node_id.lower() == "broadcast")
        dest = BROADCAST_ADDR if is_broadcast else node_id
        _bridge.interface.sendText(
            text, destinationId=dest, channelIndex=channel, wantAck=False,
        )
        direction = "broadcast" if is_broadcast else f"DM to {node_id}"
        # Note: record_message's first positional arg is also named "direction"
        # (in/out). Pass the human label as a distinct kwarg so it lands in
        # the message record without colliding.
        record_message("out", "dashboard", text, dest_label=direction)
        return jsonify({"ok": True, "direction": direction})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ask", methods=["POST"])
def api_ask():
    """Ask LORACLE a question directly from the dashboard.

    Bypasses the radio: the question is injected straight into the existing
    command-dispatch / Ollama pipeline, and the answer is returned to the
    dashboard. If ``dest`` is not ``"local"``, the answer is also broadcast
    over the mesh via the existing ``_send_response`` chunk/pager path.

    Body: ``{text, dest, channel}`` where ``dest`` is ``"local"``,
    ``"broadcast"``, or a concrete ``!hex`` node id.
    """
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    if not hasattr(_bridge, "ollama") or _bridge.ollama is None:
        return jsonify({"error": "Ollama not initialized"}), 503

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Empty message"}), 400
    dest = (data.get("dest") or "local").strip()
    try:
        channel = max(0, min(int(data.get("channel", 0)), 7))
    except (TypeError, ValueError):
        channel = 0

    # Sentinel id for Ollama history namespacing — never touches real node history.
    dash_id = "!dashboard"

    # Log the question in the dashboard message log
    record_message("in", "dashboard", text, dest_label="ask")

    # 1. Command dispatch first (!nav, !help, !triage, ...)
    answer = None
    elapsed = 0.0
    try:
        if text.startswith("!"):
            try:
                cmd_response = _bridge._handle_command(dash_id, text)
            except Exception as e:
                logger.warning(f"Ask: command dispatch error: {e}")
                cmd_response = None
            if cmd_response is not None:
                answer = cmd_response

        # 2. Regular LLM query (with optional RAG context)
        if answer is None:
            context_messages = None
            try:
                if (
                    getattr(_bridge, "rag_enabled", False)
                    and getattr(_bridge, "rag_engine", None) is not None
                ):
                    context_messages = _bridge.rag_engine.build_context_messages(text)
            except Exception as e:
                logger.debug(f"Ask: RAG context build failed: {e}")
                context_messages = None
            start = time.time()
            answer = _bridge.ollama.chat(
                dash_id, text, context_messages=context_messages
            )
            elapsed = time.time() - start
    except Exception as e:
        logger.error(f"Ask: LLM call failed: {e}")
        return jsonify({"ok": False, "error": f"LLM error: {e}"}), 500

    if not answer or not str(answer).strip():
        return jsonify({"ok": False, "error": "Empty answer from LLM"}), 500

    # 3. Optionally rebroadcast over the mesh
    transmitted = False
    tx_error = None
    dest_label = "local"
    dest_lower = dest.lower()

    if dest_lower == "local":
        dest_label = "local"
    else:
        # Must have a live interface to transmit
        if not _bridge.interface or not _bridge._is_interface_alive():
            tx_error = "Radio not connected — answer returned locally only"
        else:
            try:
                if dest_lower == "broadcast":
                    _bridge._send_response(
                        dash_id, str(answer), channel=channel, is_dm=False
                    )
                    dest_label = f"broadcast ch{channel}"
                    transmitted = True
                else:
                    # Assume a concrete node id (existing dropdown validates this)
                    _bridge._send_response(
                        dest, str(answer), channel=channel, is_dm=True
                    )
                    dest_label = f"DM to {dest}"
                    transmitted = True
            except Exception as e:
                tx_error = str(e)
                logger.error(f"Ask: rebroadcast failed: {e}")

    # Log the answer in the dashboard message log
    record_message(
        "out",
        "dashboard",
        str(answer),
        chunks=1,
        llm_time=elapsed,
        dest_label=dest_label,
    )

    payload = {
        "ok": True,
        "question": text,
        "answer": str(answer),
        "transmitted": transmitted,
        "dest_label": dest_label,
        "llm_time": round(elapsed, 2),
    }
    if tx_error:
        payload["tx_error"] = tx_error
    return jsonify(payload)


@app.route("/api/rag/ingest-file", methods=["POST"])
def api_rag_ingest_file():
    """Accept a file upload and ingest into the main RAG knowledge base."""
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    if not hasattr(_bridge, "rag_engine") or _bridge.rag_engine is None:
        return jsonify({"error": "RAG not available"}), 400
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "No filename"}), 400
    import tempfile
    safe_name = secure_filename(f.filename)
    if not safe_name:
        return jsonify({"error": "Invalid filename"}), 400
    tmp_path = os.path.join(tempfile.gettempdir(), safe_name)
    try:
        f.save(tmp_path)
        result = _bridge.rag_engine.ingest_file(tmp_path)
        return jsonify({"ok": True, "filename": result.get("filename", safe_name),
                        "chunks": result.get("chunks", 0)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def start_dashboard(port=8000):
    """Start the dashboard in a background thread."""
    # Set BLE availability in state
    try:
        from standalone_bridge import BLE_AVAILABLE
        update_state(ble_available=BLE_AVAILABLE)
    except Exception:
        pass

    # Install log handler on root logger to capture everything
    root_logger = logging.getLogger()
    _log_handler.setLevel(logging.DEBUG)
    root_logger.addHandler(_log_handler)

    def run():
        # Suppress Flask request logging
        wlog = logging.getLogger("werkzeug")
        wlog.setLevel(logging.WARNING)
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    logger.info(f"Dashboard running at http://localhost:{port}")


# ─── HTML template ───────────────────────────────────────────────────────────


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LORACLE BRIDGE</title>
<link rel="stylesheet" href="/static/leaflet/leaflet.css">
<script src="/static/leaflet/leaflet.js"></script>
<script src="/static/leaflet/leaflet-heat.js"></script>
<style>
/* ── Font Faces ───────────────────────────────────────────────────────────── */
@font-face {
  font-family: 'IBM Plex Mono';
  font-style: normal;
  font-weight: 400;
  font-display: swap;
  src: url('/static/fonts/IBMPlexMono-Regular.ttf') format('truetype');
}
@font-face {
  font-family: 'IBM Plex Mono';
  font-style: normal;
  font-weight: 500;
  font-display: swap;
  src: url('/static/fonts/IBMPlexMono-Medium.ttf') format('truetype');
}

/* ── Theme Tokens ─────────────────────────────────────────────────────────── */
:root, [data-theme="light"] {
  --lo-bg: #ebe6dc;
  --lo-bg-deep: #dcd5c6;
  --lo-ink: #1a1815;
  --lo-dim: #6b655a;
  --lo-faint: #9a948a;
  --lo-divider: rgba(26,24,21,0.08);
  --lo-divider-strong: rgba(26,24,21,0.18);
  --lo-accent: #ff4f00;
  --lo-accent-2: #0f6e56;
}
[data-theme="dark"] {
  --lo-bg: #121110;
  --lo-bg-deep: #1f1d1a;
  --lo-ink: #ede7d9;
  --lo-dim: #a39d92;
  --lo-faint: #6d675e;
  --lo-divider: rgba(237,231,217,0.12);
  --lo-divider-strong: rgba(237,231,217,0.22);
  --lo-accent: #ff4f00;
  --lo-accent-2: #5dcaa5;
}

/* ── Backward-compat aliases (addon CSS) ──────────────────────────────────── */
:root, [data-theme="light"], [data-theme="dark"] {
  --text-primary: var(--lo-ink);
  --text-secondary: var(--lo-dim);
  --text-muted: var(--lo-faint);
  --text-dim: var(--lo-faint);
  --bg-primary: var(--lo-bg);
  --bg-secondary: var(--lo-bg-deep);
  --bg-tertiary: var(--lo-bg-deep);
  --bg-input: var(--lo-bg-deep);
  --border: var(--lo-divider-strong);
  --border-subtle: var(--lo-divider);
  --border-width: 1px;
  --accent-blue: var(--lo-accent);
  --accent-green: var(--lo-accent-2);
  --accent-red: #c0392b;
  --accent-yellow: #d4a017;
  --accent-orange: var(--lo-accent);
  --accent-purple: #7b68ee;
  --shadow-raised: none;
  --shadow-inset: none;
  --glow-green: none;
  --glow-amber: none;
  --glow-red: none;
  --radius: 0;
  --radius-sm: 0;
  --font-mono: 'IBM Plex Mono', 'Menlo', 'Monaco', monospace;
  --font-sans: var(--font-mono);
}

/* ── Base Reset ───────────────────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 12px; line-height: 1.7; }
body {
  font-family: var(--font-mono);
  font-weight: 400;
  font-variant-numeric: tabular-nums;
  background: var(--lo-bg);
  color: var(--lo-ink);
  -webkit-font-smoothing: antialiased;
}
::selection { background: var(--lo-accent); color: #fff; }

/* ── Shell ────────────────────────────────────────────────────────────────── */
.lo-shell {
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 24px;
  background: var(--lo-bg);
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

/* ── Title Bar ────────────────────────────────────────────────────────────── */
.lo-title-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 0;
  background: var(--lo-bg-deep);
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--lo-dim);
  flex-wrap: wrap;
  border-bottom: 1px solid var(--lo-divider-strong);
}
.lo-title-bar .lo-brand {
  color: var(--lo-ink);
  font-weight: 500;
}
.lo-conn-dot {
  display: inline-block;
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--lo-faint);
  margin-left: 10px;
}
.lo-conn-dot.on { background: var(--lo-accent-2); animation: loPulse 2s ease-in-out infinite; }
.lo-conn-label { color: var(--lo-dim); margin-right: auto; }
.lo-title-bar .lo-brand .lo-accent { color: var(--lo-accent); }
.lo-clock { font-weight: 500; color: var(--lo-ink); }
.lo-title-bar button {
  background: none; border: none; cursor: pointer;
  color: var(--lo-dim); font-family: inherit; font-size: 11px;
  padding: 2px 6px; line-height: 1;
}
.lo-title-bar button:hover { color: var(--lo-ink); }

/* ── Tab Navigation ───────────────────────────────────────────────────────── */
nav.lo-tabs {
  display: flex;
  gap: 0;
  border-bottom: 1px solid var(--lo-divider-strong);
  padding: 0;
}
.lo-tab-btn {
  background: none; border: none; border-bottom: 2px solid transparent;
  padding: 10px 16px 8px;
  font-family: inherit; font-size: 11px; font-weight: 500;
  letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--lo-dim); cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
.lo-tab-btn:hover { color: var(--lo-ink); }
.lo-tab-btn.active { color: var(--lo-ink); border-bottom-color: var(--lo-ink); }

/* ── Tab Panels ───────────────────────────────────────────────────────────── */
.lo-panel { display: none; padding: 0 0 24px; flex: 1; }
.lo-panel.active { display: block; }

/* ── Status Banner ────────────────────────────────────────────────────────── */
.lo-status {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 0;
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--lo-dim);
  border-bottom: 1px solid var(--lo-divider);
  flex-wrap: wrap;
}
.lo-pulse {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--lo-accent-2);
  animation: loPulse 2s ease-in-out infinite;
}
.lo-status .lo-sep { color: var(--lo-divider-strong); }
.lo-status .lo-right { margin-left: auto; }

/* ── Mesh Header SVG ──────────────────────────────────────────────────────── */
.lo-mesh-header {
  width: 100%;
  height: 72px;
  overflow: hidden;
  border-bottom: 1px solid var(--lo-divider);
}
.lo-mesh-header svg { width: 100%; height: 100%; }
.lo-no-peers {
  text-align: center;
  padding: 20px 0;
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--lo-dim);
  border-bottom: 1px solid var(--lo-divider);
}

/* ── Stat Strip ───────────────────────────────────────────────────────────── */
.lo-stats {
  display: flex;
  border-bottom: 1px solid var(--lo-divider-strong);
}
.lo-stat {
  flex: 1;
  padding: 18px 16px;
  border-right: 1px solid var(--lo-divider);
}
.lo-stat:last-child { border-right: none; }
.lo-stat-label {
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--lo-dim);
  margin-bottom: 2px;
}
.lo-stat-value {
  font-size: 22px;
  font-weight: 500;
  letter-spacing: -0.01em;
  color: var(--lo-ink);
}
.lo-stat-sub {
  font-size: 10px;
  color: var(--lo-faint);
  margin-top: 1px;
}

/* ── Mesh Map SVG ─────────────────────────────────────────────────────────── */
.lo-mesh-map {
  position: relative;
  width: 100%;
  height: 260px;
  border-bottom: 1px solid var(--lo-divider-strong);
  overflow: hidden;
}
.lo-mesh-map svg { width: 100%; height: 100%; }

/* ── Message Feed ─────────────────────────────────────────────────────────── */
.lo-feed-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 0 8px;
  flex-wrap: wrap;
}
.lo-feed-header .lo-label {
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--lo-dim);
  margin-right: 8px;
}
.lo-chip {
  background: none;
  border: 1px solid var(--lo-divider-strong);
  padding: 3px 10px;
  font-family: inherit;
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--lo-dim);
  cursor: pointer;
  transition: color 0.15s;
}
.lo-chip:hover { color: var(--lo-ink); }
.lo-chip.active {
  color: var(--lo-ink);
  border-bottom: 2px solid var(--lo-ink);
}
.lo-search {
  flex: 1;
  min-width: 120px;
  background: transparent;
  border: none;
  border-bottom: 1px solid var(--lo-divider-strong);
  font-family: inherit;
  font-size: 11px;
  color: var(--lo-ink);
  padding: 4px 0;
  outline: none;
}
.lo-search::placeholder { color: var(--lo-faint); }
.lo-search:focus { border-bottom-color: var(--lo-ink); }

.lo-feed {
  max-height: 400px;
  overflow-y: auto;
  border-bottom: 1px solid var(--lo-divider-strong);
}
.lo-msg {
  display: grid;
  grid-template-columns: 56px 14px 26px 1fr;
  gap: 6px;
  padding: 6px 0;
  border-bottom: 1px solid var(--lo-divider);
  align-items: baseline;
}
.lo-msg-time { color: var(--lo-faint); font-size: 11px; }
.lo-msg-arrow { text-align: center; font-size: 12px; }
.lo-msg-arrow.in { color: var(--lo-dim); }
.lo-msg-arrow.out { color: var(--lo-accent); }
.lo-msg-arrow.relay { color: var(--lo-accent-2); }
.lo-msg-badge {
  font-size: 9px; letter-spacing: 0.1em; text-transform: uppercase;
  padding: 1px 4px; border: 1px solid var(--lo-divider-strong);
  border-radius: 2px; color: var(--lo-dim); text-align: center;
}
.lo-msg-body { color: var(--lo-ink); word-break: break-word; }
.lo-msg-node { color: var(--lo-dim); }
.lo-feed-empty {
  padding: 24px 0;
  text-align: center;
  color: var(--lo-faint);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
}

/* ── Composer Bar ─────────────────────────────────────────────────────────── */
.lo-composer {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 0;
  border-bottom: 1px solid var(--lo-divider-strong);
}
.lo-composer .lo-prompt {
  color: var(--lo-accent);
  font-size: 14px;
  font-weight: 500;
  user-select: none;
}
.lo-composer input[type="text"] {
  flex: 1;
  background: transparent;
  border: none;
  font-family: inherit;
  font-size: 12px;
  color: var(--lo-ink);
  caret-color: var(--lo-accent);
  outline: none;
  padding: 4px 0;
}
.lo-composer input[type="text"]::placeholder { color: var(--lo-faint); }
.lo-composer .lo-send {
  background: var(--lo-ink);
  color: var(--lo-bg);
  border: none;
  padding: 5px 14px;
  font-family: inherit;
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  cursor: pointer;
  border-radius: 2px;
}
.lo-composer .lo-send:hover { opacity: 0.85; }
.lo-composer .lo-send:disabled { opacity: 0.4; cursor: default; }

/* ── System Log ───────────────────────────────────────────────────────────── */
.lo-log-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
  cursor: pointer;
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--lo-faint);
  border: none;
  background: none;
  font-family: inherit;
  width: 100%;
  text-align: left;
}
.lo-log-toggle:hover { color: var(--lo-dim); }
.lo-log-toggle .lo-chevron {
  transition: transform 0.2s;
  font-size: 8px;
}
.lo-log-toggle.open .lo-chevron { transform: rotate(90deg); }
.lo-log-ticker {
  font-size: 10px;
  color: var(--lo-faint);
  margin-left: auto;
  max-width: 60%;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.lo-log-viewer {
  display: none;
  max-height: 300px;
  overflow-y: auto;
  border: 1px solid var(--lo-divider);
  padding: 8px;
  margin-bottom: 8px;
}
.lo-log-viewer.open { display: block; }
.lo-log-line {
  padding: 2px 0;
  font-size: 11px;
  border-bottom: 1px solid var(--lo-divider);
  word-break: break-word;
}
.lo-log-line.log-WARNING { color: var(--lo-accent); }
.lo-log-line.log-ERROR { color: #c0392b; }
.lo-log-line.log-DEBUG { color: var(--lo-faint); }
.lo-log-line.log-INFO { color: var(--lo-dim); }
.lo-log-controls {
  display: flex;
  gap: 4px;
  margin-bottom: 6px;
}

/* ── CONFIG Sections ──────────────────────────────────────────────────────── */
.lo-section {
  border-bottom: 1px solid var(--lo-divider-strong);
}
.lo-section > summary, .lo-section-head {
  display: flex;
  align-items: center;
  padding: 16px 0;
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--lo-ink);
  cursor: pointer;
  list-style: none;
  user-select: none;
}
.lo-section > summary::-webkit-details-marker { display: none; }
.lo-section > summary::before, .lo-section-head::before {
  content: '\25B8';
  margin-right: 10px;
  font-size: 9px;
  transition: transform 0.2s;
  color: var(--lo-dim);
}
.lo-section[open] > summary::before { transform: rotate(90deg); }
.lo-section-body {
  padding: 0 0 20px;
}
.lo-form-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 0;
  flex-wrap: wrap;
}
.lo-form-label {
  width: 140px;
  flex-shrink: 0;
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--lo-dim);
}
.lo-form-control { flex: 1; min-width: 0; }
.lo-form-hint {
  font-size: 10px;
  color: var(--lo-faint);
  text-align: right;
  flex-shrink: 0;
}

/* ── Form Elements ────────────────────────────────────────────────────────── */
input[type="text"], input[type="number"], input[type="url"],
select, textarea {
  background: transparent;
  border: 1px solid var(--lo-divider-strong);
  border-radius: 2px;
  font-family: inherit;
  font-size: 12px;
  color: var(--lo-ink);
  padding: 6px 8px;
  outline: none;
  width: 100%;
}
input:focus, select:focus, textarea:focus {
  border-color: var(--lo-ink);
}
textarea { resize: vertical; min-height: 80px; }
input[type="range"] {
  -webkit-appearance: none;
  background: var(--lo-divider-strong);
  height: 2px;
  border-radius: 1px;
  outline: none;
}
input[type="range"]::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 12px; height: 12px;
  border-radius: 50%;
  background: var(--lo-ink);
  cursor: pointer;
}
input[type="checkbox"] {
  accent-color: var(--lo-accent-2);
}

.btn {
  background: transparent;
  border: 1px solid var(--lo-divider-strong);
  border-radius: 2px;
  font-family: inherit;
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--lo-dim);
  padding: 5px 12px;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
.btn:hover { color: var(--lo-ink); border-color: var(--lo-ink); }
.btn:disabled { opacity: 0.4; cursor: default; }
.btn-primary {
  background: var(--lo-ink);
  color: var(--lo-bg);
  border-color: var(--lo-ink);
}
.btn-primary:hover { opacity: 0.85; }
.btn-sm { padding: 3px 8px; font-size: 9px; }

/* ── Help Popover ─────────────────────────────────────────────────────────── */
.lo-help {
  display: none;
  position: fixed;
  top: 40px;
  right: 20px;
  width: 320px;
  background: var(--lo-bg-deep);
  border: 1px solid var(--lo-divider-strong);
  border-radius: 2px;
  padding: 16px;
  z-index: 1000;
  font-size: 11px;
  color: var(--lo-ink);
  line-height: 1.6;
}
.lo-help.open { display: block; }
.lo-help h4 {
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--lo-dim);
  margin-bottom: 10px;
}
.lo-help p { margin-bottom: 8px; }
.lo-help code {
  background: var(--lo-divider);
  padding: 1px 4px;
  border-radius: 2px;
  font-size: 11px;
}

/* ── Onboarding Modal ─────────────────────────────────────────────────────── */
.lo-onboarding {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.6);
  z-index: 2000;
  align-items: center;
  justify-content: center;
}
.lo-onboarding.open { display: flex; }
.lo-onboarding-box {
  background: var(--lo-bg);
  border: 1px solid var(--lo-divider-strong);
  border-radius: 2px;
  width: 520px;
  max-width: 90vw;
  padding: 24px;
}
.lo-ob-progress {
  display: flex;
  gap: 8px;
  justify-content: center;
  margin-bottom: 16px;
  font-size: 14px;
}
.lo-ob-dot { color: var(--lo-divider-strong); }
.lo-ob-dot.done { color: var(--lo-ink); }
.lo-ob-dot.active { color: var(--lo-accent); }
.lo-ob-step {
  display: none;
  text-align: center;
}
.lo-ob-step.active { display: block; }
.lo-ob-step svg {
  width: 100%;
  max-width: 360px;
  height: 120px;
  margin: 0 auto 16px;
}
.lo-ob-step h3 {
  font-size: 14px;
  font-weight: 500;
  color: var(--lo-ink);
  margin-bottom: 8px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.lo-ob-step p {
  font-size: 12px;
  color: var(--lo-dim);
  line-height: 1.7;
  max-width: 400px;
  margin: 0 auto;
}
.lo-ob-nav {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 20px;
  padding-top: 14px;
  border-top: 1px solid var(--lo-divider);
}
.lo-ob-nav .btn { min-width: 70px; }
.lo-ob-skip {
  background: none; border: none;
  font-family: inherit; font-size: 10px;
  letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--lo-faint); cursor: pointer;
}
.lo-ob-skip:hover { color: var(--lo-dim); }

/* ── View Toggle ──────────────────────────────────────────────────────────── */
.lo-view-toggle {
  display: inline-flex;
  border: 1px solid var(--lo-divider-strong);
  margin-left: 12px;
}
.lo-view-toggle button {
  padding: 3px 12px !important;
  font-size: 10px !important;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  border: none;
  background: none;
  color: var(--lo-dim);
  cursor: pointer;
  font-family: inherit;
  line-height: 1.4;
}
.lo-view-toggle button.active {
  background: var(--lo-ink);
  color: var(--lo-bg);
}

/* ── Messenger Layout ─────────────────────────────────────────────────────── */
.lo-messenger {
  display: none;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}
.lo-messenger.active { display: flex; }
.lo-msg-panes {
  display: flex;
  flex: 1;
  min-height: 0;
  height: calc(100vh - 180px);
}

/* ── Sidebar ──────────────────────────────────────────────────────────────── */
.lo-sidebar {
  width: 320px;
  flex-shrink: 0;
  border-right: 1px solid var(--lo-divider-strong);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.lo-sidebar-tabs {
  display: flex;
  border-bottom: 1px solid var(--lo-divider-strong);
  padding: 0;
}
.lo-sidebar-tabs button {
  flex: 1;
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  padding: 10px 8px 8px;
  font-family: inherit;
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--lo-dim);
  cursor: pointer;
}
.lo-sidebar-tabs button:hover { color: var(--lo-ink); }
.lo-sidebar-tabs button.active { color: var(--lo-ink); border-bottom-color: var(--lo-ink); }
.lo-sidebar-search {
  padding: 8px;
  border-bottom: 1px solid var(--lo-divider);
}
.lo-sidebar-search input {
  width: 100%;
  background: transparent;
  border: none;
  border-bottom: 1px solid var(--lo-divider-strong);
  font-family: inherit;
  font-size: 11px;
  color: var(--lo-ink);
  padding: 4px 0;
  outline: none;
}
.lo-sidebar-search input::placeholder { color: var(--lo-faint); }
.lo-contact-list {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}
.lo-contact {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  cursor: pointer;
  border-bottom: 1px solid var(--lo-divider);
}
.lo-contact:hover { background: var(--lo-bg-deep); }
.lo-contact.selected {
  background: var(--lo-bg-deep);
  border-left: 2px solid var(--lo-ink);
  padding-left: 10px;
}
.lo-avatar {
  width: 32px;
  height: 32px;
  border: 1px solid var(--lo-divider-strong);
  background: var(--lo-bg-deep);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--lo-ink);
  flex-shrink: 0;
  position: relative;
}
.lo-avatar .lo-proto {
  position: absolute;
  top: 2px;
  right: 2px;
  width: 7px;
  height: 7px;
}
.lo-avatar .lo-proto.mt { background: var(--lo-accent-2); }
.lo-avatar .lo-proto.mc { border: 1px solid var(--lo-accent-2); background: transparent; }
.lo-contact-info { flex: 1; min-width: 0; }
.lo-contact-name {
  font-size: 11px;
  color: var(--lo-ink);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.lo-contact-preview {
  font-size: 10px;
  color: var(--lo-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin-top: 2px;
}
.lo-contact-meta {
  flex-shrink: 0;
  text-align: right;
}
.lo-unread-badge {
  display: inline-block;
  background: var(--lo-accent);
  color: var(--lo-bg);
  font-size: 9px;
  font-weight: 500;
  letter-spacing: 0.05em;
  padding: 1px 5px;
  min-width: 16px;
  text-align: center;
  margin-bottom: 2px;
}
.lo-contact-time {
  font-size: 9px;
  color: var(--lo-faint);
}
.lo-sidebar-footer {
  padding: 8px 12px;
  border-top: 1px solid var(--lo-divider-strong);
  font-size: 9px;
  color: var(--lo-faint);
  letter-spacing: 0.05em;
  text-transform: uppercase;
  display: flex;
  justify-content: space-between;
}

/* ── Thread View ──────────────────────────────────────────────────────────── */
.lo-thread {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
.lo-thread-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--lo-dim);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
}
.lo-thread-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--lo-divider-strong);
  min-height: 44px;
  flex-shrink: 0;
}
.lo-thread-header .lo-avatar { width: 28px; height: 28px; font-size: 10px; }
.lo-thread-name { font-size: 12px; font-weight: 500; color: var(--lo-ink); }
.lo-thread-meta {
  font-size: 9px;
  color: var(--lo-dim);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  margin-left: auto;
}
.lo-ai-toggle {
  background: none;
  border: 1px solid var(--lo-divider-strong);
  font-family: inherit;
  font-size: 9px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--lo-dim);
  padding: 2px 8px;
  cursor: pointer;
  margin-left: 8px;
}
.lo-ai-toggle:hover { color: var(--lo-ink); border-color: var(--lo-ink); }
.lo-ai-toggle.on { color: var(--lo-accent); border-color: var(--lo-accent); }
.lo-thread-messages {
  flex: 1;
  overflow-y: auto;
  padding: 12px 16px;
  min-height: 0;
}
.lo-tmsg {
  display: grid;
  grid-template-columns: 52px 14px 1fr;
  gap: 6px;
  padding: 4px 0;
  align-items: baseline;
}
.lo-tmsg-time { color: var(--lo-faint); font-size: 10px; }
.lo-tmsg-arrow { text-align: center; font-size: 11px; }
.lo-tmsg-arrow.in { color: var(--lo-dim); }
.lo-tmsg-arrow.out { color: var(--lo-ink); }
.lo-tmsg-arrow.ai { color: var(--lo-accent); }
.lo-tmsg-body { color: var(--lo-ink); word-break: break-word; font-size: 12px; }
.lo-tmsg-ai-badge {
  display: inline-block;
  font-size: 8px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  padding: 0 4px;
  border: 1px solid var(--lo-accent);
  color: var(--lo-accent);
  margin-left: 6px;
  vertical-align: middle;
}
.lo-thread-composer {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  border-top: 1px solid var(--lo-divider-strong);
  flex-shrink: 0;
}
.lo-thread-composer .lo-prompt { color: var(--lo-accent); font-size: 14px; font-weight: 500; }
.lo-thread-composer input {
  flex: 1;
  background: transparent;
  border: none;
  font-family: inherit;
  font-size: 12px;
  color: var(--lo-ink);
  caret-color: var(--lo-accent);
  outline: none;
}
.lo-thread-composer input::placeholder { color: var(--lo-faint); }
.lo-char-count { font-size: 9px; color: var(--lo-faint); margin-right: 4px; }
.lo-char-count.warn { color: var(--lo-accent); }
.lo-char-count.over { color: var(--lo-dim); }

/* ── Toast ────────────────────────────────────────────────────────────────── */
#toast-container {
  position: fixed;
  bottom: 20px;
  right: 20px;
  z-index: 3000;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.toast {
  padding: 8px 14px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--lo-ink);
  background: var(--lo-bg-deep);
  border: 1px solid var(--lo-divider-strong);
  border-left: 3px solid var(--lo-accent-2);
  border-radius: 2px;
  max-width: 320px;
  word-break: break-word;
  transition: opacity 0.3s;
}
.toast-error { border-left-color: #c0392b; }
.toast-success { border-left-color: var(--lo-accent-2); }
.toast.fade-out { opacity: 0; }

/* ── Leaflet Overrides ────────────────────────────────────────────────────── */
.leaflet-container { background: var(--lo-bg-deep) !important; }
.lo-geo-map, .lo-cov-map { height: 360px; border: 1px solid var(--lo-divider-strong); }
.node-marker-wrap { background: none !important; border: none !important; }
.node-marker {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
}
.node-marker .core {
  width: 10px; height: 10px;
  border-radius: 50%;
  background: var(--lo-accent-2);
}
.node-marker .ring {
  position: absolute;
  width: 18px; height: 18px;
  border-radius: 50%;
  border: 1px solid var(--lo-accent-2);
  opacity: 0.4;
  animation: loNodePulse 3s ease-in-out infinite;
}
.node-marker .label {
  position: absolute;
  top: 18px;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: 9px;
  color: var(--lo-ink);
  letter-spacing: 0.05em;
}
.node-marker.stale .core { background: var(--lo-faint); }
.node-marker.stale .ring { border-color: var(--lo-faint); animation: none; }

/* ── Animations ───────────────────────────────────────────────────────────── */
@keyframes loPulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
@keyframes loSonar {
  0% { r: 5; opacity: 0.9; }
  100% { r: 26; opacity: 0; }
}
@keyframes loBreath {
  0%, 100% { r: 3; opacity: 0.9; }
  50% { r: 4.2; opacity: 1; }
}
@keyframes loNodePulse {
  0%, 100% { transform: scale(1); opacity: 0.4; }
  50% { transform: scale(1.3); opacity: 0.1; }
}
@keyframes loPacket {
  0% { stroke-dashoffset: 56; }
  100% { stroke-dashoffset: 0; }
}

/* ── Reduced Motion ───────────────────────────────────────────────────────── */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}

/* ── Responsive ───────────────────────────────────────────────────────────── */
@media (max-width: 640px) {
  .lo-stats { flex-wrap: wrap; }
  .lo-stat { min-width: 50%; border-bottom: 1px solid var(--lo-divider); }
  .lo-form-row { flex-direction: column; align-items: stretch; }
  .lo-form-label { width: auto; }
  .lo-mesh-map { height: 180px; }
  .lo-onboarding-box { padding: 16px; }
  .lo-ob-step svg { height: 80px; }
}
</style>
</head>
<body>
<div class="lo-shell">

<!-- ── Title Bar ─────────────────────────────────────────────────────────── -->
<header class="lo-title-bar">
  <span class="lo-brand"><span class="lo-accent">LORACLE</span> BRIDGE</span>
  <span class="lo-conn-dot" id="hdr-conn-dot"></span>
  <span class="lo-conn-label" id="hdr-conn-label">DISCONNECTED</span>
  <span class="lo-view-toggle">
    <button class="active" data-view="messenger" onclick="switchView('messenger')">MESSENGER</button>
    <button data-view="dashboard" onclick="switchView('dashboard')">DASHBOARD</button>
  </span>
  <span class="lo-clock" id="hdr-clock" style="margin-left:auto">--:--:--</span>
  <button id="help-toggle" title="Help">?</button>
  <button id="theme-toggle" title="Toggle theme">&#9681;</button>
  <button onclick="switchView('config')" title="Settings" style="font-size:13px">&#9881;</button>
</header>

<!-- ══════════════════════════════════════════════════════════════════════════
     MESSENGER VIEW
     ══════════════════════════════════════════════════════════════════════ -->
<div class="lo-messenger active" id="view-messenger">
  <!-- Reuse mesh header -->
  <div class="lo-mesh-header" id="msg-mesh-header">
    <svg viewBox="0 0 700 68" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">
      <defs><style>
        .mh-link { stroke: var(--lo-accent-2); stroke-width: 1; fill: none; opacity: 0.5; }
        .mh-mynode { fill: var(--lo-accent); }
        .mh-peer { fill: var(--lo-accent-2); }
        .mh-packet { stroke: var(--lo-accent-2); stroke-width: 1.5; stroke-dasharray: 4 10; fill: none; }
        .mh-packet-out { stroke: var(--lo-accent); }
      </style></defs>
      <g id="mh-links2"></g>
      <circle cx="350" cy="34" r="5" fill="none" stroke="var(--lo-accent)" stroke-width="1" opacity="0.9">
        <animate attributeName="r" values="5;26" dur="2.8s" repeatCount="indefinite"/>
        <animate attributeName="opacity" values="0.9;0" dur="2.8s" repeatCount="indefinite"/>
      </circle>
      <circle cx="350" cy="34" r="5" class="mh-mynode"/>
      <g id="mh-peers2"></g>
    </svg>
  </div>

  <div class="lo-msg-panes">
    <!-- Sidebar -->
    <div class="lo-sidebar" id="messenger-sidebar">
      <div class="lo-sidebar-tabs">
        <button class="active" data-stab="dm" onclick="setSidebarTab('dm',this)">DMs</button>
        <button data-stab="channel" onclick="setSidebarTab('channel',this)">Channels</button>
        <button data-stab="all" onclick="setSidebarTab('all',this)">All</button>
      </div>
      <div class="lo-sidebar-search">
        <input type="text" id="sidebar-search" placeholder="search contacts..." oninput="filterSidebar(this.value)">
      </div>
      <div class="lo-contact-list" id="contact-list">
        <div style="padding:20px;text-align:center;color:var(--lo-faint);font-size:10px;letter-spacing:0.1em;text-transform:uppercase">
          NO CONVERSATIONS YET
        </div>
      </div>
      <div class="lo-sidebar-footer">
        <span id="sidebar-radios"></span>
        <span id="sidebar-unread"></span>
      </div>
    </div>

    <!-- Thread View -->
    <div class="lo-thread" id="thread-view">
      <div class="lo-thread-empty" id="thread-empty">
        SELECT A CONVERSATION
      </div>
      <div id="thread-active" style="display:none;flex:1;display:none;flex-direction:column;min-height:0">
        <div class="lo-thread-header" id="thread-header">
          <div class="lo-avatar" id="thread-avatar"></div>
          <span class="lo-thread-name" id="thread-name"></span>
          <span class="lo-thread-meta" id="thread-meta"></span>
          <button class="lo-ai-toggle" id="thread-ai-toggle" onclick="toggleThreadAi()">AI: --</button>
        </div>
        <div class="lo-thread-messages" id="thread-messages"></div>
        <div class="lo-thread-composer">
          <span class="lo-prompt">&gt;</span>
          <input type="text" id="thread-input" placeholder="type a message..." onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();threadSend()}" oninput="updateCharCount()">
          <span class="lo-char-count" id="thread-char-count"></span>
          <button class="lo-send" id="thread-send-btn" onclick="threadSend()">SEND</button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ══════════════════════════════════════════════════════════════════════════
     DASHBOARD VIEW (was LIVE TAB)
     ══════════════════════════════════════════════════════════════════════ -->
<section id="tab-live" class="lo-panel">

  <!-- Status Banner -->
  <div class="lo-status">
    <span class="lo-pulse" id="status-pulse"></span>
    <span id="status-conn">LISTENING</span>
    <span class="lo-sep">&#183;</span>
    <span id="status-model">--</span>
    <span class="lo-sep">&#183;</span>
    <span id="status-rag">rag: --</span>
    <span class="lo-right" id="status-uptime">0s</span>
  </div>

  <!-- Mesh Header SVG -->
  <div id="mesh-header-container">
    <div class="lo-mesh-header" id="mesh-header">
      <svg id="mesh-header-svg" viewBox="0 0 700 68" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <style>
            .mh-link { stroke: var(--lo-accent-2); stroke-width: 1; fill: none; opacity: 0.5; }
            .mh-mynode { fill: var(--lo-accent); }
            .mh-peer { fill: var(--lo-accent-2); }
            .mh-packet { stroke: var(--lo-accent-2); stroke-width: 1.5; stroke-dasharray: 4 10; fill: none; }
            .mh-packet-out { stroke: var(--lo-accent); }
          </style>
        </defs>
        <!-- Links drawn by JS -->
        <g id="mh-links"></g>
        <!-- Sonar pulse on my node -->
        <circle cx="350" cy="34" r="5" fill="none" stroke="var(--lo-accent)" stroke-width="1" opacity="0.9">
          <animate attributeName="r" values="5;26" dur="2.8s" repeatCount="indefinite"/>
          <animate attributeName="opacity" values="0.9;0" dur="2.8s" repeatCount="indefinite"/>
        </circle>
        <!-- My node -->
        <circle cx="350" cy="34" r="5" class="mh-mynode"/>
        <!-- Peer nodes drawn by JS -->
        <g id="mh-peers"></g>
      </svg>
    </div>
    <div class="lo-no-peers" id="no-peers" style="display:none">LISTENING &middot; NO PEERS IN RANGE</div>
  </div>

  <!-- Stat Strip -->
  <div class="lo-stats">
    <div class="lo-stat">
      <div class="lo-stat-label">Messages</div>
      <div class="lo-stat-value" id="stat-msgs">0</div>
      <div class="lo-stat-sub" id="stat-msgs-sub">&nbsp;</div>
    </div>
    <div class="lo-stat">
      <div class="lo-stat-label">Nodes</div>
      <div class="lo-stat-value" id="stat-nodes">0</div>
      <div class="lo-stat-sub" id="stat-nodes-sub">&nbsp;</div>
    </div>
    <div class="lo-stat">
      <div class="lo-stat-label">Reply</div>
      <div class="lo-stat-value" id="stat-reply">--</div>
      <div class="lo-stat-sub" id="stat-reply-sub">&nbsp;</div>
    </div>
    <div class="lo-stat">
      <div class="lo-stat-label">Radios</div>
      <div class="lo-stat-value" id="stat-radios">1</div>
      <div class="lo-stat-sub" id="stat-radios-sub">MT</div>
    </div>
  </div>

  <!-- Mesh Map SVG -->
  <div class="lo-mesh-map">
    <svg id="mesh-map-svg" viewBox="0 0 600 260" xmlns="http://www.w3.org/2000/svg">
      <!-- Background contour curves -->
      <path d="M0,65 Q150,45 300,65 T600,65" stroke="var(--lo-divider-strong)" stroke-width="0.5" fill="none" opacity="0.5"/>
      <path d="M0,110 Q150,130 300,110 T600,110" stroke="var(--lo-divider-strong)" stroke-width="0.5" fill="none" opacity="0.4"/>
      <path d="M0,160 Q150,140 300,160 T600,160" stroke="var(--lo-divider-strong)" stroke-width="0.5" fill="none" opacity="0.3"/>
      <path d="M0,200 Q150,220 300,200 T600,200" stroke="var(--lo-divider-strong)" stroke-width="0.5" fill="none" opacity="0.25"/>
      <!-- Crosshair -->
      <line x1="300" y1="0" x2="300" y2="260" stroke="var(--lo-divider-strong)" stroke-width="0.5" stroke-dasharray="4,6" opacity="0.4"/>
      <line x1="0" y1="130" x2="600" y2="130" stroke="var(--lo-divider-strong)" stroke-width="0.5" stroke-dasharray="4,6" opacity="0.4"/>
      <!-- Corner labels -->
      <text x="8" y="16" fill="var(--lo-faint)" font-family="var(--font-mono)" font-size="8" letter-spacing="0.1em" id="mm-corner-nw">N -- W --</text>
      <text x="592" y="254" fill="var(--lo-faint)" font-family="var(--font-mono)" font-size="8" letter-spacing="0.1em" text-anchor="end" id="mm-corner-se">S -- E --</text>
      <!-- My node center -->
      <circle cx="300" cy="130" r="5" fill="none" stroke="var(--lo-accent)" stroke-width="1" opacity="0.7">
        <animate attributeName="r" values="5;20" dur="2.8s" repeatCount="indefinite"/>
        <animate attributeName="opacity" values="0.7;0" dur="2.8s" repeatCount="indefinite"/>
      </circle>
      <circle cx="300" cy="130" r="5" fill="var(--lo-accent)"/>
      <text x="300" y="118" fill="var(--lo-accent)" font-family="var(--font-mono)" font-size="8" text-anchor="middle" letter-spacing="0.1em">MY NODE</text>
      <!-- Peer nodes drawn by JS -->
      <g id="mm-links"></g>
      <g id="mm-nodes"></g>
    </svg>
  </div>

  <!-- Geographic Node Map -->
  <div style="border-bottom:1px solid var(--lo-divider-strong)">
    <div style="display:flex;align-items:center;padding:10px 0 6px;font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:var(--lo-dim)">
      NODE MAP <span style="margin-left:auto" id="geo-node-count"></span>
    </div>
    <div id="geo-map" class="lo-geo-map"></div>
  </div>

  <!-- Node List (scrollable) -->
  <div style="border-bottom:1px solid var(--lo-divider-strong);padding:14px 0">
    <div style="display:flex;align-items:center;margin-bottom:8px;font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:var(--lo-dim)">
      NODES <span style="margin-left:auto" id="node-list-count"></span>
    </div>
    <div id="node-list" style="max-height:180px;overflow-y:auto;font-size:11px">
      <span style="color:var(--lo-faint)">Waiting for nodes...</span>
    </div>
  </div>

  <!-- Message Feed -->
  <div class="lo-feed-header">
    <span class="lo-label">Messages</span>
    <button class="lo-chip active" data-filter="all" onclick="setFilter('all',this)">ALL</button>
    <button class="lo-chip" data-filter="in" onclick="setFilter('in',this)">IN</button>
    <button class="lo-chip" data-filter="out" onclick="setFilter('out',this)">OUT</button>
    <input type="text" class="lo-search" id="msg-search" placeholder="Filter..." oninput="App.messageSearch=this.value">
  </div>
  <div class="lo-feed" id="msg-feed">
    <div class="lo-feed-empty">WAITING FOR MESSAGES</div>
  </div>

  <!-- Composer Bar -->
  <div class="lo-composer" id="composer-bar">
    <span class="lo-prompt">&gt;</span>
    <input type="text" id="composer-input" placeholder="Type a message..." onkeydown="if(event.key==='Enter')composerSend()">
    <button class="lo-send" id="composer-send" onclick="composerSend()">SEND</button>
  </div>

  <!-- System Log -->
  <div>
    <button class="lo-log-toggle" id="log-toggle" onclick="toggleLog()">
      <span class="lo-chevron">&#9656;</span>
      SYSTEM LOG
      <span class="lo-log-ticker" id="log-ticker"></span>
    </button>
    <div class="lo-log-viewer" id="log-viewer">
      <div class="lo-log-controls">
        <button class="lo-chip active" data-filter="all" onclick="setLogFilter('all',this)">ALL</button>
        <button class="lo-chip" data-filter="INFO" onclick="setLogFilter('INFO',this)">INFO</button>
        <button class="lo-chip" data-filter="WARNING" onclick="setLogFilter('WARNING',this)">WARN</button>
        <button class="lo-chip" data-filter="ERROR" onclick="setLogFilter('ERROR',this)">ERROR</button>
      </div>
      <div id="log-content"></div>
    </div>
  </div>

  <!-- Coverage (collapsed) -->
  <details class="lo-section" id="live-coverage">
    <summary class="lo-section-head">COVERAGE HEATMAP <span style="margin-left:auto;font-size:10px" id="cov-stats"></span></summary>
    <div class="lo-section-body">
      <div id="cov-banner" style="display:none;margin-bottom:8px;padding:6px 10px;border:1px solid var(--lo-accent);font-size:10px;color:var(--lo-accent);letter-spacing:0.05em">
        <span id="cov-banner-text">Bridge disconnected</span>
      </div>
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:8px;font-size:10px">
        <label style="display:flex;align-items:center;gap:4px;color:var(--lo-dim)">
          Mode:
          <select id="cov-mode" onchange="renderCoverage()" style="width:80px">
            <option value="grid">Grid</option>
            <option value="heat" selected>Heatmap</option>
            <option value="both">Both</option>
          </select>
        </label>
        <label style="display:flex;align-items:center;gap:4px;color:var(--lo-dim)">
          Window:
          <select id="cov-window" onchange="renderCoverage()" style="width:100px">
            <option value="3600">Last hour</option>
            <option value="21600">Last 6h</option>
            <option value="86400" selected>Last 24h</option>
            <option value="0">All time</option>
          </select>
        </label>
        <label style="display:flex;align-items:center;gap:4px;color:var(--lo-dim)">
          Min RSSI:
          <input type="range" id="cov-rssi" min="-130" max="-30" value="-130" oninput="document.getElementById('cov-rssi-val').textContent=this.value+' dBm';renderCoverage()" style="width:80px">
          <span id="cov-rssi-val" style="color:var(--lo-faint)">-130 dBm</span>
        </label>
        <label style="display:flex;align-items:center;gap:4px;color:var(--lo-dim)">
          <input type="checkbox" id="cov-deadzones" onchange="renderCoverage()"> Dead zones
        </label>
        <button class="btn btn-sm" onclick="loadCoverage()">REFRESH</button>
        <button class="btn btn-sm" onclick="clearCoverage()" style="color:#c0392b;border-color:#c0392b">CLEAR LOG</button>
      </div>
      <div id="cov-map" class="lo-cov-map"></div>
    </div>
  </details>

</section>

<!-- ══════════════════════════════════════════════════════════════════════════
     CONFIG TAB
     ══════════════════════════════════════════════════════════════════════ -->
<section id="tab-config" class="lo-panel">

  <!-- Radios -->
  <details class="lo-section" open>
    <summary class="lo-section-head">RADIOS</summary>
    <div class="lo-section-body" id="cfg-radios-body">
      <div id="cfg-radios-list" style="margin-bottom:8px">
        <span style="color:var(--lo-faint);font-size:10px">Loading...</span>
      </div>
    </div>
  </details>

  <!-- AI Replies -->
  <details class="lo-section">
    <summary class="lo-section-head">AI REPLIES</summary>
    <div class="lo-section-body">
      <div class="lo-form-row">
        <span class="lo-form-label">AUTO-REPLY</span>
        <label style="display:flex;align-items:center;gap:6px">
          <input type="checkbox" id="cfg-ai-replies" checked onchange="cfgToggleAiReplies(this.checked)"> Enabled
        </label>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label"></span>
        <span style="font-size:10px;color:var(--lo-faint)">When on, LORACLE answers incoming messages. When off, it logs but stays quiet.</span>
      </div>
    </div>
  </details>

  <!-- Connection -->
  <details class="lo-section">
    <summary class="lo-section-head">CONNECTION</summary>
    <div class="lo-section-body">
      <div class="lo-form-row">
        <span class="lo-form-label">STATUS</span>
        <span class="lo-form-control" style="display:flex;align-items:center;gap:6px">
          <span class="lo-pulse" id="conn-mgr-dot" style="animation:none;background:var(--lo-faint)"></span>
          <span id="conn-mgr-status">Disconnected</span>
        </span>
        <span class="lo-form-hint" id="conn-mgr-detail"></span>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label">TYPE</span>
        <select id="conn-type-select" class="lo-form-control" onchange="connTypeChanged()" style="max-width:160px">
          <option value="serial">Serial (USB)</option>
          <option value="tcp">TCP</option>
          <option value="ble">BLE</option>
        </select>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label">ADDRESS</span>
        <input type="text" id="conn-address-input" class="lo-form-control" placeholder="auto-detect (or /dev/...)">
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label"></span>
        <div style="display:flex;gap:6px">
          <button class="btn btn-primary" onclick="manualConnect()">CONNECT</button>
          <button class="btn" id="conn-disconnect-btn" onclick="disconnectRadio()" style="display:none">DISCONNECT</button>
        </div>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label">BLE SCAN</span>
        <div class="lo-form-control">
          <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
            <button class="btn btn-sm" id="ble-scan-btn" onclick="bleScan()">SCAN</button>
            <span id="ble-scan-status" style="font-size:10px;color:var(--lo-dim)"></span>
          </div>
          <div id="ble-unavailable" style="display:none;font-size:10px;color:var(--lo-faint)">BLE not available</div>
          <div id="ble-last-device" style="display:none;margin-bottom:6px">
            <button class="btn btn-sm" onclick="bleQuickConnect()">RECONNECT <span id="ble-last-name"></span></button>
          </div>
          <div id="ble-device-list"></div>
        </div>
      </div>
    </div>
  </details>

  <!-- Model -->
  <details class="lo-section">
    <summary class="lo-section-head">MODEL</summary>
    <div class="lo-section-body">
      <div class="lo-form-row">
        <span class="lo-form-label">CURRENT</span>
        <span id="cfg-current-model" style="color:var(--lo-ink)">--</span>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label">SWITCH TO</span>
        <select id="cfg-model-select" class="lo-form-control" style="max-width:240px"></select>
        <button class="btn btn-sm" onclick="cfgSwitchModel()">APPLY</button>
        <button class="btn btn-sm" onclick="cfgRefreshModels()">REFRESH LIST</button>
      </div>
    </div>
  </details>

  <!-- Model Routing -->
  <details class="lo-section">
    <summary class="lo-section-head">MODEL ROUTING</summary>
    <div class="lo-section-body">
      <div class="lo-form-row">
        <span class="lo-form-label">AUTO-ROUTING</span>
        <label style="display:flex;align-items:center;gap:6px">
          <input type="checkbox" id="cfg-routing-auto" checked onchange="cfgSetRouting('auto', this.checked)"> Enabled
        </label>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label"></span>
        <span style="font-size:10px;color:var(--lo-faint)">LORACLE picks tiny/standard/big per query. Off = uses standard for everything.</span>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label">SHOW TIER TAG</span>
        <label style="display:flex;align-items:center;gap:6px">
          <input type="checkbox" id="cfg-routing-tag" checked onchange="cfgSetRouting('tag', this.checked)"> Show [TINY]/[STD]/[BIG] on AI messages
        </label>
      </div>
      <div style="margin-top:12px;border-top:1px solid var(--lo-divider);padding-top:12px">
        <div class="lo-form-row">
          <span class="lo-form-label">TIER: TINY</span>
          <input type="text" id="cfg-tier-tiny-model" value="gemma3:4b" style="max-width:160px">
          <label style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="cfg-tier-tiny-enabled" checked> On</label>
        </div>
        <div class="lo-form-row">
          <span class="lo-form-label">TIER: STANDARD</span>
          <input type="text" id="cfg-tier-std-model" value="qwen3:8b" style="max-width:160px">
          <label style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="cfg-tier-std-enabled" checked> On</label>
        </div>
        <div class="lo-form-row">
          <span class="lo-form-label">TIER: BIG</span>
          <input type="text" id="cfg-tier-big-model" value="phi4:14b" style="max-width:160px">
          <label style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="cfg-tier-big-enabled"> On</label>
        </div>
        <div class="lo-form-row">
          <span class="lo-form-label"></span>
          <button class="btn btn-sm" onclick="cfgSaveTiers()">SAVE TIERS</button>
        </div>
      </div>
      <div style="margin-top:12px;border-top:1px solid var(--lo-divider);padding-top:12px">
        <div class="lo-form-row">
          <span class="lo-form-label">TEST CLASSIFIER</span>
          <div class="lo-form-control">
            <input type="text" id="cfg-classifier-test" placeholder="type a query to see which tier..." oninput="testClassifier(this.value)">
            <div id="cfg-classifier-result" style="font-size:10px;color:var(--lo-dim);margin-top:4px"></div>
          </div>
        </div>
      </div>
    </div>
  </details>

  <!-- Response -->
  <details class="lo-section">
    <summary class="lo-section-head">RESPONSE</summary>
    <div class="lo-section-body">
      <div class="lo-form-row">
        <span class="lo-form-label">MAX LENGTH</span>
        <input type="range" id="cfg-max-len" min="50" max="1000" value="200" oninput="document.getElementById('cfg-max-len-val').textContent=this.value" style="flex:1">
        <span class="lo-form-hint" id="cfg-max-len-val">200</span>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label">COMPRESSION</span>
        <label style="display:flex;align-items:center;gap:6px">
          <input type="checkbox" id="cfg-compression"> Enabled
        </label>
      </div>
      <div class="lo-form-row" style="align-items:flex-start">
        <span class="lo-form-label">SYSTEM PROMPT</span>
        <div class="lo-form-control">
          <textarea id="cfg-prompt" rows="4"></textarea>
          <div style="display:flex;justify-content:space-between;margin-top:4px">
            <span style="font-size:10px;color:var(--lo-faint)" id="cfg-prompt-count"></span>
            <button class="btn btn-sm" onclick="cfgSavePrompt()">SAVE PROMPT</button>
          </div>
        </div>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label"></span>
        <button class="btn" onclick="cfgApplySettings()">APPLY SETTINGS</button>
      </div>
    </div>
  </details>

  <!-- Knowledge Base -->
  <details class="lo-section" id="cfg-rag-section">
    <summary class="lo-section-head">KNOWLEDGE BASE <span style="margin-left:auto;font-size:10px" id="cfg-rag-stats"></span></summary>
    <div class="lo-section-body">
      <div class="lo-form-row">
        <span class="lo-form-label">RAG</span>
        <label style="display:flex;align-items:center;gap:6px">
          <input type="checkbox" id="cfg-rag-toggle" onchange="cfgToggleRag(this.checked)"> Enabled
        </label>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label">ADD URL</span>
        <div class="lo-form-control">
          <div style="display:flex;gap:4px">
            <input type="url" id="cfg-url-input" placeholder="https://example.com/article">
            <button class="btn btn-sm" onclick="cfgIngestUrl()">INGEST</button>
          </div>
          <div id="cfg-url-status" style="font-size:10px;margin-top:4px"></div>
        </div>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label">UPLOAD FILE</span>
        <div class="lo-form-control">
          <input type="file" id="cfg-file-upload" onchange="cfgUploadFile()" style="font-size:11px">
        </div>
      </div>
      <div class="lo-form-row" style="align-items:flex-start">
        <span class="lo-form-label">DOCUMENTS</span>
        <div class="lo-form-control" id="cfg-rag-docs">
          <span style="color:var(--lo-faint)">Loading...</span>
        </div>
      </div>
    </div>
  </details>

  <!-- Knowledge Packs -->
  <details class="lo-section" id="cfg-packs-section">
    <summary class="lo-section-head">KNOWLEDGE PACKS</summary>
    <div class="lo-section-body">
      <div id="cfg-packs-list" style="margin-bottom:12px">
        <span style="color:var(--lo-faint);font-size:10px">Loading packs...</span>
      </div>
      <div id="cfg-pack-detail" style="display:none;margin-top:12px;padding:12px 0;border-top:1px solid var(--lo-divider)">
        <div id="cfg-pack-detail-content"></div>
      </div>
    </div>
  </details>

  <!-- Data & Storage -->
  <details class="lo-section">
    <summary class="lo-section-head">DATA & STORAGE</summary>
    <div class="lo-section-body">
      <div class="lo-form-row">
        <span class="lo-form-label">DATABASE</span>
        <span style="color:var(--lo-dim);font-size:10px">~/.mesh-llm/loracle.db</span>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label">STATS</span>
        <span id="cfg-db-stats" style="color:var(--lo-dim);font-size:10px">Loading...</span>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label">RETENTION</span>
        <span style="color:var(--lo-faint);font-size:10px">Last 500 messages OR 90 days per contact</span>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label"></span>
        <div style="display:flex;gap:6px">
          <button class="btn btn-sm" onclick="cfgPruneNow()">PRUNE NOW</button>
          <button class="btn btn-sm" onclick="cfgClearAllMessages()" style="color:#c0392b;border-color:#c0392b">CLEAR ALL MESSAGES</button>
        </div>
      </div>
    </div>
  </details>

  <!-- Appearance -->
  <details class="lo-section">
    <summary class="lo-section-head">APPEARANCE</summary>
    <div class="lo-section-body">
      <div class="lo-form-row">
        <span class="lo-form-label">THEME</span>
        <select id="cfg-theme" onchange="setTheme(this.value)" style="max-width:120px">
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label">ONBOARDING</span>
        <button class="btn btn-sm" onclick="showOnboarding()">LAUNCH TOUR</button>
      </div>
    </div>
  </details>

  <!-- About -->
  <details class="lo-section">
    <summary class="lo-section-head">ABOUT</summary>
    <div class="lo-section-body">
      <div class="lo-form-row">
        <span class="lo-form-label">VERSION</span>
        <span style="color:var(--lo-ink)">LORACLE Bridge v1.0</span>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label">UPTIME</span>
        <span id="cfg-uptime" style="color:var(--lo-ink)">--</span>
      </div>
    </div>
  </details>

  <!-- ADDON_SECTIONS -->

</section>

</div><!-- .lo-shell -->

<!-- ── Help Popover ──────────────────────────────────────────────────────── -->
<div class="lo-help" id="help-popover">
  <h4>QUICK REFERENCE</h4>
  <p><strong>No device found?</strong> Try a different USB cable or port.</p>
  <p><strong>Ollama not responding?</strong> Run <code>ollama serve</code> in a terminal.</p>
  <p><strong>Slow responses?</strong> Switch to a smaller model in CONFIG.</p>
  <p><strong>Response cut off?</strong> Send <code>!more</code> to get the rest.</p>
  <p><strong>Test without radio?</strong> Use the composer bar to chat directly with the LLM.</p>
</div>

<!-- ── Onboarding Modal ──────────────────────────────────────────────────── -->
<div class="lo-onboarding" id="onboarding">
  <div class="lo-onboarding-box">
    <div class="lo-ob-progress" id="ob-progress"></div>

    <div class="lo-ob-step active" data-step="0">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <rect x="140" y="40" width="80" height="40" rx="2" fill="none" stroke="var(--lo-ink)" stroke-width="1"/>
        <text x="180" y="64" text-anchor="middle" fill="var(--lo-ink)" font-family="var(--font-mono)" font-size="8">YOUR LAPTOP</text>
        <circle cx="180" cy="100" r="4" fill="var(--lo-accent)"/>
        <circle cx="180" cy="100" r="4" fill="none" stroke="var(--lo-accent)" stroke-width="1" opacity="0.7">
          <animate attributeName="r" values="4;16" dur="2.8s" repeatCount="indefinite"/>
          <animate attributeName="opacity" values="0.7;0" dur="2.8s" repeatCount="indefinite"/>
        </circle>
        <text x="180" y="116" text-anchor="middle" fill="var(--lo-accent)" font-family="var(--font-mono)" font-size="7">RADIO</text>
        <circle cx="60" cy="40" r="3" fill="var(--lo-accent-2)"><animate attributeName="r" values="3;4" dur="3.2s" repeatCount="indefinite"/></circle>
        <circle cx="300" cy="30" r="3" fill="var(--lo-accent-2)"><animate attributeName="r" values="3;4" dur="3.2s" begin="0.6s" repeatCount="indefinite"/></circle>
        <circle cx="40" cy="100" r="3" fill="var(--lo-accent-2)"><animate attributeName="r" values="3;4" dur="3.2s" begin="1.2s" repeatCount="indefinite"/></circle>
        <circle cx="320" cy="90" r="3" fill="var(--lo-accent-2)"><animate attributeName="r" values="3;4" dur="3.2s" begin="1.8s" repeatCount="indefinite"/></circle>
      </svg>
      <h3>WHAT IT IS</h3>
      <p>LORACLE turns your laptop into an AI that answers over radio. No internet required.</p>
    </div>

    <div class="lo-ob-step" data-step="1">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <rect x="150" y="45" width="60" height="30" rx="2" fill="none" stroke="var(--lo-ink)" stroke-width="1"/>
        <text x="180" y="64" text-anchor="middle" fill="var(--lo-ink)" font-family="var(--font-mono)" font-size="7">RADIO</text>
        <line x1="140" y1="60" x2="60" y2="60" stroke="var(--lo-accent-2)" stroke-width="1" stroke-dasharray="6,4" opacity="0.6"/>
        <line x1="220" y1="60" x2="300" y2="60" stroke="var(--lo-accent-2)" stroke-width="1" stroke-dasharray="6,4" opacity="0.6"/>
        <line x1="180" y1="40" x2="180" y2="15" stroke="var(--lo-accent-2)" stroke-width="1" stroke-dasharray="6,4" opacity="0.6"/>
        <line x1="180" y1="80" x2="180" y2="105" stroke="var(--lo-accent-2)" stroke-width="1" stroke-dasharray="6,4" opacity="0.6"/>
      </svg>
      <h3>RADIOS LISTEN</h3>
      <p>Your radio listens on LoRa. No tower, no bill. LORACLE works with both Meshtastic and MeshCore radios.</p>
    </div>

    <div class="lo-ob-step" data-step="2">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <circle cx="60" cy="60" r="4" fill="var(--lo-accent-2)"/>
        <circle cx="150" cy="60" r="4" fill="var(--lo-accent-2)"/>
        <circle cx="240" cy="60" r="4" fill="var(--lo-accent-2)"/>
        <circle cx="310" cy="60" r="4" fill="var(--lo-accent)"/>
        <line x1="64" y1="60" x2="146" y2="60" stroke="var(--lo-accent-2)" stroke-width="1"/>
        <line x1="154" y1="60" x2="236" y2="60" stroke="var(--lo-accent-2)" stroke-width="1"/>
        <line x1="244" y1="60" x2="306" y2="60" stroke="var(--lo-accent)" stroke-width="1"/>
        <circle cx="100" cy="60" r="3" fill="var(--lo-accent-2)" opacity="0.8">
          <animate attributeName="cx" values="64;146" dur="1.5s" repeatCount="indefinite"/>
          <animate attributeName="opacity" values="0.8;0.3" dur="1.5s" repeatCount="indefinite"/>
        </circle>
        <text x="60" y="80" text-anchor="middle" fill="var(--lo-faint)" font-family="var(--font-mono)" font-size="7">HOP 1</text>
        <text x="150" y="80" text-anchor="middle" fill="var(--lo-faint)" font-family="var(--font-mono)" font-size="7">HOP 2</text>
        <text x="240" y="80" text-anchor="middle" fill="var(--lo-faint)" font-family="var(--font-mono)" font-size="7">HOP 3</text>
        <text x="310" y="80" text-anchor="middle" fill="var(--lo-accent)" font-family="var(--font-mono)" font-size="7">MY NODE</text>
      </svg>
      <h3>MESSAGES HOP</h3>
      <p>Messages hop through the mesh until they reach you. Each node relays to the next.</p>
    </div>

    <div class="lo-ob-step" data-step="3">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <rect x="30" y="35" width="90" height="50" rx="2" fill="none" stroke="var(--lo-accent-2)" stroke-width="1"/>
        <text x="75" y="55" text-anchor="middle" fill="var(--lo-accent-2)" font-family="var(--font-mono)" font-size="7">HIKER</text>
        <text x="75" y="68" text-anchor="middle" fill="var(--lo-faint)" font-family="var(--font-mono)" font-size="6">what plants are safe?</text>
        <line x1="125" y1="60" x2="235" y2="60" stroke="var(--lo-accent-2)" stroke-width="1" stroke-dasharray="4,6"/>
        <text x="180" y="52" text-anchor="middle" fill="var(--lo-faint)" font-family="var(--font-mono)" font-size="6">LORA</text>
        <rect x="240" y="35" width="90" height="50" rx="2" fill="none" stroke="var(--lo-accent)" stroke-width="1"/>
        <text x="285" y="55" text-anchor="middle" fill="var(--lo-accent)" font-family="var(--font-mono)" font-size="7">MY NODE</text>
        <text x="285" y="68" text-anchor="middle" fill="var(--lo-faint)" font-family="var(--font-mono)" font-size="6">thinking...</text>
      </svg>
      <h3>ASK LORACLE</h3>
      <p>Anyone in range types a question. You reply with AI, powered by your local LLM.</p>
    </div>

    <div class="lo-ob-step" data-step="4">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <!-- Sidebar sketch -->
        <rect x="30" y="10" width="100" height="100" fill="none" stroke="var(--lo-divider-strong)" stroke-width="1"/>
        <rect x="35" y="20" width="90" height="12" fill="var(--lo-bg-deep)" stroke="none"/>
        <rect x="35" y="36" width="90" height="12" fill="var(--lo-bg-deep)" stroke="none"/>
        <rect x="35" y="52" width="90" height="12" fill="var(--lo-bg-deep)" stroke="none"/>
        <rect x="42" y="54" width="6" height="6" fill="var(--lo-accent)"/>
        <!-- Thread sketch -->
        <rect x="130" y="10" width="200" height="100" fill="none" stroke="var(--lo-divider-strong)" stroke-width="1"/>
        <text x="180" y="30" fill="var(--lo-dim)" font-family="var(--font-mono)" font-size="6">hey you up?</text>
        <text x="260" y="50" fill="var(--lo-accent)" font-family="var(--font-mono)" font-size="6">AI: sure</text>
        <text x="180" y="70" fill="var(--lo-dim)" font-family="var(--font-mono)" font-size="6">how do i treat...</text>
        <circle cx="320" cy="95" r="3" fill="var(--lo-accent)">
          <animate attributeName="r" values="3;4.2" dur="3.2s" repeatCount="indefinite"/>
        </circle>
      </svg>
      <h3>EVERY CONTACT IS A THREAD</h3>
      <p>Each node gets their own conversation. Chat manually or let AI auto-reply. Toggle AI on or off per contact in the thread header.</p>
    </div>

    <div class="lo-ob-step" data-step="5">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <circle cx="180" cy="60" r="6" fill="var(--lo-accent)"/>
        <circle cx="180" cy="60" r="6" fill="none" stroke="var(--lo-accent)" stroke-width="1" opacity="0.7">
          <animate attributeName="r" values="6;28" dur="2.8s" repeatCount="indefinite"/>
          <animate attributeName="opacity" values="0.7;0" dur="2.8s" repeatCount="indefinite"/>
        </circle>
        <circle cx="80" cy="30" r="3" fill="var(--lo-accent-2)"><animate attributeName="r" values="3;4.2" dur="3.2s" repeatCount="indefinite"/></circle>
        <circle cx="280" cy="25" r="3" fill="var(--lo-accent-2)"><animate attributeName="r" values="3;4.2" dur="3.2s" begin="0.6s" repeatCount="indefinite"/></circle>
        <circle cx="60" cy="90" r="3" fill="var(--lo-accent-2)"><animate attributeName="r" values="3;4.2" dur="3.2s" begin="1.2s" repeatCount="indefinite"/></circle>
        <circle cx="300" cy="95" r="3" fill="var(--lo-accent-2)"><animate attributeName="r" values="3;4.2" dur="3.2s" begin="1.8s" repeatCount="indefinite"/></circle>
        <circle cx="120" cy="105" r="3" fill="var(--lo-accent-2)"><animate attributeName="r" values="3;4.2" dur="3.2s" begin="2.4s" repeatCount="indefinite"/></circle>
        <circle cx="250" cy="100" r="3" fill="var(--lo-accent-2)"><animate attributeName="r" values="3;4.2" dur="3.2s" begin="0.3s" repeatCount="indefinite"/></circle>
        <line x1="180" y1="60" x2="80" y2="30" stroke="var(--lo-accent-2)" stroke-width="0.5" opacity="0.4"/>
        <line x1="180" y1="60" x2="280" y2="25" stroke="var(--lo-accent-2)" stroke-width="0.5" opacity="0.4"/>
        <line x1="180" y1="60" x2="60" y2="90" stroke="var(--lo-accent-2)" stroke-width="0.5" opacity="0.4"/>
        <line x1="180" y1="60" x2="300" y2="95" stroke="var(--lo-accent-2)" stroke-width="0.5" opacity="0.4"/>
        <line x1="180" y1="60" x2="120" y2="105" stroke="var(--lo-accent-2)" stroke-width="0.5" opacity="0.4"/>
        <line x1="180" y1="60" x2="250" y2="100" stroke="var(--lo-accent-2)" stroke-width="0.5" opacity="0.4"/>
        <text x="180" y="60" text-anchor="middle" dominant-baseline="central" fill="var(--lo-bg)" font-family="var(--font-mono)" font-size="5" font-weight="500">LO</text>
      </svg>
      <h3>YOU'RE LIVE</h3>
      <p id="ob-live-stats">Your node is active. Tap any contact to open their thread. New messages appear as unread badges.</p>
    </div>

    <div class="lo-ob-nav">
      <button class="btn" id="ob-prev" onclick="obPrev()">PREV</button>
      <button class="lo-ob-skip" onclick="obSkip()">SKIP</button>
      <button class="btn btn-primary" id="ob-next" onclick="obNext()">NEXT</button>
    </div>
  </div>
</div>

<!-- ── Toast Container ───────────────────────────────────────────────────── -->
<div id="toast-container"></div>

<script>
// ─── App State ─────────────────────────────────────────────────────────────

var App = {
  currentTab: 'live',
  state: {},
  logs: [],
  messageFilter: 'all',
  messageSearch: '',
  logFilter: 'all',
  logOpen: false,
  autoScrollFeed: true,
  lastCovRefresh: 0,
  configLoaded: false,
  obStep: 0,
  obTotal: 6
};

// ─── Utilities ─────────────────────────────────────────────────────────────

function escapeHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function formatUptime(s) {
  var h = Math.floor(s / 3600);
  var m = Math.floor((s % 3600) / 60);
  var sec = s % 60;
  return h > 0 ? h+'h '+m+'m' : m > 0 ? m+'m '+sec+'s' : sec+'s';
}

function formatTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString();
}

function relativeTime(ts) {
  var diff = Math.floor(Date.now()/1000 - ts);
  if (diff < 10) return 'just now';
  if (diff < 60) return diff + 's ago';
  if (diff < 3600) return Math.floor(diff/60) + 'm ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  return Math.floor(diff/86400) + 'd ago';
}

function showToast(message, type) {
  type = type || 'info';
  var c = document.getElementById('toast-container');
  var t = document.createElement('div');
  t.className = 'toast toast-' + type;
  t.textContent = message;
  c.appendChild(t);
  setTimeout(function() { t.classList.add('fade-out'); }, 2700);
  setTimeout(function() { if (t.parentNode) c.removeChild(t); }, 3100);
}

async function callApi(method, url, body) {
  try {
    var opts = { method: method, headers: {'Content-Type': 'application/json'} };
    if (body) opts.body = JSON.stringify(body);
    var r = await fetch(url, opts);
    var data = await r.json();
    if (!r.ok) {
      showToast(data.error || 'Request failed', 'error');
      return null;
    }
    return data;
  } catch(e) {
    showToast('Network error', 'error');
    return null;
  }
}

// ─── Theme ─────────────────────────────────────────────────────────────────

function setTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('loracle-theme', theme);
  var sel = document.getElementById('cfg-theme');
  if (sel) sel.value = theme;
}

(function initTheme() {
  var saved = localStorage.getItem('loracle-theme');
  if (saved) setTheme(saved);
  else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) setTheme('dark');
})();

// ─── Clock ─────────────────────────────────────────────────────────────────

function updateClock() {
  var now = new Date();
  var hh = String(now.getHours()).padStart(2,'0');
  var mm = String(now.getMinutes()).padStart(2,'0');
  var ss = String(now.getSeconds()).padStart(2,'0');
  document.getElementById('hdr-clock').textContent = hh+':'+mm+':'+ss;
}
setInterval(updateClock, 1000);
updateClock();

// ─── Tab Switching ─────────────────────────────────────────────────────────

function switchTab(name) {
  document.querySelectorAll('.lo-panel').forEach(function(p) { p.classList.remove('active'); });
  document.querySelectorAll('.lo-tab-btn').forEach(function(b) { b.classList.remove('active'); });
  var panel = document.getElementById('tab-' + name);
  if (panel) panel.classList.add('active');
  var btn = document.querySelector('[data-tab="' + name + '"]');
  if (btn) btn.classList.add('active');
  App.currentTab = name;
  document.getElementById('composer-bar').style.display = (name === 'live') ? '' : 'none';
  if (name === 'config' && !App.configLoaded) loadConfigData();
}

document.querySelectorAll('.lo-tab-btn').forEach(function(btn) {
  btn.addEventListener('click', function() { switchTab(btn.dataset.tab); });
});

// ─── Poll Loop ─────────────────────────────────────────────────────────────

async function poll() {
  try {
    var r = await fetch('/api/state');
    var d = await r.json();
    App.state = d;

    // Title bar connection indicator
    var hdrDot = document.getElementById('hdr-conn-dot');
    var hdrLabel = document.getElementById('hdr-conn-label');
    hdrDot.className = d.connected ? 'lo-conn-dot on' : 'lo-conn-dot';
    hdrLabel.textContent = d.connected ? 'CONNECTED' : 'DISCONNECTED';

    // Status banner
    var pulse = document.getElementById('status-pulse');
    pulse.style.background = d.connected ? 'var(--lo-accent-2)' : 'var(--lo-faint)';
    pulse.style.animation = d.connected ? '' : 'none';
    // Build status text from backends if available
    var connText = 'DISCONNECTED';
    var backends = d.backends || [];
    if (backends.length > 0) {
      connText = backends.map(function(b) {
        return b.protocol.toUpperCase() + ' \u00b7 ' + b.transport + (b.connected ? '' : ' (off)');
      }).join(' | ');
    } else if (d.connected) {
      connText = (d.connection_type || '').toUpperCase() + (d.connection_address ? ' ' + d.connection_address : '');
    }
    document.getElementById('status-conn').textContent = connText;
    document.getElementById('status-model').textContent = d.model || '--';
    document.getElementById('status-rag').textContent = 'rag: ' + (d.rag_enabled ? 'on' : 'off');
    document.getElementById('status-uptime').textContent = formatUptime(d.uptime);

    // Connection manager (always update)
    updateConnMgr(d);

    if (App_view === 'dashboard') {
      updateMeshHeader(d);
      updateStats(d);
      updateMeshMap(d);
      updateMessageFeed(d);
      updateLogTicker();
      // Geographic node map — init once, update every poll
      if (!_geoInitDone) { _geoInitDone = true; setTimeout(function() { initGeoMap(); }, 200); }
      if (_geoMap) updateGeoMap();

      // Coverage auto-refresh
      var covEl = document.getElementById('live-coverage');
      if (covEl && covEl.open) {
        updateCovBanner(d);
        var nowMs = Date.now();
        if (nowMs - App.lastCovRefresh > 10000) {
          App.lastCovRefresh = nowMs;
          loadCoverage();
        }
      }
    }

    if (App_view === 'config') {
      document.getElementById('cfg-uptime').textContent = formatUptime(d.uptime);
      updateRadiosSection(d.backends || []);
      var aiToggle = document.getElementById('cfg-ai-replies');
      if (aiToggle) aiToggle.checked = d.ai_replies_enabled !== false;
    }

    // Messenger updates
    pollMessenger();

  } catch(e) { /* silent retry */ }
}

// ─── Stats ─────────────────────────────────────────────────────────────────

function updateStats(d) {
  document.getElementById('stat-msgs').textContent = d.message_count;
  document.getElementById('stat-msgs-sub').textContent = d.total_llm_calls > 0 ? d.total_llm_calls + ' LLM calls' : '';
  // Node count: merge known_nodes + node_positions for accurate live count
  var allNodes = {};
  (d.known_nodes || []).forEach(function(n) { allNodes[n] = true; });
  Object.keys(d.node_positions || {}).forEach(function(n) { allNodes[n] = true; });
  var nodeCount = Object.keys(allNodes).length;
  document.getElementById('stat-nodes').textContent = nodeCount;
  document.getElementById('stat-nodes-sub').textContent = d.nodedb_size > 0 ? d.nodedb_size + ' in nodedb' : '';
  document.getElementById('stat-reply').textContent = d.avg_llm_time > 0 ? d.avg_llm_time + 's' : '--';
  document.getElementById('stat-reply-sub').textContent = d.avg_chunks > 0 ? d.avg_chunks + ' chunks avg' : '';
  // Radios stat
  var backends = d.backends || [];
  document.getElementById('stat-radios').textContent = backends.length || 1;
  if (backends.length > 0) {
    document.getElementById('stat-radios-sub').textContent = backends.map(function(b) { return b.protocol.toUpperCase(); }).join('+');
  }

  // Node list
  var nodeListEl = document.getElementById('node-list');
  var nodeCountEl = document.getElementById('node-list-count');
  var nodeIds = Object.keys(allNodes).sort();
  nodeCountEl.textContent = nodeIds.length;
  if (nodeIds.length === 0) {
    nodeListEl.innerHTML = '<span style="color:var(--lo-faint)">Waiting for nodes...</span>';
  } else {
    var positions = d.node_positions || {};
    var nodeMeta = d.node_meta || {};
    nodeListEl.innerHTML = nodeIds.map(function(nid) {
      var shortId = nid.length > 8 ? nid.slice(-6) : nid;
      var pos = positions[nid] || {};
      var meta = nodeMeta[nid] || {};
      var parts = ['<span style="color:var(--lo-ink);min-width:60px;display:inline-block">' + escapeHtml(shortId) + '</span>'];
      if (typeof meta.hops === 'number') parts.push(meta.hops === 0 ? 'direct' : meta.hops + 'h');
      if (pos.lat) parts.push(pos.lat.toFixed(2) + ',' + pos.lon.toFixed(2));
      if (pos.last_update) parts.push(relativeTime(pos.last_update));
      return '<div style="padding:3px 0;border-bottom:1px solid var(--lo-divider);display:flex;gap:12px;color:var(--lo-dim)">' + parts.join('<span style="color:var(--lo-faint)"> \u00b7 </span>') + '</div>';
    }).join('');
  }
}

// ─── Mesh Header SVG ───────────────────────────────────────────────────────

var _mhPeerPositions = [
  {x: 120, y: 20}, {x: 580, y: 25}, {x: 100, y: 52}, {x: 600, y: 48}
];

function updateMeshHeader(d) {
  // Count all nodes: known_nodes + node_positions keys
  var allNodes = {};
  (d.known_nodes || []).forEach(function(n) { allNodes[n] = true; });
  Object.keys(d.node_positions || {}).forEach(function(n) { allNodes[n] = true; });
  var peerCount = Object.keys(allNodes).length;
  var header = document.getElementById('mesh-header');
  var noPeers = document.getElementById('no-peers');
  if (peerCount === 0) {
    header.style.display = 'none';
    noPeers.style.display = '';
    return;
  }
  header.style.display = '';
  noPeers.style.display = 'none';

  var shown = Math.min(peerCount, 4);
  var linksG = document.getElementById('mh-links');
  var peersG = document.getElementById('mh-peers');
  linksG.innerHTML = '';
  peersG.innerHTML = '';

  for (var i = 0; i < shown; i++) {
    var p = _mhPeerPositions[i];
    // Link line
    var line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', '350'); line.setAttribute('y1', '34');
    line.setAttribute('x2', p.x); line.setAttribute('y2', p.y);
    line.setAttribute('class', 'mh-link');
    linksG.appendChild(line);
    // Packet animation on link
    var packet = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    packet.setAttribute('x1', '350'); packet.setAttribute('y1', '34');
    packet.setAttribute('x2', p.x); packet.setAttribute('y2', p.y);
    packet.setAttribute('class', i === 0 ? 'mh-packet mh-packet-out' : 'mh-packet');
    packet.setAttribute('style', 'animation: loPacket '+(4+i)+'s linear infinite');
    linksG.appendChild(packet);
    // Peer node
    var circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx', p.x); circle.setAttribute('cy', p.y);
    circle.setAttribute('r', '3'); circle.setAttribute('class', 'mh-peer');
    var anim = document.createElementNS('http://www.w3.org/2000/svg', 'animate');
    anim.setAttribute('attributeName', 'r'); anim.setAttribute('values', '3;4.2;3');
    anim.setAttribute('dur', '3.2s'); anim.setAttribute('begin', (i*0.6)+'s');
    anim.setAttribute('repeatCount', 'indefinite');
    circle.appendChild(anim);
    peersG.appendChild(circle);
  }
}

// ─── Mesh Map SVG ──────────────────────────────────────────────────────────

function hashNodeId(nodeId) {
  var h = 0;
  for (var i = 0; i < nodeId.length; i++) {
    h = ((h << 5) - h + nodeId.charCodeAt(i)) | 0;
  }
  return Math.abs(h);
}

function updateMeshMap(d) {
  var nodesG = document.getElementById('mm-nodes');
  var linksG = document.getElementById('mm-links');
  nodesG.innerHTML = '';
  linksG.innerHTML = '';

  var knownNodes = d.known_nodes || [];
  var positions = d.node_positions || {};
  var nodeMeta = d.node_meta || {};
  var cx = 300, cy = 130;

  // Merge known_nodes + node_positions keys so all discovered nodes show
  var allNodeIds = {};
  knownNodes.forEach(function(n) { allNodeIds[n] = true; });
  Object.keys(positions).forEach(function(n) { allNodeIds[n] = true; });
  var nodeList = Object.keys(allNodeIds);

  // Distribute evenly around concentric rings to avoid cluster
  var total = nodeList.length;
  nodeList.forEach(function(nodeId, idx) {
    var h = hashNodeId(nodeId);
    // Place nodes in concentric rings — golden angle for even spread
    var goldenAngle = 2.399963;  // ~137.5 degrees in radians
    var angle = idx * goldenAngle;
    // Ring radius grows with sqrt(index) for even area distribution
    var maxR = Math.min(cx - 30, cy - 20);
    var radius = 40 + (Math.sqrt((idx + 1) / Math.max(total, 1)) * (maxR - 40));
    var nx = cx + Math.cos(angle) * radius;
    var ny = cy + Math.sin(angle) * (radius * 0.75);
    // Clamp to viewBox
    nx = Math.max(30, Math.min(570, nx));
    ny = Math.max(18, Math.min(245, ny));

    var meta = nodeMeta[nodeId] || {};
    var pos = positions[nodeId] || {};
    var hops = (typeof meta.hops === 'number') ? meta.hops : null;
    var shortId = nodeId.length > 8 ? nodeId.slice(-6) : nodeId;

    // Determine line style based on RSSI if available
    var lineStroke = 'var(--lo-accent-2)';
    var lineDash = '';
    var fillColor = 'var(--lo-accent-2)';
    if (pos.rssi != null && pos.rssi < -100) {
      lineDash = '4,4';
    }
    if (!pos.lat && !pos.lon && hops === null) {
      fillColor = 'var(--lo-dim)';
      lineStroke = 'var(--lo-dim)';
    }

    // Link line
    var line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', cx); line.setAttribute('y1', cy);
    line.setAttribute('x2', nx); line.setAttribute('y2', ny);
    line.setAttribute('stroke', lineStroke);
    line.setAttribute('stroke-width', '0.5');
    line.setAttribute('opacity', '0.4');
    if (lineDash) line.setAttribute('stroke-dasharray', lineDash);
    linksG.appendChild(line);

    // Node group
    var g = document.createElementNS('http://www.w3.org/2000/svg', 'g');

    // Pulse ring
    var ring = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    ring.setAttribute('cx', nx); ring.setAttribute('cy', ny);
    ring.setAttribute('r', '3'); ring.setAttribute('fill', 'none');
    ring.setAttribute('stroke', fillColor); ring.setAttribute('stroke-width', '0.5');
    ring.setAttribute('opacity', '0.5');
    var ringAnim = document.createElementNS('http://www.w3.org/2000/svg', 'animate');
    ringAnim.setAttribute('attributeName', 'r'); ringAnim.setAttribute('values', '3;8');
    ringAnim.setAttribute('dur', '3.2s'); ringAnim.setAttribute('begin', ((h%5)*0.6)+'s');
    ringAnim.setAttribute('repeatCount', 'indefinite');
    var ringOpacAnim = document.createElementNS('http://www.w3.org/2000/svg', 'animate');
    ringOpacAnim.setAttribute('attributeName', 'opacity'); ringOpacAnim.setAttribute('values', '0.5;0');
    ringOpacAnim.setAttribute('dur', '3.2s'); ringOpacAnim.setAttribute('begin', ((h%5)*0.6)+'s');
    ringOpacAnim.setAttribute('repeatCount', 'indefinite');
    ring.appendChild(ringAnim);
    ring.appendChild(ringOpacAnim);
    g.appendChild(ring);

    // Node dot
    var dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    dot.setAttribute('cx', nx); dot.setAttribute('cy', ny);
    dot.setAttribute('r', '3'); dot.setAttribute('fill', fillColor);
    g.appendChild(dot);

    // Name label
    var label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    label.setAttribute('x', nx); label.setAttribute('y', ny - 8);
    label.setAttribute('text-anchor', 'middle');
    label.setAttribute('fill', 'var(--lo-ink)');
    label.setAttribute('font-family', 'var(--font-mono)');
    label.setAttribute('font-size', '7');
    label.setAttribute('letter-spacing', '0.05em');
    label.textContent = shortId;
    g.appendChild(label);

    // RSSI / hop sublabel
    var subParts = [];
    if (hops !== null) subParts.push(hops === 0 ? 'direct' : hops + 'h');
    if (pos.rssi != null) subParts.push(pos.rssi + ' dBm');
    if (subParts.length > 0) {
      var sub = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      sub.setAttribute('x', nx); sub.setAttribute('y', ny + 12);
      sub.setAttribute('text-anchor', 'middle');
      sub.setAttribute('fill', 'var(--lo-dim)');
      sub.setAttribute('font-family', 'var(--font-mono)');
      sub.setAttribute('font-size', '6');
      sub.textContent = subParts.join(' / ');
      g.appendChild(sub);
    }

    nodesG.appendChild(g);
  });
}

// ─── Message Feed ──────────────────────────────────────────────────────────

function setFilter(f, btn) {
  App.messageFilter = f;
  document.querySelectorAll('.lo-feed-header .lo-chip').forEach(function(b) {
    b.classList.toggle('active', b.dataset.filter === f);
  });
}

function updateMessageFeed(d) {
  var msgs = d.messages || [];
  if (App.messageFilter !== 'all') msgs = msgs.filter(function(m) { return m.dir === App.messageFilter; });
  if (App.messageSearch) {
    var q = App.messageSearch.toLowerCase();
    msgs = msgs.filter(function(m) { return m.text.toLowerCase().indexOf(q) !== -1 || m.node.toLowerCase().indexOf(q) !== -1; });
  }

  var feed = document.getElementById('msg-feed');
  if (msgs.length === 0) {
    feed.innerHTML = '<div class="lo-feed-empty">WAITING FOR MESSAGES' +
      (App.messageFilter !== 'all' || App.messageSearch ? ' (FILTERED)' : '') + '</div>';
    return;
  }

  // Check scroll position before update (auto-scroll if near bottom)
  var wasAtBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 50;

  var html = '';
  msgs.forEach(function(m) {
    var arrowClass = m.dir === 'in' ? 'in' : 'out';
    var arrow = m.dir === 'in' ? '\u2190' : '\u2192';
    var shortNode = m.node;
    if (shortNode && shortNode.length > 8) shortNode = shortNode.slice(-6);
    // Protocol badge (MT/MC) — derive from node ID prefix or protocol field
    var badge = 'MT';
    if (m.protocol === 'mc' || (m.node && m.node.indexOf('mc:') === 0)) badge = 'MC';
    html += '<div class="lo-msg">' +
      '<span class="lo-msg-time">' + formatTime(m.ts) + '</span>' +
      '<span class="lo-msg-arrow ' + arrowClass + '">' + arrow + '</span>' +
      '<span class="lo-msg-badge">' + badge + '</span>' +
      '<span class="lo-msg-body"><span class="lo-msg-node">!' + escapeHtml(shortNode) + ' \u00b7 </span>' + escapeHtml(m.text) +
      (m.tier && m.dir === 'out' ? ' <span class="lo-msg-badge" style="border-color:var(--lo-dim)">' + m.tier.toUpperCase() + '</span>' : '') +
      '</span></div>';
  });
  feed.innerHTML = html;

  if (wasAtBottom) feed.scrollTop = feed.scrollHeight;
}

// ─── Composer (Chat) ───────────────────────────────────────────────────────

var chatMessages = [];

async function composerSend() {
  var input = document.getElementById('composer-input');
  var msg = input.value.trim();
  if (!msg) return;

  input.value = '';
  input.disabled = true;
  document.getElementById('composer-send').disabled = true;

  chatMessages.push({role: 'you', text: msg, ts: Date.now()/1000});

  var d = await callApi('POST', '/api/chat', {message: msg});

  if (d && d.ok) {
    chatMessages.push({role: 'ai', text: d.response, ts: Date.now()/1000});
  } else {
    chatMessages.push({role: 'ai', text: 'Error: ' + (d ? d.error : 'No response'), ts: Date.now()/1000});
  }

  input.disabled = false;
  document.getElementById('composer-send').disabled = false;
  input.focus();
}

// ─── System Log ────────────────────────────────────────────────────────────

function toggleLog() {
  App.logOpen = !App.logOpen;
  var btn = document.getElementById('log-toggle');
  var viewer = document.getElementById('log-viewer');
  btn.classList.toggle('open', App.logOpen);
  viewer.classList.toggle('open', App.logOpen);
  if (App.logOpen) loadLogs();
}

function setLogFilter(level, btn) {
  App.logFilter = level;
  document.querySelectorAll('.lo-log-controls .lo-chip').forEach(function(b) { b.classList.remove('active'); });
  if (btn) btn.classList.add('active');
  renderLogs();
}

async function loadLogs() {
  try {
    var r = await fetch('/api/logs?limit=300');
    var d = await r.json();
    App.logs = d.logs || [];
    renderLogs();
  } catch(e) {}
}

function renderLogs() {
  var logs = App.logs;
  if (App.logFilter !== 'all') {
    logs = logs.filter(function(l) { return l.level === App.logFilter; });
  }
  var el = document.getElementById('log-content');
  if (logs.length === 0) {
    el.innerHTML = '<span style="color:var(--lo-faint)">No logs</span>';
    return;
  }
  el.innerHTML = logs.map(function(l) {
    return '<div class="lo-log-line log-' + l.level + '">' + escapeHtml(l.message) + '</div>';
  }).join('');
  el.scrollTop = el.scrollHeight;
}

async function updateLogTicker() {
  if (!App.logOpen) {
    // Fetch last few logs for ticker
    try {
      var r = await fetch('/api/logs?limit=1');
      var d = await r.json();
      var logs = d.logs || [];
      var ticker = document.getElementById('log-ticker');
      if (logs.length > 0) {
        ticker.textContent = logs[logs.length - 1].message;
      }
    } catch(e) {}
  } else {
    loadLogs();
  }
}

// ─── Coverage ──────────────────────────────────────────────────────────────

var _covMap = null;
var _covHeatLayer = null;
var _covDeadLayer = null;
var _covGridLayer = null;
var _covSamples = [];

// Lazy-init coverage map when section opens
document.getElementById('live-coverage').addEventListener('toggle', function() {
  if (this.open) {
    setTimeout(function() { initCovMap(); if (_covMap) _covMap.invalidateSize(); loadCoverage(); }, 100);
  }
});

function initCovMap() {
  if (_covMap) return;
  var el = document.getElementById('cov-map');
  if (!el || typeof L === 'undefined') return;
  _covMap = L.map('cov-map', {attributionControl: false}).setView([39.8, -98.5], 4);
  L.tileLayer('/tiles/{z}/{x}/{y}.png', {maxZoom: 15, attribution: 'OSM'}).addTo(_covMap);
}

function updateCovBanner(d) {
  var banner = document.getElementById('cov-banner');
  var txt = document.getElementById('cov-banner-text');
  if (!banner || !txt) return;
  if (!d || !d.connected) {
    banner.style.display = '';
    txt.textContent = 'Bridge disconnected \u2014 coverage data shown is from the last connected session.';
  } else {
    banner.style.display = 'none';
  }
}

async function clearCoverage() {
  if (!confirm('Permanently delete the coverage log file? This cannot be undone.')) return;
  try {
    var r = await fetch('/api/coverage/clear', {method: 'POST'});
    var d = await r.json();
    var statsEl = document.getElementById('cov-stats');
    if (d && d.ok) {
      _covSamples = [];
      App.lastCovRefresh = 0;
      if (statsEl) statsEl.textContent = 'Cleared (' + d.removed + ' samples removed)';
      renderCoverage();
      setTimeout(loadCoverage, 800);
    } else {
      if (statsEl) statsEl.textContent = 'Clear failed';
    }
  } catch(e) {
    var statsEl = document.getElementById('cov-stats');
    if (statsEl) statsEl.textContent = 'Clear failed: ' + e;
  }
}

async function loadCoverage() {
  try {
    var sr = await fetch('/api/coverage/samples?limit=10000');
    var sd = await sr.json();
    _covSamples = sd.samples || [];
    var tr = await fetch('/api/coverage/stats');
    var td = await tr.json();
    var statsEl = document.getElementById('cov-stats');
    if (td.count > 0) {
      statsEl.textContent = td.count + ' samples / ' + td.nodes + ' nodes';
    } else {
      statsEl.textContent = 'No samples yet';
    }
    renderCoverage();
  } catch(e) {
    var el = document.getElementById('cov-stats');
    if (el) el.textContent = 'Error: ' + e;
  }
}

function covColorForRssi(rssi) {
  // Must use hex colors (not CSS vars) — Leaflet canvas gradient can't parse var()
  if (rssi == null) return '#c0392b';
  if (rssi >= -60)  return '#0f6e56';
  if (rssi >= -80)  return '#7dcea0';
  if (rssi >= -95)  return '#d4a017';
  if (rssi >= -110) return '#ff4f00';
  return '#c0392b';
}

function renderCoverage() {
  if (!_covMap) return;
  var mode = document.getElementById('cov-mode').value;
  var windowSec = parseInt(document.getElementById('cov-window').value);
  var minRssi = parseInt(document.getElementById('cov-rssi').value);
  var showDead = document.getElementById('cov-deadzones').checked;
  var now = Date.now() / 1000;

  var filtered = _covSamples.filter(function(s) {
    if (windowSec > 0 && (now - s.ts) > windowSec) return false;
    if (s.rssi != null && s.rssi < minRssi) return false;
    return true;
  });

  var cellDeg = 0.00036;
  var cells = {};
  filtered.forEach(function(s) {
    var latBin = Math.round(s.lat / cellDeg);
    var lonBin = Math.round(s.lon / cellDeg);
    var key = latBin + ',' + lonBin;
    var c = cells[key];
    if (!c) { cells[key] = { latBin: latBin, lonBin: lonBin, bestRssi: s.rssi, count: 1 }; }
    else { c.count++; if (s.rssi != null && (c.bestRssi == null || s.rssi > c.bestRssi)) c.bestRssi = s.rssi; }
  });

  if (_covGridLayer) { _covMap.removeLayer(_covGridLayer); _covGridLayer = null; }
  if (_covHeatLayer) { _covMap.removeLayer(_covHeatLayer); _covHeatLayer = null; }
  if (_covDeadLayer) { _covMap.removeLayer(_covDeadLayer); _covDeadLayer = null; }

  if (mode === 'grid' || mode === 'both') {
    _covGridLayer = L.layerGroup();
    Object.keys(cells).forEach(function(k) {
      var c = cells[k];
      var latMin = c.latBin * cellDeg - cellDeg / 2;
      var latMax = c.latBin * cellDeg + cellDeg / 2;
      var lonMin = c.lonBin * cellDeg - cellDeg / 2;
      var lonMax = c.lonBin * cellDeg + cellDeg / 2;
      var color = covColorForRssi(c.bestRssi);
      var fillOpacity = (c.count >= 3) ? 0.72 : (c.count === 2 ? 0.58 : 0.42);
      L.rectangle([[latMin, lonMin], [latMax, lonMax]], {
        color: color, weight: 1, opacity: 0.85, fillColor: color, fillOpacity: fillOpacity
      }).addTo(_covGridLayer);
    });
    _covGridLayer.addTo(_covMap);
  }

  if ((mode === 'heat' || mode === 'both') && filtered.length > 0 && typeof L.heatLayer === 'function') {
    var heatPoints = filtered.map(function(s) {
      var intensity = 0.5;
      if (s.rssi != null) intensity = Math.max(0, Math.min(1, (s.rssi - (-130)) / 100));
      return [s.lat, s.lon, intensity];
    });
    _covHeatLayer = L.heatLayer(heatPoints, {
      radius: 55, blur: 35, maxZoom: 15, minOpacity: 0.55,
      gradient: { 0.0: '#7a0000', 0.2: '#c0392b', 0.4: '#ff4f00', 0.6: '#d4a017', 0.8: '#7dcea0', 1.0: '#0f6e56' }
    }).addTo(_covMap);
  }

  if (showDead) {
    var deadCells = [];
    Object.keys(cells).forEach(function(k) {
      var c = cells[k];
      if (c.bestRssi == null || c.bestRssi < -110) deadCells.push([c.latBin * cellDeg, c.lonBin * cellDeg]);
    });
    if (deadCells.length > 0) {
      _covDeadLayer = L.layerGroup();
      deadCells.forEach(function(ll) {
        L.circle(ll, { radius: 80, color: '#c0392b', weight: 2, fillColor: '#c0392b', fillOpacity: 0.45 }).addTo(_covDeadLayer);
      });
      _covDeadLayer.addTo(_covMap);
    }
  }

  if (filtered.length > 0) {
    var bounds = filtered.map(function(s) { return [s.lat, s.lon]; });
    try { _covMap.fitBounds(bounds, {padding: [40, 40], maxZoom: 16}); } catch(e) {}
  }
}

// ─── Connection Manager ────────────────────────────────────────────────────

function updateConnMgr(d) {
  var dot = document.getElementById('conn-mgr-dot');
  var status = document.getElementById('conn-mgr-status');
  var detail = document.getElementById('conn-mgr-detail');
  var disconnBtn = document.getElementById('conn-disconnect-btn');
  if (d.connected) {
    dot.style.background = 'var(--lo-accent-2)';
    dot.style.animation = 'loPulse 2s ease-in-out infinite';
    status.textContent = 'Connected';
    detail.textContent = (d.connection_type || '').toUpperCase() +
      (d.connection_address ? ' \u2014 ' + d.connection_address : '');
    disconnBtn.style.display = '';
  } else {
    dot.style.background = 'var(--lo-faint)';
    dot.style.animation = 'none';
    status.textContent = 'Disconnected';
    detail.textContent = '';
    disconnBtn.style.display = 'none';
  }
  if (d.ble_available === false) {
    document.getElementById('ble-unavailable').style.display = '';
    document.getElementById('ble-scan-btn').disabled = true;
  }
}

// ─── CONFIG Tab Controls ───────────────────────────────────────────────────

async function loadConfigData() {
  App.configLoaded = true;
  await cfgRefreshModels();
  var pd = await callApi('GET', '/api/system-prompt');
  if (pd) {
    document.getElementById('cfg-prompt').value = pd.prompt;
    document.getElementById('cfg-prompt-count').textContent = pd.prompt.length + ' chars';
  }
  var cd = await callApi('GET', '/api/config');
  if (cd) {
    document.getElementById('cfg-max-len').value = cd.max_response_length;
    document.getElementById('cfg-max-len-val').textContent = cd.max_response_length;
    document.getElementById('cfg-compression').checked = cd.compression_enabled;
  }
  cfgLoadRagDocs();
  cfgLoadDbStats();
  cfgLoadRouting();
  cfgLoadPacks();
  loadLastBleDevice();
}

document.getElementById('cfg-prompt').addEventListener('input', function() {
  document.getElementById('cfg-prompt-count').textContent = this.value.length + ' chars';
});

async function cfgRefreshModels() {
  var d = await callApi('GET', '/api/models');
  if (!d) return;
  var sel = document.getElementById('cfg-model-select');
  sel.innerHTML = d.models.map(function(m) {
    return '<option' + (m === d.current ? ' selected' : '') + '>' + escapeHtml(m) + '</option>';
  }).join('');
  document.getElementById('cfg-current-model').textContent = d.current;
  // Also update stats
  try {
    var rs = await callApi('GET', '/api/rag/stats');
    if (rs && rs.available) {
      document.getElementById('cfg-rag-toggle').checked = App.state.rag_enabled;
      var stats = rs.stats || {};
      document.getElementById('cfg-rag-stats').textContent = (stats.total_docs || stats.documents || 0) + ' docs';
      document.getElementById('stat-docs').textContent = (stats.total_docs || stats.documents || 0);
      document.getElementById('stat-docs-sub').textContent = (stats.total_chunks || stats.chunks || 0) + ' chunks';
    }
  } catch(e) {}
}

async function cfgSwitchModel() {
  var model = document.getElementById('cfg-model-select').value;
  var d = await callApi('POST', '/api/model', {model: model});
  if (d && d.ok) { showToast('Model: ' + d.model); cfgRefreshModels(); }
}

async function cfgSavePrompt() {
  var prompt = document.getElementById('cfg-prompt').value;
  var d = await callApi('POST', '/api/system-prompt', {prompt: prompt});
  if (d && d.ok) showToast('Prompt saved');
}

async function cfgApplySettings() {
  var d = await callApi('POST', '/api/config', {
    max_response_length: parseInt(document.getElementById('cfg-max-len').value),
    compression_enabled: document.getElementById('cfg-compression').checked
  });
  if (d && d.ok) showToast('Settings applied');
}

async function cfgToggleRag(enabled) {
  await callApi('POST', '/api/rag/toggle', {enabled: enabled});
}

async function cfgIngestUrl() {
  var urlInput = document.getElementById('cfg-url-input');
  var url = urlInput.value.trim();
  if (!url) return;
  var status = document.getElementById('cfg-url-status');
  status.innerHTML = '<span style="color:var(--lo-dim)">Fetching & ingesting...</span>';
  var d = await callApi('POST', '/api/rag/ingest-url', {url: url});
  if (d && d.ok) {
    status.innerHTML = '<span style="color:var(--lo-accent-2)">Ingested: ' + escapeHtml(d.filename) + ' (' + d.chunks + ' chunks)</span>';
    urlInput.value = '';
    cfgLoadRagDocs();
  } else {
    status.innerHTML = '<span style="color:#c0392b">Error: ' + escapeHtml((d && d.error) || 'Unknown') + '</span>';
  }
}

async function cfgUploadFile() {
  var input = document.getElementById('cfg-file-upload');
  if (!input.files.length) return;
  var formData = new FormData();
  formData.append('file', input.files[0]);
  try {
    var res = await fetch('/api/rag/ingest-file', {method: 'POST', body: formData});
    var data = await res.json();
    if (data.ok) {
      showToast('Uploaded: ' + data.filename + ' (' + data.chunks + ' chunks)');
      input.value = '';
      cfgLoadRagDocs();
    } else {
      showToast(data.error || 'Upload failed', 'error');
    }
  } catch(e) { showToast('Upload error', 'error'); }
}

async function cfgLoadRagDocs() {
  var d = await callApi('GET', '/api/rag/stats');
  var container = document.getElementById('cfg-rag-docs');
  if (!container) return;
  if (!d || !d.documents || d.documents.length === 0) {
    container.innerHTML = '<span style="color:var(--lo-faint)">No documents ingested yet.</span>';
    return;
  }
  container.innerHTML = d.documents.map(function(doc) {
    return '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid var(--lo-divider)">' +
      '<span style="color:var(--lo-dim)">' + escapeHtml(doc.filename || doc.doc_id) + '</span>' +
      '<span style="display:flex;align-items:center;gap:8px">' +
        '<span style="color:var(--lo-faint);font-size:10px">' + (doc.chunk_count || doc.chunks || 0) + ' chunks</span>' +
        '<button class="btn btn-sm" style="color:#c0392b;border-color:#c0392b" onclick="cfgDeleteDoc(\'' + escapeHtml(doc.doc_id) + '\')">x</button>' +
      '</span></div>';
  }).join('');
}

async function cfgDeleteDoc(docId) {
  if (!confirm('Delete this document from the knowledge base?')) return;
  var d = await callApi('POST', '/api/rag/delete', {doc_id: docId});
  if (d && d.ok) { showToast('Deleted'); cfgLoadRagDocs(); }
}

async function clearHistory() {
  if (!confirm('Clear all conversation history? This cannot be undone.')) return;
  var d = await callApi('POST', '/api/clear-history', {});
  if (d && d.ok) showToast('Cleared history for ' + d.cleared + ' node(s)');
}

// ─── Knowledge Packs ───────────────────────────────────────────────────────

async function cfgLoadPacks() {
  try {
    var d = await callApi('GET', '/api/packs');
    if (!d || !d.packs) return;
    var el = document.getElementById('cfg-packs-list');
    if (d.packs.length === 0) {
      el.innerHTML = '<span style="color:var(--lo-faint);font-size:10px">No packs available</span>';
      return;
    }
    el.innerHTML = d.packs.map(function(p) {
      var status = p.installed ?
        '<span style="color:var(--lo-accent-2)">INSTALLED</span> \u00b7 ' + (p.doc_count_success || 0) + ' docs' :
        '<span style="color:var(--lo-faint)">NOT INSTALLED</span>';
      var size = p.estimated_size_mb ? ' \u00b7 ~' + p.estimated_size_mb + 'MB' : '';
      return '<div style="padding:8px 0;border-bottom:1px solid var(--lo-divider);cursor:pointer" onclick="cfgShowPackDetail(\'' + escapeHtml(p.id) + '\')">' +
        '<div style="display:flex;justify-content:space-between;align-items:center">' +
          '<span style="color:var(--lo-ink);font-size:11px;font-weight:500">' + escapeHtml(p.name) + '</span>' +
          '<span style="font-size:9px">' + status + size + '</span>' +
        '</div>' +
        '<div style="color:var(--lo-dim);font-size:10px;margin-top:2px">' + escapeHtml(p.description).substring(0, 80) + '</div>' +
      '</div>';
    }).join('');
  } catch(e) {}
}

async function cfgShowPackDetail(packId) {
  try {
    var d = await callApi('GET', '/api/packs/' + encodeURIComponent(packId));
    if (!d) return;
    var el = document.getElementById('cfg-pack-detail');
    var content = document.getElementById('cfg-pack-detail-content');
    el.style.display = '';

    var docsHtml = (d.documents || []).map(function(doc) {
      var installed = (d.installed_docs || []).find(function(id) { return id.doc_id === doc.id; });
      var icon = installed ? '\u25cf' : '\u25cb';
      var chunks = installed ? ' \u00b7 ' + (installed.chunk_count || 0) + ' chunks' : '';
      return '<div style="padding:3px 0;font-size:10px;color:var(--lo-dim)">' +
        icon + ' ' + escapeHtml(doc.filename) + chunks +
        (doc.attribution ? ' <span style="color:var(--lo-faint)">(' + escapeHtml(doc.license) + ')</span>' : '') +
      '</div>';
    }).join('');

    var actions = '';
    if (d.installed) {
      actions = '<button class="btn btn-sm" onclick="cfgReinstallPack(\'' + escapeHtml(packId) + '\')">REINGEST</button> ' +
                '<button class="btn btn-sm" style="color:#c0392b;border-color:#c0392b" onclick="cfgUninstallPack(\'' + escapeHtml(packId) + '\')">UNINSTALL</button>';
    } else {
      actions = '<button class="btn btn-primary btn-sm" onclick="cfgInstallPack(\'' + escapeHtml(packId) + '\')">INSTALL PACK</button>';
    }

    content.innerHTML =
      '<div style="font-size:12px;font-weight:500;color:var(--lo-ink);margin-bottom:4px">' + escapeHtml(d.name) + ' \u00b7 v' + escapeHtml(d.version) + '</div>' +
      '<div style="font-size:10px;color:var(--lo-dim);margin-bottom:8px">' +
        (d.installed ? 'INSTALLED' : 'NOT INSTALLED') + ' \u00b7 ' + (d.documents || []).length + ' documents \u00b7 ~' + (d.estimated_size_mb || 0) + 'MB' +
      '</div>' +
      '<div style="font-size:10px;color:var(--lo-dim);margin-bottom:8px">' + escapeHtml(d.license_summary || '') + '</div>' +
      '<div style="margin-bottom:8px">' + docsHtml + '</div>' +
      '<div id="cfg-pack-progress" style="display:none;margin-bottom:8px"></div>' +
      '<div>' + actions + '</div>';
  } catch(e) {}
}

async function cfgInstallPack(packId) {
  showToast('Installing pack... this may take a few minutes');
  // Subscribe to SSE for progress
  var progEl = document.getElementById('cfg-pack-progress');
  if (progEl) { progEl.style.display = ''; progEl.innerHTML = '<span style="color:var(--lo-dim);font-size:10px">Starting download...</span>'; }
  await callApi('POST', '/api/packs/' + encodeURIComponent(packId) + '/install');
  // Poll for completion (SSE handles real-time, but also reload after)
  setTimeout(function() { cfgLoadPacks(); cfgShowPackDetail(packId); }, 5000);
}

async function cfgUninstallPack(packId) {
  if (!confirm('Uninstall this pack? Documents and RAG chunks will be removed.')) return;
  var d = await callApi('POST', '/api/packs/' + encodeURIComponent(packId) + '/uninstall');
  if (d && d.ok) showToast('Pack uninstalled');
  cfgLoadPacks();
  document.getElementById('cfg-pack-detail').style.display = 'none';
}

async function cfgReinstallPack(packId) {
  var d = await callApi('POST', '/api/packs/' + encodeURIComponent(packId) + '/reingest');
  if (d && d.ok) showToast('Re-ingested: ' + d.total_chunks + ' chunks');
  cfgShowPackDetail(packId);
}

// ─── Model Routing Config ──────────────────────────────────────────────────

async function cfgLoadRouting() {
  try {
    var d = await callApi('GET', '/api/routing/config');
    if (!d) return;
    document.getElementById('cfg-routing-auto').checked = d.auto_enabled !== false;
    document.getElementById('cfg-routing-tag').checked = d.show_tier_tag !== false;
    if (d.tiers) {
      if (d.tiers.tiny) {
        document.getElementById('cfg-tier-tiny-model').value = d.tiers.tiny.model || '';
        document.getElementById('cfg-tier-tiny-enabled').checked = d.tiers.tiny.enabled;
      }
      if (d.tiers.std) {
        document.getElementById('cfg-tier-std-model').value = d.tiers.std.model || '';
        document.getElementById('cfg-tier-std-enabled').checked = d.tiers.std.enabled;
      }
      if (d.tiers.big) {
        document.getElementById('cfg-tier-big-model').value = d.tiers.big.model || '';
        document.getElementById('cfg-tier-big-enabled').checked = d.tiers.big.enabled;
      }
    }
  } catch(e) {}
}

async function cfgSetRouting(key, value) {
  var payload = {};
  if (key === 'auto') payload.auto_enabled = value;
  if (key === 'tag') payload.show_tier_tag = value;
  await callApi('POST', '/api/routing/config', payload);
}

async function cfgSaveTiers() {
  var tiers = {
    tiny: { model: document.getElementById('cfg-tier-tiny-model').value, enabled: document.getElementById('cfg-tier-tiny-enabled').checked },
    std: { model: document.getElementById('cfg-tier-std-model').value, enabled: document.getElementById('cfg-tier-std-enabled').checked },
    big: { model: document.getElementById('cfg-tier-big-model').value, enabled: document.getElementById('cfg-tier-big-enabled').checked },
  };
  var d = await callApi('POST', '/api/routing/config', { tiers: tiers });
  if (d && d.ok) showToast('Tiers saved');
}

var _classifierDebounce = null;
function testClassifier(query) {
  clearTimeout(_classifierDebounce);
  var el = document.getElementById('cfg-classifier-result');
  if (!query.trim()) { el.textContent = ''; return; }
  _classifierDebounce = setTimeout(async function() {
    var d = await callApi('POST', '/api/routing/classify', { query: query });
    if (d) el.textContent = 'Would route to: [' + d.tier.toUpperCase() + '] \u00b7 model: ' + d.model;
  }, 200);
}

async function cfgPruneNow() {
  var d = await callApi('POST', '/api/db/prune');
  if (d && d.ok) showToast('Pruned ' + d.pruned + ' messages');
  cfgLoadDbStats();
}

async function cfgClearAllMessages() {
  if (!confirm('Delete ALL messages from the database? This cannot be undone. Contacts are preserved.')) return;
  var d = await callApi('POST', '/api/db/clear-messages');
  if (d && d.ok) showToast('Deleted ' + d.deleted + ' messages');
  cfgLoadDbStats();
}

async function cfgLoadDbStats() {
  try {
    var d = await callApi('GET', '/api/db/stats');
    if (d) {
      var sizeKb = Math.round((d.db_size_bytes || 0) / 1024);
      document.getElementById('cfg-db-stats').textContent =
        d.contacts + ' contacts, ' + d.messages + ' messages, ' + sizeKb + ' KB';
    }
  } catch(e) {}
}

// ─── BLE / Connection ──────────────────────────────────────────────────────

var _bleLastDevice = null;

async function bleScan() {
  var btn = document.getElementById('ble-scan-btn');
  var statusEl = document.getElementById('ble-scan-status');
  var listEl = document.getElementById('ble-device-list');
  btn.disabled = true; btn.textContent = 'SCANNING...';
  statusEl.textContent = 'Scanning (~10s)...';
  listEl.innerHTML = '';
  try {
    var d = await callApi('GET', '/api/ble/scan?timeout=10');
    if (!d || !d.devices) { statusEl.textContent = 'Scan failed.'; return; }
    var realDevices = d.devices.filter(function(dev) { return !dev.error; });
    if (realDevices.length === 0) {
      statusEl.textContent = 'No Meshtastic devices found.';
      // Check for permission error
      if (d.devices.length === 1 && d.devices[0].error === 'bluetooth_permission') {
        statusEl.textContent = d.devices[0].message || 'Bluetooth permission required';
      }
      return;
    }
    statusEl.textContent = realDevices.length + ' device(s) found:';
    var html = '';
    realDevices.forEach(function(dev) {
      html += '<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--lo-divider)">' +
        '<div style="flex:1"><div>' + escapeHtml(dev.name || 'Unknown') + '</div>' +
        '<div style="font-size:10px;color:var(--lo-faint)">' + escapeHtml(dev.address) + ' / ' + dev.rssi + ' dBm</div></div>' +
        '<button class="btn btn-sm btn-primary" onclick="bleConnect(\'' + escapeHtml(dev.address) + '\',\'' + escapeHtml(dev.name || '') + '\')">CONNECT</button></div>';
    });
    listEl.innerHTML = html;
  } catch(e) { statusEl.textContent = 'Scan error: ' + e; }
  finally { btn.disabled = false; btn.textContent = 'SCAN'; }
}

async function bleConnect(address, name) {
  document.getElementById('ble-scan-status').textContent = 'Connecting to ' + (name || address) + '...';
  var d = await callApi('POST', '/api/connection/switch', {type: 'ble', address: address});
  if (d && d.ok) {
    document.getElementById('ble-scan-status').textContent = 'Connected to ' + (name || address);
    loadLastBleDevice();
  } else {
    document.getElementById('ble-scan-status').textContent = 'Connecting in background...';
  }
}

async function bleQuickConnect() {
  if (!_bleLastDevice) return;
  await bleConnect(_bleLastDevice.address, _bleLastDevice.name);
}

async function disconnectRadio() { await callApi('POST', '/api/connection/disconnect'); }

function connTypeChanged() {
  var sel = document.getElementById('conn-type-select');
  var inp = document.getElementById('conn-address-input');
  if (sel.value === 'serial') inp.placeholder = 'auto-detect (or /dev/...)';
  else if (sel.value === 'tcp') inp.placeholder = '192.168.1.1:4403';
  else inp.placeholder = 'BLE address (or empty to scan)';
}

async function manualConnect() {
  var type = document.getElementById('conn-type-select').value;
  var addr = document.getElementById('conn-address-input').value.trim();
  var payload = {type: type};
  if (type === 'ble') payload.address = addr || null;
  else if (type === 'tcp') {
    if (addr && addr.indexOf(':') !== -1) {
      var parts = addr.split(':');
      payload.host = parts[0];
      payload.port = parseInt(parts[1]);
    } else if (addr) {
      payload.host = addr;
    }
  } else {
    payload.address = addr || null;
  }
  var d = await callApi('POST', '/api/connection/switch', payload);
  if (d && d.ok) showToast('Connected!');
  else showToast('Connecting in background...');
}

async function loadLastBleDevice() {
  try {
    var d = await callApi('GET', '/api/ble/last-device');
    if (d && d.device && d.device.address) {
      _bleLastDevice = d.device;
      var el = document.getElementById('ble-last-device');
      el.style.display = '';
      document.getElementById('ble-last-name').textContent = d.device.name || d.device.address;
    }
  } catch(e) {}
}

// ─── Geographic Map ────────────────────────────────────────────────────────

var _geoMap = null;
var _geoMarkers = {};
var _geoInitDone = false;

function initGeoMap() {
  if (_geoMap) return;
  var el = document.getElementById('geo-map');
  if (!el || typeof L === 'undefined') return;
  _geoMap = L.map('geo-map', {attributionControl: false}).setView([39.8, -98.5], 4);
  L.tileLayer('/tiles/{z}/{x}/{y}.png', {maxZoom: 15, attribution: 'OSM'}).addTo(_geoMap);
}

function updateGeoMap() {
  if (!_geoMap || !App.state) return;
  var positions = App.state.node_positions || {};
  var nodeMeta = App.state.node_meta || {};
  var bounds = [];
  var count = 0;

  Object.keys(positions).forEach(function(nodeId) {
    var p = positions[nodeId];
    if (!p.lat || !p.lon) return;
    count++;
    var latlng = [p.lat, p.lon];
    bounds.push(latlng);
    var shortId = nodeId.length > 10 ? nodeId.slice(-6) : nodeId;
    var meta = nodeMeta[nodeId] || {};
    var hops = (typeof meta.hops === 'number') ? meta.hops : null;
    var hopSuffix = '';
    if (hops !== null) {
      if (hops === 0) hopSuffix = ' \u00b7 direct';
      else hopSuffix = ' \u00b7 ' + hops + 'h';
    }
    var label = shortId + hopSuffix;
    var ageSec = p.last_update ? (Date.now() / 1000 - p.last_update) : 0;
    var staleCls = ageSec > 600 ? ' stale' : '';
    var iconHtml = '<div class="node-marker' + staleCls + '"><div class="ring"></div><div class="core"></div><div class="label">' + escapeHtml(label) + '</div></div>';

    if (_geoMarkers[nodeId]) {
      _geoMarkers[nodeId].setLatLng(latlng);
    } else {
      var icon = L.divIcon({ className: 'node-marker-wrap', html: iconHtml, iconSize: [28, 28], iconAnchor: [14, 14] });
      _geoMarkers[nodeId] = L.marker(latlng, {icon: icon, zIndexOffset: 1000}).addTo(_geoMap);
    }
    var popupHtml = '<div style="font-family:var(--font-mono);font-size:11px;min-width:160px">' +
      '<div style="font-weight:500;color:var(--lo-accent-2);margin-bottom:3px">' + escapeHtml(nodeId) + '</div>' +
      '<div>' + p.lat.toFixed(5) + ', ' + p.lon.toFixed(5) + '</div>' +
      '<div style="color:var(--lo-dim);margin-top:2px">' + (p.last_update ? relativeTime(p.last_update) : '') + '</div></div>';
    _geoMarkers[nodeId].bindPopup(popupHtml);
  });

  Object.keys(_geoMarkers).forEach(function(id) {
    if (!positions[id]) { _geoMap.removeLayer(_geoMarkers[id]); delete _geoMarkers[id]; }
  });
  document.getElementById('geo-node-count').textContent = count + ' nodes with GPS';
  if (bounds.length > 0 && !App._geoFitted) {
    _geoMap.fitBounds(bounds, {padding: [30, 30], maxZoom: 14});
    App._geoFitted = true;
  }
}

// ─── Onboarding ────────────────────────────────────────────────────────────

function showOnboarding() {
  App.obStep = 0;
  renderObStep();
  document.getElementById('onboarding').classList.add('open');
}

function obNext() {
  if (App.obStep < App.obTotal - 1) { App.obStep++; renderObStep(); }
  else obSkip();
}

function obPrev() {
  if (App.obStep > 0) { App.obStep--; renderObStep(); }
}

function obSkip() {
  document.getElementById('onboarding').classList.remove('open');
  localStorage.setItem('loracle-onboarded', 'true');
}

function renderObStep() {
  document.querySelectorAll('.lo-ob-step').forEach(function(s) { s.classList.remove('active'); });
  var steps = document.querySelectorAll('.lo-ob-step');
  if (steps[App.obStep]) steps[App.obStep].classList.add('active');
  // Progress dots
  var prog = '';
  for (var i = 0; i < App.obTotal; i++) {
    if (i < App.obStep) prog += '<span class="lo-ob-dot done">\u25CF</span>';
    else if (i === App.obStep) prog += '<span class="lo-ob-dot active">\u25B8</span>';
    else prog += '<span class="lo-ob-dot">\u25CB</span>';
  }
  document.getElementById('ob-progress').innerHTML = prog;
  // Nav buttons
  document.getElementById('ob-prev').style.visibility = App.obStep > 0 ? '' : 'hidden';
  document.getElementById('ob-next').textContent = App.obStep < App.obTotal - 1 ? 'NEXT' : 'DONE';
  // Update live stats on last step
  if (App.obStep === 4 && App.state) {
    var statsText = (App.state.node_count || 0) + ' nodes in range';
    if (App.state.rag_enabled) statsText += ' \u00b7 rag: on';
    document.getElementById('ob-live-stats').textContent = statsText;
  }
}

// Keyboard navigation
document.addEventListener('keydown', function(e) {
  if (!document.getElementById('onboarding').classList.contains('open')) return;
  if (e.key === 'ArrowRight') obNext();
  if (e.key === 'ArrowLeft') obPrev();
  if (e.key === 'Escape') obSkip();
});

// Auto-show on first visit
if (localStorage.getItem('loracle-onboarded') !== 'true') {
  setTimeout(showOnboarding, 500);
}

// ─── Help Popover ──────────────────────────────────────────────────────────

document.getElementById('help-toggle').addEventListener('click', function() {
  document.getElementById('help-popover').classList.toggle('open');
});

// Close help on outside click
document.addEventListener('click', function(e) {
  if (!e.target.closest('#help-popover') && !e.target.closest('#help-toggle')) {
    document.getElementById('help-popover').classList.remove('open');
  }
});

// ─── Theme Toggle ──────────────────────────────────────────────────────────

document.getElementById('theme-toggle').addEventListener('click', function() {
  var current = document.documentElement.getAttribute('data-theme') || 'light';
  setTheme(current === 'light' ? 'dark' : 'light');
});

// ─── Radios + AI Replies ───────────────────────────────────────────────────

function updateRadiosSection(backends) {
  var el = document.getElementById('cfg-radios-list');
  if (!el) return;
  if (!backends || backends.length === 0) {
    el.innerHTML = '<span style="color:var(--lo-faint);font-size:10px">No radios connected</span>';
    return;
  }
  el.innerHTML = backends.map(function(b, i) {
    var statusDot = b.connected ?
      '<span class="lo-pulse" style="width:6px;height:6px"></span>' :
      '<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--lo-faint)"></span>';
    var label = (i === 0 ? 'PRIMARY' : 'SECONDARY');
    return '<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--lo-divider)">' +
      statusDot +
      '<span style="font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:var(--lo-dim);min-width:70px">' + label + '</span>' +
      '<span style="font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:var(--lo-ink)">' + b.protocol.toUpperCase() + '</span>' +
      '<span style="color:var(--lo-dim);font-size:10px">' + escapeHtml(b.transport) + '</span>' +
      '<span style="color:var(--lo-faint);font-size:10px">' + (b.connected ? 'connected' : 'disconnected') + '</span>' +
      '</div>';
  }).join('');
}

async function cfgToggleAiReplies(enabled) {
  await callApi('POST', '/api/ai-replies', {enabled: enabled});
}

// ─── View Toggle ───────────────────────────────────────────────────────────

var App_view = 'messenger';  // 'messenger' | 'dashboard' | 'config'

function switchView(view) {
  App_view = view;
  // Hide all views
  var messenger = document.getElementById('view-messenger');
  var dashboard = document.getElementById('tab-live');
  var config = document.getElementById('tab-config');
  messenger.classList.remove('active');
  dashboard.classList.remove('active');
  config.classList.remove('active');
  // Show selected
  if (view === 'messenger') messenger.classList.add('active');
  else if (view === 'dashboard') dashboard.classList.add('active');
  else if (view === 'config') config.classList.add('active');
  // Update toggle buttons
  document.querySelectorAll('.lo-view-toggle button').forEach(function(b) {
    b.classList.toggle('active', b.dataset.view === view);
  });
  // Trigger config load if needed
  if (view === 'config' && !App.configLoaded) loadConfigData();
  if (view === 'dashboard') {
    App.currentTab = 'live';
    setTimeout(function() { initGeoMap(); if (_geoMap) _geoMap.invalidateSize(); }, 100);
  }
  if (view === 'messenger') {
    App.currentTab = 'messenger';
    loadSidebar();
  }
}

// ─── Messenger Sidebar ─────────────────────────────────────────────────────

var _sidebarTab = 'dm';
var _sidebarSearch = '';
var _sidebarContacts = [];
var _selectedThread = null;

function setSidebarTab(tab, btn) {
  _sidebarTab = tab;
  document.querySelectorAll('.lo-sidebar-tabs button').forEach(function(b) {
    b.classList.toggle('active', b.dataset.stab === tab);
  });
  renderSidebar();
}

function filterSidebar(q) {
  _sidebarSearch = q.toLowerCase();
  renderSidebar();
}

async function loadSidebar() {
  try {
    var r = await fetch('/api/threads');
    var d = await r.json();
    _sidebarContacts = d.threads || [];
    renderSidebar();
    // Footer
    var state = App.state || {};
    var backends = state.backends || [];
    var radioText = backends.map(function(b) { return b.protocol.toUpperCase() + ' ' + (b.connected ? 'on' : 'off'); }).join(' \u00b7 ');
    document.getElementById('sidebar-radios').textContent = radioText || 'MT';
    var totalUnread = 0;
    _sidebarContacts.forEach(function(c) { totalUnread += (c.unread_count || 0); });
    document.getElementById('sidebar-unread').textContent = totalUnread > 0 ? '\u2709 ' + totalUnread + ' unread' : '';
  } catch(e) {}
}

function renderSidebar() {
  var list = document.getElementById('contact-list');
  var filtered = _sidebarContacts;
  // Tab filter
  if (_sidebarTab === 'dm') filtered = filtered.filter(function(c) { return !c.is_channel; });
  else if (_sidebarTab === 'channel') filtered = filtered.filter(function(c) { return c.is_channel; });
  // Search
  if (_sidebarSearch) {
    filtered = filtered.filter(function(c) {
      var searchable = (c.short_name + ' ' + (c.long_name || '') + ' ' + (c.last_message_text || '')).toLowerCase();
      return searchable.indexOf(_sidebarSearch) !== -1;
    });
  }
  if (filtered.length === 0) {
    list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--lo-faint);font-size:10px;letter-spacing:0.1em;text-transform:uppercase">NO CONVERSATIONS YET</div>';
    return;
  }
  list.innerHTML = filtered.map(function(c) {
    var shortId = c.short_name || c.id.slice(-6);
    var avatarChars = shortId.replace(/^!/, '').slice(0, 2).toUpperCase();
    var proto = (c.protocol === 'meshcore' || c.protocol === 'mc') ? 'mc' : 'mt';
    var name = c.long_name ? (c.short_name + ' \u00b7 ' + c.long_name) : c.short_name;
    var preview = c.last_message_text ? escapeHtml(c.last_message_text).substring(0, 40) : '';
    var timeStr = c.last_heard ? relativeTime(c.last_heard) : '';
    var selected = _selectedThread === c.id ? ' selected' : '';
    var unread = (c.unread_count > 0) ?
      '<div class="lo-unread-badge">' + (c.unread_count > 99 ? '99+' : c.unread_count) + '</div>' : '';
    return '<div class="lo-contact' + selected + '" onclick="openThread(\'' + escapeHtml(c.id) + '\')">' +
      '<div class="lo-avatar">' + avatarChars + '<div class="lo-proto ' + proto + '"></div></div>' +
      '<div class="lo-contact-info">' +
        '<div class="lo-contact-name">' + escapeHtml(name) + '</div>' +
        '<div class="lo-contact-preview">' + preview + '</div>' +
      '</div>' +
      '<div class="lo-contact-meta">' + unread +
        '<div class="lo-contact-time">' + timeStr + '</div>' +
      '</div></div>';
  }).join('');
}

// ─── Thread View ───────────────────────────────────────────────────────────

var _threadContact = null;
var _threadMessages = [];

async function openThread(contactId) {
  _selectedThread = contactId;
  renderSidebar();
  // Fetch thread
  try {
    var r = await fetch('/api/threads/' + encodeURIComponent(contactId));
    var d = await r.json();
    _threadContact = d.contact;
    _threadMessages = d.messages || [];
    // Mark as read
    fetch('/api/threads/' + encodeURIComponent(contactId) + '/open', {method: 'POST'});
    renderThread();
    // Update sidebar unread
    loadSidebar();
  } catch(e) {
    showToast('Failed to load thread', 'error');
  }
}

function renderThread() {
  if (!_threadContact) return;
  var empty = document.getElementById('thread-empty');
  var active = document.getElementById('thread-active');
  empty.style.display = 'none';
  active.style.display = 'flex';

  // Header
  var shortId = _threadContact.short_name || _threadContact.id.slice(-6);
  var avatarChars = shortId.replace(/^!/, '').slice(0, 2).toUpperCase();
  document.getElementById('thread-avatar').textContent = avatarChars;
  var name = _threadContact.long_name ? shortId + ' \u00b7 ' + _threadContact.long_name : shortId;
  document.getElementById('thread-name').textContent = name;

  var metaParts = [];
  var proto = (_threadContact.protocol === 'meshcore' || _threadContact.protocol === 'mc') ? 'MC' : 'MT';
  metaParts.push(proto);
  if (_threadContact.last_hops !== null && _threadContact.last_hops !== undefined) metaParts.push(_threadContact.last_hops + ' hops');
  if (_threadContact.last_rssi) metaParts.push(_threadContact.last_rssi + ' dBm');
  if (_threadContact.last_heard) metaParts.push('heard ' + relativeTime(_threadContact.last_heard));
  document.getElementById('thread-meta').textContent = metaParts.join(' \u00b7 ');

  // AI toggle
  var aiBtn = document.getElementById('thread-ai-toggle');
  var aiVal = _threadContact.ai_enabled;
  if (aiVal === 1) { aiBtn.textContent = 'AI: ON'; aiBtn.className = 'lo-ai-toggle on'; }
  else if (aiVal === 0) { aiBtn.textContent = 'AI: OFF'; aiBtn.className = 'lo-ai-toggle'; }
  else { aiBtn.textContent = 'AI: INHERIT'; aiBtn.className = 'lo-ai-toggle'; }

  // Messages
  var el = document.getElementById('thread-messages');
  if (_threadMessages.length === 0) {
    el.innerHTML = '<div style="padding:20px;text-align:center;color:var(--lo-faint);font-size:10px;text-transform:uppercase;letter-spacing:0.1em">NO MESSAGES YET</div>';
  } else {
    el.innerHTML = _threadMessages.map(function(m) {
      var arrowClass = m.direction === 'in' ? 'in' : (m.author === 'ai' ? 'ai' : 'out');
      var arrow = m.direction === 'in' ? '\u2190' : '\u2192';
      var aiBadge = m.author === 'ai' ? '<span class="lo-tmsg-ai-badge">AI</span>' : '';
      var tierTag = (m.tier && m.author === 'ai') ? '<span class="lo-tmsg-ai-badge" style="border-color:var(--lo-dim)">' + m.tier.toUpperCase() + '</span>' : '';
      var channelHint = '';
      if (m.originating_channel_id) {
        channelHint = '<div style="font-size:9px;color:var(--lo-faint);margin-bottom:2px">\u2196 replying to channel message</div>';
      }
      return '<div class="lo-tmsg">' +
        '<span class="lo-tmsg-time">' + formatTime(m.timestamp) + '</span>' +
        '<span class="lo-tmsg-arrow ' + arrowClass + '">' + arrow + '</span>' +
        '<span class="lo-tmsg-body">' + channelHint + escapeHtml(m.text) + aiBadge + tierTag + '</span>' +
        '</div>';
    }).join('');
    el.scrollTop = el.scrollHeight;
  }

  // Composer placeholder
  document.getElementById('thread-input').placeholder = 'type a message to ' + shortId + '...';
}

async function threadSend() {
  if (!_selectedThread) return;
  var input = document.getElementById('thread-input');
  var text = input.value.trim();
  if (!text || text.length > 233) return;
  input.value = '';
  updateCharCount();
  document.getElementById('thread-send-btn').disabled = true;
  try {
    await callApi('POST', '/api/threads/' + encodeURIComponent(_selectedThread) + '/send', {text: text});
    // Refresh thread
    await openThread(_selectedThread);
  } catch(e) {
    showToast('Send failed', 'error');
  }
  document.getElementById('thread-send-btn').disabled = false;
  input.focus();
}

async function toggleThreadAi() {
  if (!_selectedThread) return;
  var d = await callApi('POST', '/api/threads/' + encodeURIComponent(_selectedThread) + '/ai-toggle');
  if (d) {
    _threadContact.ai_enabled = d.ai_enabled;
    renderThread();
  }
}

function updateCharCount() {
  var input = document.getElementById('thread-input');
  var el = document.getElementById('thread-char-count');
  var len = input.value.length;
  el.textContent = len + ' / 233';
  el.className = 'lo-char-count' + (len > 233 ? ' over' : len > 210 ? ' warn' : '');
  document.getElementById('thread-send-btn').disabled = len > 233;
}

// ─── Messenger Poll Integration ────────────────────────────────────────────

// Refresh sidebar every poll when messenger is active
var _lastSidebarRefresh = 0;
function pollMessenger() {
  if (App_view !== 'messenger') return;
  var now = Date.now();
  if (now - _lastSidebarRefresh > 3000) {
    _lastSidebarRefresh = now;
    loadSidebar();
    // Refresh current thread if open
    if (_selectedThread) {
      fetch('/api/threads/' + encodeURIComponent(_selectedThread))
        .then(function(r) { return r.json(); })
        .then(function(d) {
          if (d.messages) {
            var oldLen = _threadMessages.length;
            _threadMessages = d.messages;
            _threadContact = d.contact;
            if (d.messages.length !== oldLen) renderThread();
          }
        }).catch(function() {});
    }
  }
}

// ─── Init ──────────────────────────────────────────────────────────────────

loadLastBleDevice();
loadSidebar();
poll();
setInterval(poll, 2000);
</script>
</body>
</html>"""
