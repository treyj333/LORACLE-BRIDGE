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
    # Live connection check — don't trust the cached flag
    if _bridge:
        try:
            state["connected"] = _bridge._is_interface_alive()
        except Exception:
            state["connected"] = False
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
    # Build backends info from bridge's actual connection state
    backends_info = []
    if _bridge:
        try:
            backends_info = _bridge._radio_manager.get_backends_info()
        except Exception:
            pass
        # If RadioManager has no backends, report from bridge's own state
        if not backends_info:
            backends_info = [{
                "id": "mt-primary",
                "protocol": "mt",
                "transport": getattr(_bridge, "connection_type", "serial"),
                "connected": state.get("connected", False),
            }]
    state["backends"] = backends_info
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

    # BLE connections block for 10-30s — run in background thread
    # to avoid HTTP request timeout
    import threading as _threading
    def _do_switch():
        _bridge.switch_connection(conn_type, address=address, host=host, port=port)
    _threading.Thread(target=_do_switch, daemon=True).start()
    return jsonify({"ok": True, "status": "connecting"})


@app.route("/api/connection/disconnect", methods=["POST"])
def api_connection_disconnect():
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    _bridge.disconnect_radio()
    return jsonify({"ok": True})


@app.route("/api/radios", methods=["GET"])
def api_radios():
    """Return info about all active radio backends."""
    if _bridge is None:
        return jsonify({"backends": []})
    try:
        info = _bridge._radio_manager.get_backends_info()
    except Exception:
        info = []
    if not info:
        info = [{"id": "mt-primary", "protocol": "mt",
                 "transport": getattr(_bridge, "connection_type", "serial"),
                 "connected": _bridge._is_interface_alive()}]
    return jsonify({"backends": info})


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

    # Auto-create contact if not in DB yet
    contact = _bridge._contact_store.get(thread_id)
    if contact is None:
        try:
            short = thread_id[-6:] if len(thread_id) > 6 else thread_id
            _bridge._contact_store.upsert(
                contact_id=thread_id, protocol="meshtastic",
                backend_id=thread_id, short_name=short,
            )
            contact = _bridge._contact_store.get(thread_id)
        except Exception:
            pass
    if contact is None:
        return jsonify({"error": "Contact not found"}), 404

    try:
        # Send via the proven interface path (not RadioManager)
        is_channel = "channel:" in thread_id
        if is_channel:
            ch_num = int(thread_id.split(":")[-1])
            from meshtastic import BROADCAST_ADDR
            _bridge.interface.sendText(text, destinationId=BROADCAST_ADDR, channelIndex=ch_num, wantAck=False)
        else:
            _bridge.interface.sendText(text, destinationId=thread_id, wantAck=False)

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


@app.route("/api/factory-reset", methods=["POST"])
def api_factory_reset():
    """Delete all settings, messages, contacts, coverage — preserve CONTEXT FILES."""
    import shutil
    mesh_dir = os.path.join(os.path.expanduser("~"), ".mesh-llm")
    deleted = []
    # Files to delete
    for fname in ["loracle.db", "settings.json", "settings.json.bak",
                  "greeted_nodes.json", "ble_last_device.json", "coverage.jsonl"]:
        fpath = os.path.join(mesh_dir, fname)
        if os.path.exists(fpath):
            os.remove(fpath)
            deleted.append(fname)
    # Directories to delete (packs downloads, briefs, dead_drop)
    for dname in ["packs", "briefs"]:
        dpath = os.path.join(mesh_dir, dname)
        if os.path.isdir(dpath):
            shutil.rmtree(dpath, ignore_errors=True)
            deleted.append(dname + "/")
    # DB files (dead_drop.db, brief.db)
    for dbf in ["dead_drop.db", "brief.db"]:
        dbpath = os.path.join(mesh_dir, dbf)
        if os.path.exists(dbpath):
            os.remove(dbpath)
            deleted.append(dbf)
    logger.info(f"Factory reset: deleted {deleted}")
    return jsonify({"ok": True, "deleted": deleted,
                    "message": "Factory reset complete. Restart the bridge to apply."})


@app.route("/api/nodes/refresh", methods=["POST"])
def api_nodes_refresh():
    """Trigger an immediate nodeDB rescan to discover new nodes."""
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    try:
        _bridge._load_nodedb_positions()
        nodes = list(getattr(_bridge, "_known_nodes", set()))
        return jsonify({"ok": True, "node_count": len(nodes)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
<script src="/static/js/d3-dispatch.min.js"></script>
<script src="/static/js/d3-quadtree.min.js"></script>
<script src="/static/js/d3-timer.min.js"></script>
<script src="/static/js/d3-force.min.js"></script>
<style>
/* ── Font ─────────────────────────────────────────────────────────────────── */
@font-face { font-family: 'IBM Plex Mono'; font-style: normal; font-weight: 400; font-display: swap; src: url('/static/fonts/IBMPlexMono-Regular.ttf') format('truetype'); }
@font-face { font-family: 'IBM Plex Mono'; font-style: normal; font-weight: 500; font-display: swap; src: url('/static/fonts/IBMPlexMono-Medium.ttf') format('truetype'); }

/* ── Theme Tokens ─────────────────────────────────────────────────────────── */
:root, [data-theme="light"] {
  --lo-bg: #ebe6dc; --lo-bg-deep: #dcd5c6; --lo-ink: #1a1815; --lo-dim: #6b655a;
  --lo-faint: #9a948a; --lo-divider: rgba(26,24,21,0.08); --lo-divider-strong: rgba(26,24,21,0.18);
  --lo-accent: #ff4f00; --lo-accent-2: #0f6e56;
}
[data-theme="dark"] {
  --lo-bg: #121110; --lo-bg-deep: #1f1d1a; --lo-ink: #ede7d9; --lo-dim: #a39d92;
  --lo-faint: #6d675e; --lo-divider: rgba(237,231,217,0.12); --lo-divider-strong: rgba(237,231,217,0.22);
  --lo-accent: #ff4f00; --lo-accent-2: #5dcaa5;
}
/* Backward-compat aliases for addon CSS */
:root, [data-theme="light"], [data-theme="dark"] {
  --text-primary: var(--lo-ink); --text-secondary: var(--lo-dim); --text-muted: var(--lo-faint);
  --text-dim: var(--lo-faint); --bg-primary: var(--lo-bg); --bg-secondary: var(--lo-bg-deep);
  --bg-tertiary: var(--lo-bg-deep); --bg-input: var(--lo-bg-deep); --border: var(--lo-divider-strong);
  --border-subtle: var(--lo-divider); --border-width: 1px; --accent-blue: var(--lo-accent);
  --accent-green: var(--lo-accent-2); --accent-red: #c0392b; --accent-yellow: #d4a017;
  --accent-orange: var(--lo-accent); --shadow-raised: none; --shadow-inset: none;
  --font-mono: 'IBM Plex Mono', 'Menlo', 'Monaco', monospace;
}

/* ── Base ─────────────────────────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 12px; line-height: 1.7; }
body { font-family: var(--font-mono); font-weight: 400; font-variant-numeric: tabular-nums; background: var(--lo-bg); color: var(--lo-ink); -webkit-font-smoothing: antialiased; overflow: hidden; }
::selection { background: var(--lo-accent); color: #fff; }
button:focus, select:focus, input:focus, summary:focus { outline: none; }
button::-moz-focus-inner { border: 0; }

/* ── App Shell ────────────────────────────────────────────────────────────── */
.lo-app { display: flex; flex-direction: column; height: 100vh; width: 100vw; }

/* ── Title Bar ────────────────────────────────────────────────────────────── */
.lo-bar {
  display: flex; align-items: center; gap: 12px; padding: 10px 20px;
  background: var(--lo-bg-deep); border-bottom: 2px solid var(--lo-divider-strong);
  font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--lo-dim);
  flex-shrink: 0; z-index: 100;
}
.lo-bar .lo-brand { color: var(--lo-ink); font-weight: 500; font-size: 13px; letter-spacing: 0.15em; }
.lo-bar .lo-brand .lo-accent { color: var(--lo-accent); }
.lo-bar .lo-conn { display: flex; align-items: center; gap: 6px; }
.lo-bar .lo-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--lo-faint); transition: background 0.3s; }
.lo-bar .lo-dot.on { background: var(--lo-accent-2); animation: loPulse 2s ease-in-out infinite; }
.lo-bar .lo-filters { display: flex; margin-left: auto; }
.lo-bar .lo-filters button {
  padding: 4px 14px; font-family: inherit; font-size: 10px; letter-spacing: 0.1em;
  text-transform: uppercase; border: 1px solid var(--lo-divider-strong); border-right: none;
  background: none; color: var(--lo-dim); cursor: pointer; transition: background 0.15s, color 0.15s;
}
.lo-bar .lo-filters button:last-child { border-right: 1px solid var(--lo-divider-strong); }
.lo-bar .lo-filters button:hover { color: var(--lo-ink); }
.lo-bar .lo-filters button.active { background: var(--lo-ink); color: var(--lo-bg); }
.lo-bar .lo-tools { display: flex; gap: 4px; margin-left: 12px; }
.lo-bar .lo-tools button {
  background: none; border: none; cursor: pointer; color: var(--lo-dim);
  font-family: inherit; font-size: 13px; padding: 4px 8px; transition: color 0.15s;
}
.lo-bar .lo-tools button:hover { color: var(--lo-ink); }

/* ── Canvas Container ─────────────────────────────────────────────────────── */
.lo-canvas-wrap { position: relative; flex: 1; overflow: hidden; }
#mesh-canvas { width: 100%; height: 100%; display: block; }

/* ── HUD Stats (top-left overlay) ─────────────────────────────────────────── */
.lo-hud {
  position: absolute; top: 16px; left: 20px; z-index: 50;
  font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--lo-dim); line-height: 1.8; pointer-events: none;
}
.lo-hud-val { color: var(--lo-ink); font-weight: 500; font-size: 13px; }

/* ── Hop Rings Legend (bottom-left) ───────────────────────────────────────── */
.lo-hop-legend {
  position: absolute; bottom: 50px; left: 20px; z-index: 50;
  font-size: 9px; letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--lo-faint); pointer-events: none;
}

/* ── Floating Node Windows ─────────────────────────────────────────────────── */
.lo-float-win {
  position: absolute; z-index: 80;
  width: 320px; max-height: 400px;
  background: var(--lo-bg);
  border: 1px solid var(--lo-divider-strong);
  display: flex; flex-direction: column;
  font-size: 11px;
  pointer-events: auto;
}
.lo-float-win .lo-fw-header {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 12px 8px;
  border-bottom: 1px solid var(--lo-divider);
  cursor: grab;
}
.lo-float-win .lo-fw-header:active { cursor: grabbing; }
.lo-fw-name { font-size: 12px; font-weight: 500; color: var(--lo-ink); flex: 1; }
.lo-fw-close {
  background: none; border: none; font-size: 14px; color: var(--lo-dim);
  cursor: pointer; padding: 0 2px; line-height: 1;
}
.lo-fw-close:hover { color: var(--lo-ink); }
.lo-fw-meta {
  padding: 6px 12px; font-size: 9px; color: var(--lo-dim);
  letter-spacing: 0.06em; text-transform: uppercase;
  border-bottom: 1px solid var(--lo-divider);
}
.lo-fw-meta div { padding: 1px 0; }
.lo-fw-actions {
  display: flex; gap: 4px; padding: 6px 12px;
  border-bottom: 1px solid var(--lo-divider);
}
.lo-fw-actions button {
  background: none; border: 1px solid var(--lo-divider-strong); padding: 2px 8px;
  font-family: inherit; font-size: 8px; letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--lo-dim); cursor: pointer;
}
.lo-fw-actions button:hover { color: var(--lo-ink); border-color: var(--lo-ink); }
.lo-fw-actions button.on { color: var(--lo-accent); border-color: var(--lo-accent); }
.lo-fw-messages {
  flex: 1; overflow-y: auto; padding: 8px 12px; max-height: 180px; min-height: 40px;
}
.lo-fw-msg {
  display: grid; grid-template-columns: 42px 12px 1fr; gap: 3px;
  padding: 2px 0; align-items: baseline; font-size: 10px;
}
.lo-fw-msg-time { color: var(--lo-faint); font-size: 9px; }
.lo-fw-msg-arrow { text-align: center; font-size: 10px; }
.lo-fw-msg-arrow.in { color: var(--lo-dim); }
.lo-fw-msg-arrow.out { color: var(--lo-ink); }
.lo-fw-msg-arrow.ai { color: var(--lo-accent); }
.lo-fw-msg-body { color: var(--lo-ink); word-break: break-word; }
.lo-fw-empty { padding: 12px; text-align: center; color: var(--lo-faint); font-size: 9px; text-transform: uppercase; letter-spacing: 0.08em; }
.lo-fw-composer {
  display: flex; align-items: center; gap: 6px; padding: 8px 12px;
  border-top: 1px solid var(--lo-divider-strong);
}
.lo-fw-composer .lo-prompt { color: var(--lo-accent); font-size: 12px; font-weight: 500; }
.lo-fw-composer input {
  flex: 1; background: transparent; border: none; font-family: inherit; font-size: 11px;
  color: var(--lo-ink); caret-color: var(--lo-accent); outline: none; padding: 2px 0;
}
.lo-fw-composer input::placeholder { color: var(--lo-faint); }
.lo-fw-composer .lo-send {
  background: var(--lo-ink); color: var(--lo-bg); border: none; padding: 3px 10px;
  font-family: inherit; font-size: 9px; font-weight: 500; letter-spacing: 0.1em;
  text-transform: uppercase; cursor: pointer;
}
.lo-fw-composer .lo-send:disabled { opacity: 0.4; }

/* ── Activity Ribbon (bottom strip) ───────────────────────────────────────── */
.lo-ribbon {
  height: 36px; flex-shrink: 0; background: var(--lo-bg-deep);
  border-top: 1px solid var(--lo-divider-strong);
  display: flex; align-items: center; padding: 0 20px; gap: 12px;
  font-size: 9px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--lo-faint);
}
.lo-ribbon canvas { height: 20px; flex: 1; }

/* ── CONFIG Section (reuse existing styles) ───────────────────────────────── */
.lo-config { display: none; flex: 1; overflow-y: auto; padding: 0 24px 24px; }
.lo-config.active { display: block; }
.lo-section { border-bottom: 1px solid var(--lo-divider-strong); }
.lo-section > summary, .lo-section-head {
  display: flex; align-items: center; padding: 16px 0; font-size: 11px; font-weight: 500;
  letter-spacing: 0.1em; text-transform: uppercase; color: var(--lo-ink); cursor: pointer;
  list-style: none; user-select: none;
}
.lo-section > summary::-webkit-details-marker { display: none; }
.lo-section > summary::before, .lo-section-head::before { content: '\25B8'; margin-right: 10px; font-size: 9px; transition: transform 0.2s; color: var(--lo-dim); }
.lo-section[open] > summary::before { transform: rotate(90deg); }
.lo-section-body { padding: 0 0 20px; }
.lo-form-row { display: flex; align-items: center; gap: 12px; padding: 8px 0; flex-wrap: wrap; }
.lo-form-label { width: 140px; flex-shrink: 0; font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--lo-dim); }
.lo-form-control { flex: 1; min-width: 0; }
.lo-form-hint { font-size: 10px; color: var(--lo-faint); text-align: right; flex-shrink: 0; }
input[type="text"], input[type="number"], input[type="url"], select, textarea {
  background: transparent; border: 1px solid var(--lo-divider-strong); font-family: inherit;
  font-size: 12px; color: var(--lo-ink); padding: 6px 8px; outline: none; width: 100%;
}
textarea { resize: vertical; min-height: 80px; }
input[type="checkbox"] { accent-color: var(--lo-accent-2); }
.btn {
  background: transparent; border: 1px solid var(--lo-divider-strong);
  font-family: inherit; font-size: 10px; font-weight: 500; letter-spacing: 0.1em;
  text-transform: uppercase; color: var(--lo-dim); padding: 5px 12px; cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
.btn:hover { color: var(--lo-ink); border-color: var(--lo-ink); }
.btn:disabled { opacity: 0.4; cursor: default; }
.btn-primary { background: var(--lo-ink); color: var(--lo-bg); border-color: var(--lo-ink); }
.btn-sm { padding: 3px 8px; font-size: 9px; }

/* ── Toast ────────────────────────────────────────────────────────────────── */
#toast-container { position: fixed; bottom: 50px; right: 20px; z-index: 3000; display: flex; flex-direction: column; gap: 6px; }
.toast { padding: 8px 14px; font-family: var(--font-mono); font-size: 11px; color: var(--lo-ink); background: var(--lo-bg-deep); border: 1px solid var(--lo-divider-strong); border-left: 3px solid var(--lo-accent-2); max-width: 320px; transition: opacity 0.3s; }
.toast-error { border-left-color: #c0392b; }
.toast.fade-out { opacity: 0; }

/* ── Connect Modal ────────────────────────────────────────────────────────── */
.lo-connect-modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 1500; align-items: center; justify-content: center; }
.lo-connect-modal.open { display: flex; }
.lo-connect-box { background: var(--lo-bg); border: 2px solid var(--lo-divider-strong); width: 420px; max-width: 90vw; padding: 32px; }
.lo-connect-box h3 { font-size: 14px; font-weight: 500; letter-spacing: 0.12em; text-transform: uppercase; color: var(--lo-ink); margin-bottom: 6px; }
.lo-connect-box h3::before { content: '\25C9 '; color: var(--lo-accent); }
.lo-connect-box p { font-size: 11px; color: var(--lo-dim); margin-bottom: 20px; line-height: 1.7; }
.lo-connect-box .lo-form-row { padding: 6px 0; }
.lo-scan-device { display: flex; align-items: center; gap: 10px; padding: 10px 8px; border-bottom: 1px solid var(--lo-divider); cursor: pointer; border-left: 3px solid transparent; transition: background 0.1s; }
.lo-scan-device:hover { background: var(--lo-bg-deep); }
.lo-scan-device.selected { background: var(--lo-bg-deep); border-left-color: var(--lo-accent); }

/* ── Help Popover ─────────────────────────────────────────────────────────── */
.lo-help { display: none; position: fixed; top: 48px; right: 20px; width: 320px; background: var(--lo-bg-deep); border: 1px solid var(--lo-divider-strong); padding: 16px; z-index: 1000; font-size: 11px; color: var(--lo-ink); line-height: 1.6; }
.lo-help.open { display: block; }
.lo-help h4 { font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--lo-dim); margin-bottom: 10px; }
.lo-help p { margin-bottom: 8px; }
.lo-help code { background: var(--lo-divider); padding: 1px 4px; font-size: 11px; }

/* ── Onboarding ───────────────────────────────────────────────────────────── */
.lo-onboarding { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 2000; align-items: center; justify-content: center; }
.lo-onboarding.open { display: flex; }
.lo-onboarding-box { background: var(--lo-bg); border: 1px solid var(--lo-divider-strong); width: 520px; max-width: 90vw; padding: 24px; }
.lo-ob-progress { display: flex; gap: 8px; justify-content: center; margin-bottom: 16px; font-size: 14px; }
.lo-ob-dot { color: var(--lo-divider-strong); } .lo-ob-dot.done { color: var(--lo-ink); } .lo-ob-dot.active { color: var(--lo-accent); }
.lo-ob-step { display: none; text-align: center; } .lo-ob-step.active { display: block; }
.lo-ob-step svg { width: 100%; max-width: 360px; height: 120px; margin: 0 auto 16px; }
.lo-ob-step h3 { font-size: 14px; font-weight: 500; color: var(--lo-ink); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.05em; }
.lo-ob-step p { font-size: 12px; color: var(--lo-dim); line-height: 1.7; max-width: 400px; margin: 0 auto; }
.lo-ob-nav { display: flex; align-items: center; justify-content: space-between; margin-top: 20px; padding-top: 14px; border-top: 1px solid var(--lo-divider); }
.lo-ob-skip { background: none; border: none; font-family: inherit; font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--lo-faint); cursor: pointer; }

/* ── Animations ───────────────────────────────────────────────────────────── */
@keyframes loPulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.5; transform: scale(0.9); } }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; transition-duration: 0.01ms !important; } }
</style>
</head>
<body>
<div class="lo-app">

<!-- ── Title Bar ───────────────────────────────────────────────────────────── -->
<header class="lo-bar">
  <span class="lo-brand"><span class="lo-accent">LORACLE</span> BRIDGE</span>
  <span class="lo-conn">
    <span class="lo-dot" id="hdr-dot"></span>
    <span id="hdr-conn-label">DISCONNECTED</span>
  </span>
  <div class="lo-filters">
    <button class="active" data-view="mesh" onclick="setView('mesh')">MESH</button>
    <button data-view="traffic" onclick="setView('traffic')">TRAFFIC</button>
    <button data-view="config" onclick="setView('config')">CONFIG</button>
  </div>
  <div class="lo-tools">
    <button id="help-toggle" title="Help">?</button>
    <button id="theme-toggle" title="Toggle theme">&#9681;</button>
  </div>
</header>

<!-- ── Canvas ──────────────────────────────────────────────────────────────── -->
<div class="lo-canvas-wrap" id="canvas-wrap">
  <canvas id="mesh-canvas"></canvas>

  <!-- HUD Stats -->
  <div class="lo-hud" id="hud">
    <div><span class="lo-hud-val" id="hud-nodes">0</span> NODES</div>
    <div><span class="lo-hud-val" id="hud-msgs">0</span> MESSAGES</div>
    <div><span class="lo-hud-val" id="hud-model">--</span> MODEL</div>
    <div><span class="lo-hud-val" id="hud-uptime">0s</span> UPTIME</div>
    <div style="margin-top:8px"><button class="btn btn-sm" onclick="refreshNodes()" id="hud-refresh-btn" style="pointer-events:auto">SCAN MESH</button></div>
  </div>

  <!-- Hop ring legend -->
  <div class="lo-hop-legend" id="hop-legend"></div>

  <!-- Floating node windows rendered here by JS -->
  <div id="float-windows" style="position:absolute;inset:0;pointer-events:none;z-index:80"></div>
</div>

<!-- ── Activity Ribbon ────────────────────────────────────────────────────── -->
<div class="lo-ribbon">
  <span id="ribbon-label">PACKETS</span>
  <canvas id="ribbon-canvas"></canvas>
  <span id="ribbon-stats"></span>
</div>

<!-- ── CONFIG View ─────────────────────────────────────────────────────────── -->
<div class="lo-config" id="config-view">

  <!-- Connection -->
  <details class="lo-section" open>
    <summary class="lo-section-head">CONNECTION</summary>
    <div class="lo-section-body">
      <div class="lo-form-row">
        <span class="lo-form-label">STATUS</span>
        <span style="display:flex;align-items:center;gap:6px">
          <span class="lo-dot" id="cfg-conn-dot"></span>
          <span id="cfg-conn-status">Disconnected</span>
        </span>
        <span class="lo-form-hint" id="cfg-conn-detail"></span>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label">TYPE</span>
        <select id="cfg-conn-type" style="max-width:160px"><option value="serial">Serial (USB)</option><option value="tcp">TCP</option><option value="ble">BLE</option></select>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label">ADDRESS</span>
        <input type="text" id="cfg-conn-addr" placeholder="auto-detect">
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label"></span>
        <div style="display:flex;gap:6px">
          <button class="btn btn-primary" onclick="cfgConnect()">CONNECT</button>
          <button class="btn" id="cfg-disconn-btn" onclick="cfgDisconnect()" style="display:none">DISCONNECT</button>
        </div>
      </div>
    </div>
  </details>

  <!-- AI Replies -->
  <details class="lo-section">
    <summary class="lo-section-head">AI REPLIES</summary>
    <div class="lo-section-body">
      <div class="lo-form-row">
        <span class="lo-form-label">AUTO-REPLY</span>
        <label style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="cfg-ai-replies" checked onchange="cfgToggleAiReplies(this.checked)"> Enabled</label>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label"></span>
        <span style="font-size:10px;color:var(--lo-faint)">When on, LORACLE answers incoming messages. When off, it logs but stays quiet.</span>
      </div>
    </div>
  </details>

  <!-- Model -->
  <details class="lo-section">
    <summary class="lo-section-head">MODEL</summary>
    <div class="lo-section-body">
      <div class="lo-form-row">
        <span class="lo-form-label">CURRENT</span>
        <span id="cfg-model-cur" style="color:var(--lo-ink)">--</span>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label">SWITCH TO</span>
        <select id="cfg-model-sel" style="max-width:240px"></select>
        <button class="btn btn-sm" onclick="cfgSwitchModel()">APPLY</button>
        <button class="btn btn-sm" onclick="cfgRefreshModels()">REFRESH</button>
      </div>
    </div>
  </details>

  <!-- Model Routing -->
  <details class="lo-section">
    <summary class="lo-section-head">MODEL ROUTING</summary>
    <div class="lo-section-body">
      <div class="lo-form-row">
        <span class="lo-form-label">AUTO-ROUTING</span>
        <label style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="cfg-routing-auto" checked onchange="cfgSetRouting('auto',this.checked)"> Enabled</label>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label">SHOW TIER TAG</span>
        <label style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="cfg-routing-tag" checked onchange="cfgSetRouting('tag',this.checked)"> On AI messages</label>
      </div>
      <div style="margin-top:10px;border-top:1px solid var(--lo-divider);padding-top:10px">
        <div class="lo-form-row"><span class="lo-form-label">TIER: TINY</span><input type="text" id="cfg-tier-tiny" value="gemma3:4b" style="max-width:160px"><label style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="cfg-tier-tiny-on" checked> On</label></div>
        <div class="lo-form-row"><span class="lo-form-label">TIER: STANDARD</span><input type="text" id="cfg-tier-std" value="qwen3:8b" style="max-width:160px"><label style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="cfg-tier-std-on" checked> On</label></div>
        <div class="lo-form-row"><span class="lo-form-label">TIER: BIG</span><input type="text" id="cfg-tier-big" value="phi4:14b" style="max-width:160px"><label style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="cfg-tier-big-on"> On</label></div>
        <div class="lo-form-row"><span class="lo-form-label"></span><button class="btn btn-sm" onclick="cfgSaveTiers()">SAVE TIERS</button></div>
      </div>
      <div style="margin-top:10px;border-top:1px solid var(--lo-divider);padding-top:10px">
        <div class="lo-form-row">
          <span class="lo-form-label">TEST CLASSIFIER</span>
          <div class="lo-form-control"><input type="text" id="cfg-test-q" placeholder="type a query to test..." oninput="cfgTestClassifier(this.value)"><div id="cfg-test-result" style="font-size:10px;color:var(--lo-dim);margin-top:4px"></div></div>
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
        <input type="range" id="cfg-max-len" min="50" max="1000" value="200" oninput="document.getElementById('cfg-max-len-val').textContent=this.value;markConfigDirty()" style="flex:1">
        <span class="lo-form-hint" id="cfg-max-len-val">200</span>
      </div>
      <div class="lo-form-row">
        <span class="lo-form-label">COMPRESSION</span>
        <label style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="cfg-compression"> Enabled</label>
      </div>
      <div class="lo-form-row" style="align-items:flex-start">
        <span class="lo-form-label">SYSTEM PROMPT</span>
        <div class="lo-form-control">
          <textarea id="cfg-prompt" rows="4" oninput="markConfigDirty()"></textarea>
          <div style="display:flex;justify-content:space-between;margin-top:4px">
            <span style="font-size:10px;color:var(--lo-faint)" id="cfg-prompt-count"></span>
            <button class="btn btn-sm" onclick="cfgSavePrompt()">SAVE PROMPT</button>
          </div>
        </div>
      </div>
      <div class="lo-form-row"><span class="lo-form-label"></span><button class="btn" onclick="cfgApplySettings()">APPLY SETTINGS</button></div>
    </div>
  </details>

  <!-- Knowledge Base -->
  <details class="lo-section">
    <summary class="lo-section-head">KNOWLEDGE BASE</summary>
    <div class="lo-section-body">
      <div class="lo-form-row"><span class="lo-form-label">RAG</span><label style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="cfg-rag-toggle" checked onchange="cfgToggleRag(this.checked)"> Enabled</label><span class="lo-form-hint" id="cfg-rag-stats"></span></div>
      <div class="lo-form-row"><span class="lo-form-label">ADD URL</span><div class="lo-form-control"><div style="display:flex;gap:4px"><input type="url" id="cfg-url-input" placeholder="https://..."><button class="btn btn-sm" onclick="cfgIngestUrl()">INGEST</button></div><div id="cfg-url-status" style="font-size:10px;margin-top:4px"></div></div></div>
      <div class="lo-form-row"><span class="lo-form-label">UPLOAD FILE</span><input type="file" id="cfg-file-upload" onchange="cfgUploadFile()" style="font-size:11px"></div>
      <div class="lo-form-row" style="align-items:flex-start"><span class="lo-form-label">DOCUMENTS</span><div class="lo-form-control" id="cfg-rag-docs"><span style="color:var(--lo-faint)">Loading...</span></div></div>
    </div>
  </details>

  <!-- Knowledge Packs -->
  <details class="lo-section">
    <summary class="lo-section-head">KNOWLEDGE PACKS</summary>
    <div class="lo-section-body">
      <div id="cfg-packs-list"><span style="color:var(--lo-faint);font-size:10px">Loading...</span></div>
      <div id="cfg-pack-detail" style="display:none;margin-top:12px;padding:12px 0;border-top:1px solid var(--lo-divider)"><div id="cfg-pack-detail-content"></div></div>
    </div>
  </details>

  <!-- Data & Storage -->
  <details class="lo-section">
    <summary class="lo-section-head">DATA & STORAGE</summary>
    <div class="lo-section-body">
      <div class="lo-form-row"><span class="lo-form-label">DATABASE</span><span style="color:var(--lo-dim);font-size:10px">~/.mesh-llm/loracle.db</span></div>
      <div class="lo-form-row"><span class="lo-form-label">STATS</span><span id="cfg-db-stats" style="color:var(--lo-dim);font-size:10px">Loading...</span></div>
      <div class="lo-form-row"><span class="lo-form-label">RETENTION</span><span style="color:var(--lo-faint);font-size:10px">Last 500 messages OR 90 days per contact</span></div>
      <div class="lo-form-row"><span class="lo-form-label"></span><div style="display:flex;gap:6px"><button class="btn btn-sm" onclick="cfgPruneNow()">PRUNE NOW</button><button class="btn btn-sm" onclick="cfgClearAllMessages()" style="color:#c0392b;border-color:#c0392b">CLEAR ALL MESSAGES</button></div></div>
      <div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--lo-divider)">
        <div class="lo-form-row"><span class="lo-form-label">FACTORY RESET</span><div><button class="btn btn-sm" onclick="cfgFactoryReset()" style="color:#c0392b;border-color:#c0392b">RESET ALL SETTINGS</button><div style="font-size:9px;color:var(--lo-faint);margin-top:4px">Erases all data. CONTEXT FILES/ preserved.</div></div></div>
      </div>
    </div>
  </details>

  <!-- Appearance -->
  <details class="lo-section">
    <summary class="lo-section-head">APPEARANCE</summary>
    <div class="lo-section-body">
      <div class="lo-form-row"><span class="lo-form-label">THEME</span><select id="cfg-theme" onchange="setTheme(this.value)" style="max-width:120px"><option value="light">Light</option><option value="dark">Dark</option></select></div>
      <div class="lo-form-row"><span class="lo-form-label">ONBOARDING</span><button class="btn btn-sm" onclick="showOnboarding()">LAUNCH TOUR</button></div>
    </div>
  </details>

  <!-- About -->
  <details class="lo-section">
    <summary class="lo-section-head">ABOUT</summary>
    <div class="lo-section-body">
      <div class="lo-form-row"><span class="lo-form-label">VERSION</span><span style="color:var(--lo-ink)">LORACLE Bridge v1.0</span></div>
      <div class="lo-form-row"><span class="lo-form-label">UPTIME</span><span id="cfg-uptime" style="color:var(--lo-ink)">--</span></div>
    </div>
  </details>

  <!-- ADDON_SECTIONS -->

</div>

<!-- ── Connect Modal ──────────────────────────────────────────────────────── -->
<div class="lo-connect-modal" id="connect-modal">
  <div class="lo-connect-box">
    <h3>CONNECT A RADIO</h3>
    <p>No radio detected. Plug in a USB radio, or scan for nearby Bluetooth devices.</p>
    <div class="lo-form-row">
      <span class="lo-form-label">PROTOCOL</span>
      <select id="connect-protocol" style="max-width:140px">
        <option value="auto">Auto-detect</option>
        <option value="meshtastic">Meshtastic</option>
        <option value="meshcore">MeshCore</option>
      </select>
    </div>
    <div class="lo-form-row">
      <span class="lo-form-label">TRANSPORT</span>
      <select id="connect-type" style="max-width:140px" onchange="connectModalTypeChanged()">
        <option value="ble" selected>Bluetooth (BLE)</option>
        <option value="serial">Serial (USB)</option>
        <option value="tcp">TCP</option>
      </select>
    </div>
    <div class="lo-form-row" id="connect-address-row" style="display:none">
      <span class="lo-form-label">ADDRESS</span>
      <input type="text" id="connect-address" placeholder="auto-detect (or /dev/...)">
    </div>
    <div class="lo-form-row" id="connect-scan-row">
      <span class="lo-form-label">DEVICES</span>
      <div style="flex:1">
        <button class="btn btn-sm" id="connect-scan-btn" onclick="connectModalScan()">SCAN FOR DEVICES</button>
        <div id="connect-scan-status" style="font-size:10px;color:var(--lo-dim);margin-top:6px"></div>
        <div id="connect-scan-list" style="margin-top:6px"></div>
      </div>
    </div>
    <div class="lo-form-row" style="justify-content:space-between;margin-top:8px">
      <button class="btn" onclick="dismissConnectModal()">DISMISS</button>
      <button class="btn btn-primary" id="connect-modal-btn" onclick="connectFromModal()">CONNECT</button>
    </div>
    <div id="connect-modal-status" style="font-size:10px;color:var(--lo-dim);margin-top:8px"></div>
  </div>
</div>

<!-- ── Help ────────────────────────────────────────────────────────────────── -->
<div class="lo-help" id="help-popover">
  <h4>QUICK REFERENCE</h4>
  <p><strong>Click a node</strong> to see its info and send a message.</p>
  <p><strong>Hexagon nodes</strong> are public channels.</p>
  <p><strong>Ring distance</strong> = hop count from your radio.</p>
  <p><strong>Line thickness</strong> = signal strength.</p>
  <p><strong>Pulsing dots</strong> = active packet traffic.</p>
</div>

<!-- ── Onboarding ─────────────────────────────────────────────────────────── -->
<div class="lo-onboarding" id="onboarding">
  <div class="lo-onboarding-box">
    <div class="lo-ob-progress" id="ob-progress"></div>
    <div class="lo-ob-step active" data-step="0">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <circle cx="180" cy="60" r="6" fill="var(--lo-accent)"/>
        <circle cx="180" cy="60" r="6" fill="none" stroke="var(--lo-accent)" stroke-width="1" opacity="0.7"><animate attributeName="r" values="6;28" dur="2.8s" repeatCount="indefinite"/><animate attributeName="opacity" values="0.7;0" dur="2.8s" repeatCount="indefinite"/></circle>
        <circle cx="80" cy="30" r="3" fill="var(--lo-accent-2)"><animate attributeName="r" values="3;4.2" dur="3.2s" repeatCount="indefinite"/></circle>
        <circle cx="280" cy="25" r="3" fill="var(--lo-accent-2)"><animate attributeName="r" values="3;4.2" dur="3.2s" begin="0.6s" repeatCount="indefinite"/></circle>
        <circle cx="60" cy="90" r="3" fill="var(--lo-accent-2)"><animate attributeName="r" values="3;4.2" dur="3.2s" begin="1.2s" repeatCount="indefinite"/></circle>
        <circle cx="300" cy="95" r="3" fill="var(--lo-accent-2)"><animate attributeName="r" values="3;4.2" dur="3.2s" begin="1.8s" repeatCount="indefinite"/></circle>
      </svg>
      <h3>THE MESH IS YOUR INTERFACE</h3>
      <p>Every dot is a radio. Every line is a connection. Click any node to message it or see its info.</p>
    </div>
    <div class="lo-ob-step" data-step="1">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <circle cx="180" cy="60" r="4" fill="var(--lo-accent)"/>
        <circle cx="120" cy="60" r="3" fill="var(--lo-accent-2)"/><line x1="180" y1="60" x2="120" y2="60" stroke="var(--lo-accent-2)" stroke-width="1" opacity="0.5"/>
        <circle cx="60" cy="60" r="3" fill="var(--lo-accent-2)"/><line x1="120" y1="60" x2="60" y2="60" stroke="var(--lo-accent-2)" stroke-width="1" opacity="0.5"/>
        <text x="120" y="80" text-anchor="middle" fill="var(--lo-faint)" font-family="var(--font-mono)" font-size="7">1H</text>
        <text x="60" y="80" text-anchor="middle" fill="var(--lo-faint)" font-family="var(--font-mono)" font-size="7">2H</text>
      </svg>
      <h3>DISTANCE = HOPS</h3>
      <p>Closer nodes are direct peers. Farther nodes are more hops away. The mesh handles routing automatically.</p>
    </div>
    <div class="lo-ob-step" data-step="2">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <rect x="140" y="30" width="80" height="60" rx="0" fill="none" stroke="var(--lo-divider-strong)" stroke-width="1"/>
        <text x="180" y="55" text-anchor="middle" fill="var(--lo-dim)" font-family="var(--font-mono)" font-size="7">NODE INFO</text>
        <text x="180" y="70" text-anchor="middle" fill="var(--lo-faint)" font-family="var(--font-mono)" font-size="6">&gt; type a message...</text>
      </svg>
      <h3>CLICK TO CHAT</h3>
      <p>Click any node to open its panel. See signal info, hop count, and send messages directly.</p>
    </div>
    <div class="lo-ob-step" data-step="3">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <polygon points="180,40 200,52 200,68 180,80 160,68 160,52" fill="none" stroke="var(--lo-accent-2)" stroke-width="1.5"/>
        <text x="180" y="64" text-anchor="middle" fill="var(--lo-accent-2)" font-family="var(--font-mono)" font-size="7">CH 0</text>
      </svg>
      <h3>CHANNELS ARE HEXAGONS</h3>
      <p>Public channels appear as hexagon nodes. Click to read channel traffic or broadcast to everyone in range.</p>
    </div>
    <div class="lo-ob-step" data-step="4">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <circle cx="180" cy="60" r="6" fill="var(--lo-accent)"/>
        <circle cx="80" cy="30" r="3" fill="var(--lo-accent-2)"/><circle cx="280" cy="25" r="3" fill="var(--lo-accent-2)"/>
        <circle cx="60" cy="90" r="3" fill="var(--lo-accent-2)"/><circle cx="300" cy="95" r="3" fill="var(--lo-accent-2)"/>
        <circle cx="120" cy="105" r="3" fill="var(--lo-accent-2)"/><circle cx="250" cy="100" r="3" fill="var(--lo-accent-2)"/>
        <line x1="180" y1="60" x2="80" y2="30" stroke="var(--lo-accent-2)" stroke-width="0.5" opacity="0.4"/>
        <line x1="180" y1="60" x2="280" y2="25" stroke="var(--lo-accent-2)" stroke-width="0.5" opacity="0.4"/>
        <line x1="180" y1="60" x2="60" y2="90" stroke="var(--lo-accent-2)" stroke-width="0.5" opacity="0.4"/>
        <line x1="180" y1="60" x2="300" y2="95" stroke="var(--lo-accent-2)" stroke-width="0.5" opacity="0.4"/>
      </svg>
      <h3>YOUR MESH IS ALIVE</h3>
      <p id="ob-live-stats">Nodes breathe, packets pulse along links. The UI's energy matches your network's activity.</p>
    </div>
    <div class="lo-ob-nav">
      <button class="btn" id="ob-prev" onclick="obPrev()">PREV</button>
      <button class="lo-ob-skip" onclick="obSkip()">SKIP</button>
      <button class="btn btn-primary" id="ob-next" onclick="obNext()">NEXT</button>
    </div>
  </div>
</div>

<div id="toast-container"></div>

</div><!-- .lo-app -->

<!-- ADDON_SECTIONS marker kept for addon tab injection compat -->
<!-- ADDON_SECTIONS -->

<script>
// ─── State ─────────────────────────────────────────────────────────────────

var App = {
  state: {},
  view: 'mesh',        // 'mesh' | 'traffic' | 'config'
  selectedNode: null,
  configLoaded: false,
  nodes: [],
  links: [],
  simulation: null,
  canvas: null,
  ctx: null,
  width: 0,
  height: 0,
  animFrame: null,
  breathPhase: 0,
  packets: [],
  ribbonData: [],
};

// ─── Utilities ─────────────────────────────────────────────────────────────

function escapeHtml(s) { return s ? String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') : ''; }
function formatUptime(s) { var h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60; return h>0?h+'h '+m+'m':m>0?m+'m '+sec+'s':sec+'s'; }
function formatTime(ts) { return new Date(ts*1000).toLocaleTimeString(); }
function relativeTime(ts) { var d=Math.floor(Date.now()/1000-ts); return d<10?'now':d<60?d+'s':d<3600?Math.floor(d/60)+'m':d<86400?Math.floor(d/3600)+'h':Math.floor(d/86400)+'d'; }

function showToast(message, type) {
  type = type || 'info';
  var c = document.getElementById('toast-container'), t = document.createElement('div');
  t.className = 'toast toast-' + type; t.textContent = message; c.appendChild(t);
  setTimeout(function() { t.classList.add('fade-out'); }, 9500);
  setTimeout(function() { if (t.parentNode) c.removeChild(t); }, 10000);
}

async function callApi(method, url, body) {
  try {
    var opts = { method: method, headers: {'Content-Type': 'application/json'} };
    if (body) opts.body = JSON.stringify(body);
    var r = await fetch(url, opts); var data = await r.json();
    if (!r.ok) { showToast(data.error || 'Request failed', 'error'); return null; }
    return data;
  } catch(e) { showToast('Network error', 'error'); return null; }
}

// ─── Theme ─────────────────────────────────────────────────────────────────

function setTheme(theme) { document.documentElement.setAttribute('data-theme', theme); localStorage.setItem('loracle-theme', theme); }
(function() { var s = localStorage.getItem('loracle-theme'); if (s) setTheme(s); else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) setTheme('dark'); })();

// ─── View Switching ────────────────────────────────────────────────────────

var _configDirty = false;

function markConfigDirty() { _configDirty = true; }

function setView(view) {
  // Warn if leaving CONFIG with unsaved changes
  if (App.view === 'config' && view !== 'config' && _configDirty) {
    if (!confirm('You have unsaved changes in CONFIG. Leave without applying?')) return;
    _configDirty = false;
  }
  App.view = view;
  document.querySelectorAll('.lo-filters button').forEach(function(b) { b.classList.toggle('active', b.dataset.view === view); });
  document.getElementById('canvas-wrap').style.display = (view === 'config') ? 'none' : '';
  document.querySelector('.lo-ribbon').style.display = (view === 'config') ? 'none' : '';
  document.getElementById('config-view').classList.toggle('active', view === 'config');
  document.getElementById('hud').style.display = (view === 'config') ? 'none' : '';
  if (view === 'config' && !App.configLoaded) { App.configLoaded = true; loadConfigData(); }
  if (view !== 'config') resizeCanvas();
}

// ─── Canvas Setup ──────────────────────────────────────────────────────────

function initCanvas() {
  App.canvas = document.getElementById('mesh-canvas');
  App.ctx = App.canvas.getContext('2d');
  resizeCanvas();
  window.addEventListener('resize', resizeCanvas);
  App.canvas.addEventListener('click', onCanvasClick);
  requestAnimationFrame(renderLoop);
}

function resizeCanvas() {
  var wrap = document.getElementById('canvas-wrap');
  App.width = wrap.clientWidth; App.height = wrap.clientHeight;
  App.canvas.width = App.width * (window.devicePixelRatio || 1);
  App.canvas.height = App.height * (window.devicePixelRatio || 1);
  App.canvas.style.width = App.width + 'px';
  App.canvas.style.height = App.height + 'px';
  App.ctx.setTransform(window.devicePixelRatio || 1, 0, 0, window.devicePixelRatio || 1, 0, 0);
}

// ─── Force Simulation ──────────────────────────────────────────────────────

var _prevNodeIds = '';  // track whether node set changed

function buildGraph(state) {
  var knownNodes = state.known_nodes || [];
  var positions = state.node_positions || {};
  var meta = state.node_meta || {};
  var cx = App.width / 2, cy = App.height / 2;

  // Merge all node IDs
  var allIds = {};
  var selfId = null;
  knownNodes.forEach(function(n) { allIds[n] = true; });
  Object.keys(positions).forEach(function(n) { allIds[n] = true; });

  // Add public channel 0 (always present when connected)
  if (state.connected) {
    allIds['meshtastic:channel:0'] = true;
  }

  var backends = state.backends || [];
  if (backends.length > 0 && backends[0].self_node_id) selfId = backends[0].self_node_id;

  // Check if node set actually changed — if not, just update data in place
  var currentIds = Object.keys(allIds).sort().join(',');
  if (currentIds === _prevNodeIds && App.nodes.length > 0) {
    // Just update metadata on existing nodes (no position reset)
    App.nodes.forEach(function(n) {
      if (n.isSelf) return;
      var m = meta[n.id] || {};
      var pos = positions[n.id] || {};
      n.hops = (typeof m.hops === 'number') ? m.hops : n.hops;
      n.rssi = m.rssi || pos.rssi || n.rssi;
      n.snr = m.snr || pos.snr || n.snr;
      n.lat = pos.lat || n.lat;
      n.lon = pos.lon || n.lon;
      n.lastHeard = pos.last_update || n.lastHeard;
    });
    return;
  }
  _prevNodeIds = currentIds;

  // Build node list — preserve existing positions if node existed before
  var oldNodeMap = {};
  (App.nodes || []).forEach(function(n) { oldNodeMap[n.id] = n; });

  var nodes = [];
  var nodeMap = {};

  // MY NODE at center (fixed position)
  var selfNode = oldNodeMap['__self__'] || { id: '__self__', x: cx, y: cy };
  selfNode.fx = cx; selfNode.fy = cy; selfNode.isSelf = true;
  selfNode.label = 'MY NODE'; selfNode.hops = 0; selfNode.isChannel = false;
  nodes.push(selfNode);
  nodeMap['__self__'] = selfNode;

  Object.keys(allIds).forEach(function(nid) {
    if (selfId && nid === selfId) return;
    var m = meta[nid] || {};
    var pos = positions[nid] || {};
    var hops = (typeof m.hops === 'number') ? m.hops : null;
    var shortId = nid.length > 8 ? nid.slice(-4) : nid;
    var isChannel = nid.indexOf('channel:') !== -1;
    if (isChannel) shortId = 'CH ' + (nid.split(':').pop() || '0');

    // Reuse existing position if available
    var existing = oldNodeMap[nid];
    var node;
    if (existing) {
      node = existing;
      node.hops = hops; node.isChannel = isChannel; node.label = shortId;
      node.rssi = m.rssi || pos.rssi || null;
      node.snr = m.snr || pos.snr || null;
      node.lat = pos.lat || null; node.lon = pos.lon || null;
      node.lastHeard = pos.last_update || null;
    } else {
      // New node — place on hop ring with organic jitter
      var angle = hashStr(nid) % 360 * Math.PI / 180;
      var ringR = hops !== null ? (80 + hops * 100) : 200;
      ringR = Math.min(ringR, Math.min(cx, cy) - 40);
      // Add chaos: ±30% radius jitter + ±15° angle jitter
      var jitterR = ringR * (0.7 + (hashStr(nid + 'r') % 60) / 100);
      var jitterA = angle + ((hashStr(nid + 'a') % 30) - 15) * Math.PI / 180;
      node = {
        id: nid, label: shortId, hops: hops, isChannel: isChannel,
        x: cx + Math.cos(jitterA) * jitterR,
        y: cy + Math.sin(jitterA) * jitterR,
        rssi: m.rssi || pos.rssi || null, snr: m.snr || pos.snr || null,
        lat: pos.lat || null, lon: pos.lon || null,
        lastHeard: pos.last_update || null,
      };
    }
    nodes.push(node);
    nodeMap[nid] = node;
  });

  // Links
  var links = [];
  nodes.forEach(function(n) {
    if (n.isSelf) return;
    links.push({ source: nodeMap['__self__'], target: n });
  });

  App.nodes = nodes;
  App.links = links;

  // Rebuild simulation (only when nodes actually changed)
  if (App.simulation) App.simulation.stop();
  App.simulation = d3.forceSimulation(nodes)
    .force('charge', d3.forceManyBody().strength(-80))
    .force('center', d3.forceCenter(cx, cy))
    .force('link', d3.forceLink(links).distance(function(l) {
      var h = l.target.hops;
      return h !== null ? 80 + h * 80 : 180;
    }).strength(0.3))
    .force('collision', d3.forceCollide(30))
    .alphaDecay(0.05)
    .velocityDecay(0.4)
    .on('tick', function() {});
}

function hashStr(s) { var h=0; for(var i=0;i<s.length;i++) h=((h<<5)-h+s.charCodeAt(i))|0; return Math.abs(h); }

// ─── Canvas Rendering ──────────────────────────────────────────────────────

function renderLoop() {
  App.breathPhase += 0.02;
  // Mouse magnetism — attract nearby nodes toward cursor
  if (_mouseX > 0 && _mouseY > 0) {
    App.nodes.forEach(function(n) {
      if (n.isSelf) return;
      var dx = _mouseX - n.x, dy = _mouseY - n.y;
      var dist = Math.sqrt(dx*dx + dy*dy);
      if (dist < 120 && dist > 5) {
        var pull = Math.max(0, (120 - dist) / 120) * 0.6;
        n.x += dx * pull * 0.03;
        n.y += dy * pull * 0.03;
      }
    });
  }
  renderCanvas();
  App.animFrame = requestAnimationFrame(renderLoop);
}

function getColor(varName) {
  return getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
}

function renderCanvas() {
  var ctx = App.ctx, w = App.width, h = App.height;
  if (!ctx || !w) return;

  var bg = getColor('--lo-bg');
  var ink = getColor('--lo-ink');
  var dim = getColor('--lo-dim');
  var faint = getColor('--lo-faint');
  var accent = getColor('--lo-accent');
  var accent2 = getColor('--lo-accent-2');
  var divider = getColor('--lo-divider-strong');

  ctx.clearRect(0, 0, w, h);

  var cx = w/2, cy = h/2;
  var isTraffic = App.view === 'traffic';

  // In TRAFFIC mode, build a set of node IDs with recent messages
  var activeNodes = {};
  if (isTraffic && App.state && App.state.messages) {
    var now = Date.now() / 1000;
    App.state.messages.forEach(function(m) {
      if (now - m.ts < 300) activeNodes[m.node] = true; // last 5 min
    });
  }

  // Draw hop rings
  ctx.strokeStyle = divider;
  ctx.lineWidth = 0.5;
  ctx.setLineDash([4, 6]);
  for (var ring = 1; ring <= 4; ring++) {
    var r = 80 + ring * 80;
    if (r > Math.min(cx, cy) - 20) break;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
  }
  ctx.setLineDash([]);

  // Draw links with hop labels
  App.links.forEach(function(link) {
    var s = link.source, t = link.target;
    var hops = t.hops;
    ctx.beginPath();
    ctx.moveTo(s.x, s.y); ctx.lineTo(t.x, t.y);
    var linkActive = isTraffic && (activeNodes[t.id] || false);
    ctx.strokeStyle = linkActive ? accent : accent2;
    ctx.lineWidth = linkActive ? 2.5 : (hops === 0 ? 2 : (hops !== null && hops <= 2 ? 1 : 0.5));
    ctx.globalAlpha = isTraffic ? (linkActive ? 0.8 : 0.08) : 0.3;
    if (hops !== null && hops > 2) { ctx.setLineDash([3, 5]); }
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;

    // Hop count label on the link midpoint
    if (hops !== null) {
      var mx = (s.x + t.x) / 2, my = (s.y + t.y) / 2;
      ctx.font = '500 8px "IBM Plex Mono"';
      ctx.textAlign = 'center';
      ctx.fillStyle = faint;
      ctx.fillText(hops === 0 ? 'DIRECT' : hops + 'H', mx, my - 3);
    }
  });

  // Draw nodes
  App.nodes.forEach(function(node, i) {
    var nodeActive = !isTraffic || node.isSelf || activeNodes[node.id];
    if (isTraffic && !nodeActive) ctx.globalAlpha = 0.15;
    var breath = Math.sin(App.breathPhase + i * 0.7) * 0.3 + 1;
    var r = node.isSelf ? 10 : (node.isChannel ? 8 : 5);
    var baseR = r * breath;

    if (node.isChannel) {
      // Hexagon
      ctx.beginPath();
      for (var a = 0; a < 6; a++) {
        var angle = Math.PI / 3 * a - Math.PI / 6;
        var px = node.x + baseR * Math.cos(angle);
        var py = node.y + baseR * Math.sin(angle);
        if (a === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      }
      ctx.closePath();
      ctx.fillStyle = accent2; ctx.fill();
      ctx.strokeStyle = accent2; ctx.lineWidth = 1; ctx.stroke();
    } else if (node.isSelf) {
      // MY NODE — orange with sonar ring
      var sonarR = 10 + (App.breathPhase * 8 % 30);
      var sonarAlpha = Math.max(0, 1 - sonarR / 40);
      ctx.beginPath(); ctx.arc(node.x, node.y, sonarR, 0, Math.PI * 2);
      ctx.strokeStyle = accent; ctx.lineWidth = 1; ctx.globalAlpha = sonarAlpha;
      ctx.stroke(); ctx.globalAlpha = 1;
      ctx.beginPath(); ctx.arc(node.x, node.y, baseR, 0, Math.PI * 2);
      ctx.fillStyle = accent; ctx.fill();
    } else {
      // Regular peer
      // Signal halo
      if (node.hops !== null) {
        ctx.beginPath(); ctx.arc(node.x, node.y, baseR + 6, 0, Math.PI * 2);
        ctx.strokeStyle = accent2; ctx.lineWidth = 0.5; ctx.globalAlpha = 0.15;
        ctx.stroke(); ctx.globalAlpha = 1;
      }
      ctx.beginPath(); ctx.arc(node.x, node.y, baseR, 0, Math.PI * 2);
      ctx.fillStyle = accent2; ctx.fill();
    }

    // Label
    ctx.font = '500 9px "IBM Plex Mono"';
    ctx.textAlign = 'center';
    ctx.fillStyle = node.isSelf ? accent : dim;
    ctx.fillText(node.label, node.x, node.y + baseR + 12);

    // Hop label
    if (!node.isSelf && node.hops !== null) {
      ctx.font = '400 8px "IBM Plex Mono"';
      ctx.fillStyle = faint;
      ctx.fillText(node.hops === 0 ? 'direct' : node.hops + 'h', node.x, node.y + baseR + 21);
    }

    // Selected highlight
    if (App.selectedNode && App.selectedNode === node.id) {
      ctx.beginPath(); ctx.arc(node.x, node.y, baseR + 10, 0, Math.PI * 2);
      ctx.strokeStyle = accent; ctx.lineWidth = 1.5; ctx.setLineDash([3, 3]);
      ctx.stroke(); ctx.setLineDash([]);
    }
    ctx.globalAlpha = 1; // reset after each node
  });
}

// ─── Canvas Interaction + Floating Windows ────────────────────────────────

var _mouseX = 0, _mouseY = 0;

function onCanvasClick(e) {
  var rect = App.canvas.getBoundingClientRect();
  var mx = e.clientX - rect.left, my = e.clientY - rect.top;
  var closest = null, closestDist = Infinity;
  App.nodes.forEach(function(n) {
    var dx = n.x - mx, dy = n.y - my;
    var dist = Math.sqrt(dx*dx + dy*dy);
    if (dist < 40 && dist < closestDist) { closest = n; closestDist = dist; }
  });
  if (closest && !closest.isSelf) {
    openFloatWindow(closest);
  } else if (closest && closest.isSelf) {
    showToast('This is your radio node');
  }
}

// Track mouse for magnetism
App.canvas.addEventListener('mousemove', function(e) {
  var rect = App.canvas.getBoundingClientRect();
  _mouseX = e.clientX - rect.left;
  _mouseY = e.clientY - rect.top;
});

// ─── Floating Node Windows ────────────────────────────────────────────────

var _openWindows = {};

async function openFloatWindow(node) {
  if (_openWindows[node.id]) return; // already open
  var win = document.createElement('div');
  win.className = 'lo-float-win';
  win.dataset.nodeId = node.id;
  var wx = Math.min(Math.max(node.x + 20, 10), App.width - 340);
  var wy = Math.max(Math.min(node.y - 80, App.height - 420), 10);
  win.style.left = wx + 'px'; win.style.top = wy + 'px';
  var label = node.label || node.id.slice(-6);
  var eid = node.id.replace(/[^a-zA-Z0-9]/g, '_');
  win.innerHTML =
    '<div class="lo-fw-header" onmousedown="startDragWin(event,this.parentElement)">' +
      '<span class="lo-fw-name">' + escapeHtml(label) + '</span>' +
      '<button class="lo-fw-close" onclick="closeFloatWin(\'' + escapeHtml(node.id) + '\')">\u00d7</button>' +
    '</div>' +
    '<div class="lo-fw-meta" id="fw-m-' + eid + '">Loading...</div>' +
    '<div class="lo-fw-actions" id="fw-a-' + eid + '"></div>' +
    '<div class="lo-fw-messages" id="fw-g-' + eid + '"><div class="lo-fw-empty">LOADING...</div></div>' +
    '<div class="lo-fw-composer">' +
      '<span class="lo-prompt">\u003e</span>' +
      '<input type="text" placeholder="message ' + escapeHtml(label) + '..." onkeydown="if(event.key===\'Enter\'){event.preventDefault();floatSend(\'' + escapeHtml(node.id) + '\',this)}">' +
      '<button class="lo-send" onclick="floatSend(\'' + escapeHtml(node.id) + '\',this.previousElementSibling)">SEND</button>' +
    '</div>';
  document.getElementById('float-windows').appendChild(win);
  _openWindows[node.id] = win;
  await loadFloatData(node.id);
}

async function loadFloatData(nodeId) {
  var eid = nodeId.replace(/[^a-zA-Z0-9]/g, '_');
  var metaEl = document.getElementById('fw-m-' + eid);
  var actEl = document.getElementById('fw-a-' + eid);
  var msgsEl = document.getElementById('fw-g-' + eid);
  if (!metaEl) return;
  var canvasNode = App.nodes.find(function(n) { return n.id === nodeId; });
  var d = await callApi('GET', '/api/threads/' + encodeURIComponent(nodeId));
  var contact = (d && d.contact) || {};
  var msgs = (d && d.messages) || [];
  var proto = (contact.protocol === 'mc' || contact.protocol === 'meshcore') ? 'MC' : 'MT';
  var lines = ['<div>ID: ' + escapeHtml(nodeId) + '</div>', '<div>PROTOCOL: ' + proto + '</div>'];
  var hops = contact.last_hops;
  if (hops !== null && hops !== undefined) lines.push('<div>HOPS: ' + (hops === 0 ? 'DIRECT' : hops) + '</div>');
  if (contact.last_rssi) lines.push('<div>RSSI: ' + contact.last_rssi + ' dBm</div>');
  if (contact.last_snr) lines.push('<div>SNR: ' + contact.last_snr + ' dB</div>');
  if (contact.last_heard) lines.push('<div>HEARD: ' + relativeTime(contact.last_heard) + '</div>');
  if (canvasNode && canvasNode.lat) lines.push('<div>GPS: ' + canvasNode.lat.toFixed(4) + ', ' + canvasNode.lon.toFixed(4) + '</div>');
  metaEl.innerHTML = lines.join('');
  var aiVal = contact.ai_enabled;
  var aiLabel = aiVal === 1 ? 'AI: ON' : aiVal === 0 ? 'AI: OFF' : 'AI: AUTO';
  var aiClass = aiVal === 1 ? ' on' : '';
  actEl.innerHTML = '<button class="' + aiClass + '" onclick="floatToggleAi(\'' + escapeHtml(nodeId) + '\')">' + aiLabel + '</button>';
  if (msgs.length === 0) { msgsEl.innerHTML = '<div class="lo-fw-empty">NO MESSAGES YET</div>'; }
  else {
    msgsEl.innerHTML = msgs.slice(-15).map(function(m) {
      var ac = m.direction === 'in' ? 'in' : (m.author === 'ai' ? 'ai' : 'out');
      var arrow = m.direction === 'in' ? '\u2190' : '\u2192';
      return '<div class="lo-fw-msg"><span class="lo-fw-msg-time">' + formatTime(m.timestamp) +
        '</span><span class="lo-fw-msg-arrow ' + ac + '">' + arrow +
        '</span><span class="lo-fw-msg-body">' + escapeHtml(m.text) + '</span></div>';
    }).join('');
    msgsEl.scrollTop = msgsEl.scrollHeight;
  }
  try { callApi('POST', '/api/threads/' + encodeURIComponent(nodeId) + '/open'); } catch(e) {}
}

function closeFloatWin(nodeId) {
  var win = _openWindows[nodeId];
  if (win && win.parentNode) win.parentNode.removeChild(win);
  delete _openWindows[nodeId];
}

async function floatSend(nodeId, inputEl) {
  var text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = '';
  var isChannel = nodeId.indexOf('channel:') !== -1;
  if (isChannel) {
    var ch = parseInt(nodeId.split(':').pop()) || 0;
    await callApi('POST', '/api/send-mesh', {text: text, node_id: '', channel: ch});
    showToast('Broadcast on CH ' + ch);
  } else {
    await callApi('POST', '/api/send-mesh', {text: text, node_id: nodeId, channel: 0});
    showToast('Sent to ' + nodeId.slice(-6));
  }
  loadFloatData(nodeId);
}

async function floatToggleAi(nodeId) {
  await callApi('POST', '/api/threads/' + encodeURIComponent(nodeId) + '/ai-toggle');
  loadFloatData(nodeId);
}

var _dragWin = null, _dragOff = {x:0, y:0};
function startDragWin(e, win) {
  _dragWin = win; _dragOff.x = e.clientX - win.offsetLeft; _dragOff.y = e.clientY - win.offsetTop; e.preventDefault();
}
document.addEventListener('mousemove', function(e) {
  if (!_dragWin) return;
  _dragWin.style.left = (e.clientX - _dragOff.x) + 'px';
  _dragWin.style.top = (e.clientY - _dragOff.y) + 'px';
});
document.addEventListener('mouseup', function() { _dragWin = null; });


// ─── Ribbon ────────────────────────────────────────────────────────────────

function updateRibbon(state) {
  var msgs = state.messages || [];
  var recent = msgs.slice(-30);
  App.ribbonData = recent.map(function(m) { return m.dir === 'in' ? 1 : -1; });
  var rc = document.getElementById('ribbon-canvas');
  var rctx = rc.getContext('2d');
  rc.width = rc.parentElement.clientWidth - 200;
  rc.height = 20;
  rctx.clearRect(0, 0, rc.width, rc.height);
  var accent2 = getColor('--lo-accent-2');
  var accent = getColor('--lo-accent');
  var barW = Math.max(2, rc.width / 60);
  App.ribbonData.forEach(function(v, i) {
    rctx.fillStyle = v > 0 ? accent2 : accent;
    var h = Math.abs(v) * 8;
    var x = i * barW;
    rctx.fillRect(x, 10 - (v > 0 ? h : 0), barW - 1, h);
  });
  document.getElementById('ribbon-stats').textContent = state.message_count + ' msgs \u00b7 ' + (state.node_count || 0) + ' nodes';
}

// ─── Poll Loop ─────────────────────────────────────────────────────────────

async function poll() {
  try {
    var r = await fetch('/api/state');
    var d = await r.json();
    App.state = d;

    // Connection
    var dot = document.getElementById('hdr-dot');
    var label = document.getElementById('hdr-conn-label');
    var connected = false;
    try { connected = d.connected; } catch(e) {}
    dot.className = connected ? 'lo-dot on' : 'lo-dot';
    label.textContent = connected ? 'CONNECTED' : 'DISCONNECTED';
    checkConnectionForModal(connected);

    // HUD
    document.getElementById('hud-nodes').textContent = d.node_count || 0;
    document.getElementById('hud-msgs').textContent = d.message_count || 0;
    document.getElementById('hud-model').textContent = d.model || '--';
    document.getElementById('hud-uptime').textContent = formatUptime(d.uptime || 0);

    // Update CONFIG connection status if visible
    if (App.view === 'config') {
      var cfgDot = document.getElementById('cfg-conn-dot');
      var cfgSt = document.getElementById('cfg-conn-status');
      var cfgDet = document.getElementById('cfg-conn-detail');
      var cfgDisc = document.getElementById('cfg-disconn-btn');
      if (cfgDot) { cfgDot.className = connected ? 'lo-dot on' : 'lo-dot'; }
      if (cfgSt) { cfgSt.textContent = connected ? 'Connected' : 'Disconnected'; }
      if (cfgDet) { cfgDet.textContent = connected ? ((d.connection_type || '').toUpperCase() + (d.connection_address ? ' \u2014 ' + d.connection_address : '')) : ''; }
      if (cfgDisc) { cfgDisc.style.display = connected ? '' : 'none'; }
      var cfgUp = document.getElementById('cfg-uptime');
      if (cfgUp) cfgUp.textContent = formatUptime(d.uptime || 0);
    }

    // Rebuild graph
    if (App.view !== 'config') {
      buildGraph(d);
      updateRibbon(d);
    }

    // Refresh open panel
    if (App.selectedNode) {
      // Soft refresh every 5 polls (~10s)
      if (!App._panelRefreshCount) App._panelRefreshCount = 0;
      App._panelRefreshCount++;
      if (App._panelRefreshCount % 5 === 0) openNodePanel(App.selectedNode);
    }
  } catch(e) {}
}

// ─── Connect Modal ─────────────────────────────────────────────────────────

var _connectModalDismissed = false, _disconnectedSince = 0, _wasConnected = false;
function showConnectModal() { if (!_connectModalDismissed) document.getElementById('connect-modal').classList.add('open'); }
function dismissConnectModal() { _connectModalDismissed = true; document.getElementById('connect-modal').classList.remove('open'); }
function hideConnectModal() { document.getElementById('connect-modal').classList.remove('open'); }

function connectModalTypeChanged() {
  var sel = document.getElementById('connect-type');
  document.getElementById('connect-address-row').style.display = sel.value === 'ble' ? 'none' : '';
  document.getElementById('connect-scan-row').style.display = sel.value === 'ble' ? '' : 'none';
  if (sel.value === 'serial') document.getElementById('connect-address').placeholder = 'auto-detect (or /dev/...)';
  else if (sel.value === 'tcp') document.getElementById('connect-address').placeholder = '192.168.1.1:4403';
}

async function connectModalScan() {
  var btn = document.getElementById('connect-scan-btn'), statusEl = document.getElementById('connect-scan-status'), listEl = document.getElementById('connect-scan-list');
  btn.disabled = true; btn.textContent = 'SCANNING...'; statusEl.textContent = 'Scanning for Bluetooth devices (~10s)...'; listEl.innerHTML = '';
  try {
    // BLE scan takes 10-20s — use AbortController with long timeout
    var controller = new AbortController();
    var timeoutId = setTimeout(function() { controller.abort(); }, 45000);
    var resp = await fetch('/api/ble/scan?timeout=10', { signal: controller.signal });
    clearTimeout(timeoutId);
    var d = await resp.json();
    if (!d || !d.devices) { statusEl.textContent = 'Scan returned no data. Is Bluetooth enabled?'; return; }
    var devices = d.devices.filter(function(dev) { return !dev.error; });
    if (devices.length === 0) { statusEl.textContent = 'No devices found.'; return; }
    statusEl.textContent = devices.length + ' found. Tap to connect:';
    listEl.innerHTML = devices.map(function(dev) {
      var bars = (dev.rssi > -60) ? '\u2588\u2588\u2588' : (dev.rssi > -80) ? '\u2588\u2588\u2591' : '\u2588\u2591\u2591';
      return '<div class="lo-scan-device" onclick="connectModalPickDevice(this,\'' + escapeHtml(dev.address) + '\',\'' + escapeHtml(dev.name || '') + '\')">' +
        '<span style="flex:1">' + escapeHtml(dev.name || 'Unknown') + '</span>' +
        '<span style="color:var(--lo-faint);font-size:10px">' + escapeHtml(dev.address) + '</span>' +
        '<span style="color:var(--lo-accent-2);font-size:10px">' + bars + '</span></div>';
    }).join('');
  } catch(e) { statusEl.textContent = 'Error: ' + e; }
  finally { btn.disabled = false; btn.textContent = 'SCAN FOR DEVICES'; }
}

function connectModalPickDevice(el, address, name) {
  document.querySelectorAll('.lo-scan-device').forEach(function(d) { d.classList.remove('selected'); });
  el.classList.add('selected');
  document.getElementById('connect-modal-status').textContent = 'Connecting to ' + (name || address) + '... (BLE takes 10-20s)';
  // Fire and forget — don't use callApi (it shows network error toast on timeout)
  fetch('/api/connection/switch', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({type: 'ble', address: address})
  }).catch(function() {}); // ignore — poll loop will detect connection
}

async function connectFromModal() {
  var type = document.getElementById('connect-type').value;
  var addr = document.getElementById('connect-address').value.trim();
  var payload = {type: type};
  if (type === 'tcp' && addr) {
    if (addr.indexOf(':') !== -1) { var p = addr.split(':'); payload.host = p[0]; payload.port = parseInt(p[1]); }
    else payload.host = addr;
  } else { payload.address = addr || null; }
  document.getElementById('connect-modal-status').textContent = 'Connecting... (modal will close when connected)';
  // Fire and forget — poll loop handles the rest
  fetch('/api/connection/switch', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  }).catch(function() {});
}

function checkConnectionForModal(connected) {
  if (connected) { _wasConnected = true; _disconnectedSince = 0; hideConnectModal(); return; }
  if (!_wasConnected && !_connectModalDismissed) { showConnectModal(); return; }
  if (_wasConnected && !_connectModalDismissed) {
    if (!_disconnectedSince) _disconnectedSince = Date.now();
    if (Date.now() - _disconnectedSince > 10000) { _connectModalDismissed = false; showConnectModal(); }
  }
}

// ─── Help + Theme ──────────────────────────────────────────────────────────

document.getElementById('help-toggle').addEventListener('click', function() { document.getElementById('help-popover').classList.toggle('open'); });
document.addEventListener('click', function(e) { if (!e.target.closest('#help-popover') && !e.target.closest('#help-toggle')) document.getElementById('help-popover').classList.remove('open'); });
document.getElementById('theme-toggle').addEventListener('click', function() { var c = document.documentElement.getAttribute('data-theme') || 'light'; setTheme(c === 'light' ? 'dark' : 'light'); });

// ─── Onboarding ────────────────────────────────────────────────────────────

var _obStep = 0, _obTotal = 5;
function showOnboarding() { _obStep = 0; renderOb(); document.getElementById('onboarding').classList.add('open'); }
function obNext() { if (_obStep < _obTotal - 1) { _obStep++; renderOb(); } else obSkip(); }
function obPrev() { if (_obStep > 0) { _obStep--; renderOb(); } }
function obSkip() { document.getElementById('onboarding').classList.remove('open'); localStorage.setItem('loracle-onboarded', 'true'); }
function renderOb() {
  document.querySelectorAll('.lo-ob-step').forEach(function(s) { s.classList.remove('active'); });
  var steps = document.querySelectorAll('.lo-ob-step');
  if (steps[_obStep]) steps[_obStep].classList.add('active');
  var prog = '';
  for (var i = 0; i < _obTotal; i++) {
    if (i < _obStep) prog += '<span class="lo-ob-dot done">\u25CF</span>';
    else if (i === _obStep) prog += '<span class="lo-ob-dot active">\u25B8</span>';
    else prog += '<span class="lo-ob-dot">\u25CB</span>';
  }
  document.getElementById('ob-progress').innerHTML = prog;
  document.getElementById('ob-prev').style.visibility = _obStep > 0 ? '' : 'hidden';
  document.getElementById('ob-next').textContent = _obStep < _obTotal - 1 ? 'NEXT' : 'DONE';
}
document.addEventListener('keydown', function(e) { if (!document.getElementById('onboarding').classList.contains('open')) return; if (e.key==='ArrowRight') obNext(); if (e.key==='ArrowLeft') obPrev(); if (e.key==='Escape') obSkip(); });
if (localStorage.getItem('loracle-onboarded') !== 'true') setTimeout(showOnboarding, 500);

// ─── Config Data Loading (reuse existing endpoints) ────────────────────────

async function loadConfigData() {
  // Models
  await cfgRefreshModels();
  // System prompt
  var pd = await callApi('GET', '/api/system-prompt');
  if (pd) { document.getElementById('cfg-prompt').value = pd.prompt; document.getElementById('cfg-prompt-count').textContent = pd.prompt.length + ' chars'; }
  document.getElementById('cfg-prompt').addEventListener('input', function() { document.getElementById('cfg-prompt-count').textContent = this.value.length + ' chars'; });
  // Config values
  var cd = await callApi('GET', '/api/config');
  if (cd) { document.getElementById('cfg-max-len').value = cd.max_response_length; document.getElementById('cfg-max-len-val').textContent = cd.max_response_length; document.getElementById('cfg-compression').checked = cd.compression_enabled; }
  // RAG
  cfgLoadRagDocs(); cfgLoadDbStats(); cfgLoadRouting(); cfgLoadPacks();
}

async function cfgRefreshModels() {
  var d = await callApi('GET', '/api/models');
  if (!d) return;
  var sel = document.getElementById('cfg-model-sel');
  sel.innerHTML = d.models.map(function(m) { return '<option' + (m === d.current ? ' selected' : '') + '>' + escapeHtml(m) + '</option>'; }).join('');
  document.getElementById('cfg-model-cur').textContent = d.current;
}
async function cfgSwitchModel() { var m = document.getElementById('cfg-model-sel').value; var d = await callApi('POST', '/api/model', {model: m}); if (d && d.ok) { showToast('Model: ' + d.model); cfgRefreshModels(); } }
async function cfgSavePrompt() { var d = await callApi('POST', '/api/system-prompt', {prompt: document.getElementById('cfg-prompt').value}); if (d && d.ok) { showToast('Prompt saved'); _configDirty = false; } }
async function cfgApplySettings() { var d = await callApi('POST', '/api/config', { max_response_length: parseInt(document.getElementById('cfg-max-len').value), compression_enabled: document.getElementById('cfg-compression').checked }); if (d && d.ok) { showToast('Settings applied'); _configDirty = false; } }
async function cfgToggleAiReplies(on) { await callApi('POST', '/api/ai-replies', {enabled: on}); }
async function cfgToggleRag(on) { await callApi('POST', '/api/rag/toggle', {enabled: on}); }
async function cfgIngestUrl() { var url = document.getElementById('cfg-url-input').value.trim(); if (!url) return; var st = document.getElementById('cfg-url-status'); st.textContent = 'Fetching...'; var d = await callApi('POST', '/api/rag/ingest-url', {url: url}); if (d && d.ok) { st.innerHTML = '<span style="color:var(--lo-accent-2)">\u2713 ' + escapeHtml(d.filename) + '</span>'; document.getElementById('cfg-url-input').value = ''; cfgLoadRagDocs(); } else { st.innerHTML = '<span style="color:#c0392b">Error</span>'; } }
async function cfgUploadFile() { var input = document.getElementById('cfg-file-upload'); if (!input.files.length) return; var fd = new FormData(); fd.append('file', input.files[0]); try { var r = await fetch('/api/rag/ingest-file', {method:'POST',body:fd}); var d = await r.json(); if (d.ok) { showToast('Uploaded: ' + d.filename); input.value = ''; cfgLoadRagDocs(); } else showToast(d.error||'Failed','error'); } catch(e) { showToast('Error','error'); } }
async function cfgLoadRagDocs() { var d = await callApi('GET', '/api/rag/stats'); var el = document.getElementById('cfg-rag-docs'); if (!el) return; if (!d || !d.documents || d.documents.length === 0) { el.innerHTML = '<span style="color:var(--lo-faint)">No documents yet.</span>'; return; } document.getElementById('cfg-rag-stats').textContent = (d.stats && d.stats.total_docs || d.documents.length) + ' docs'; el.innerHTML = d.documents.map(function(doc) { return '<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--lo-divider)"><span style="color:var(--lo-dim)">' + escapeHtml(doc.filename||doc.doc_id) + '</span><span style="display:flex;gap:6px;align-items:center"><span style="color:var(--lo-faint);font-size:10px">' + (doc.chunk_count||0) + ' chunks</span><button class="btn btn-sm" style="color:#c0392b;border-color:#c0392b" onclick="cfgDeleteDoc(\'' + escapeHtml(doc.doc_id) + '\')">x</button></span></div>'; }).join(''); }
async function cfgDeleteDoc(id) { if (!confirm('Delete this document?')) return; var d = await callApi('POST', '/api/rag/delete', {doc_id: id}); if (d && d.ok) { showToast('Deleted'); cfgLoadRagDocs(); } }
async function cfgConnect() { var type = document.getElementById('cfg-conn-type').value; var addr = document.getElementById('cfg-conn-addr').value.trim(); var payload = {type: type}; if (type === 'tcp' && addr) { if (addr.indexOf(':') !== -1) { var p = addr.split(':'); payload.host = p[0]; payload.port = parseInt(p[1]); } else payload.host = addr; } else payload.address = addr || null; fetch('/api/connection/switch', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).catch(function(){}); showToast('Connecting...'); }
async function cfgDisconnect() { await callApi('POST', '/api/connection/disconnect'); }
async function cfgLoadDbStats() { var d = await callApi('GET', '/api/db/stats'); if (d) { var kb = Math.round((d.db_size_bytes||0)/1024); document.getElementById('cfg-db-stats').textContent = d.contacts + ' contacts, ' + d.messages + ' msgs, ' + kb + ' KB'; } }
async function cfgPruneNow() { var d = await callApi('POST', '/api/db/prune'); if (d && d.ok) showToast('Pruned ' + d.pruned + ' messages'); cfgLoadDbStats(); }
async function cfgClearAllMessages() { if (!confirm('Delete ALL messages? This cannot be undone.')) return; var d = await callApi('POST', '/api/db/clear-messages'); if (d && d.ok) showToast('Deleted ' + d.deleted + ' messages'); cfgLoadDbStats(); }
async function cfgFactoryReset() { if (!confirm('FACTORY RESET\\n\\nErase all data? CONTEXT FILES/ preserved.\\n\\nRestart required after reset.')) return; if (!confirm('Are you sure?')) return; var d = await callApi('POST', '/api/factory-reset'); if (d && d.ok) showToast('Reset complete. Restart the bridge.'); cfgLoadDbStats(); }
async function cfgLoadRouting() { try { var d = await callApi('GET', '/api/routing/config'); if (!d) return; document.getElementById('cfg-routing-auto').checked = d.auto_enabled !== false; document.getElementById('cfg-routing-tag').checked = d.show_tier_tag !== false; if (d.tiers) { if (d.tiers.tiny) { document.getElementById('cfg-tier-tiny').value = d.tiers.tiny.model; document.getElementById('cfg-tier-tiny-on').checked = d.tiers.tiny.enabled; } if (d.tiers.std) { document.getElementById('cfg-tier-std').value = d.tiers.std.model; document.getElementById('cfg-tier-std-on').checked = d.tiers.std.enabled; } if (d.tiers.big) { document.getElementById('cfg-tier-big').value = d.tiers.big.model; document.getElementById('cfg-tier-big-on').checked = d.tiers.big.enabled; } } } catch(e) {} }
async function cfgSetRouting(key, val) { var p = {}; if (key === 'auto') p.auto_enabled = val; if (key === 'tag') p.show_tier_tag = val; await callApi('POST', '/api/routing/config', p); }
async function cfgSaveTiers() { var t = { tiny: {model: document.getElementById('cfg-tier-tiny').value, enabled: document.getElementById('cfg-tier-tiny-on').checked}, std: {model: document.getElementById('cfg-tier-std').value, enabled: document.getElementById('cfg-tier-std-on').checked}, big: {model: document.getElementById('cfg-tier-big').value, enabled: document.getElementById('cfg-tier-big-on').checked} }; var d = await callApi('POST', '/api/routing/config', {tiers: t}); if (d && d.ok) { showToast('Tiers saved'); _configDirty = false; } }
var _classifierTimer = null;
function cfgTestClassifier(q) { clearTimeout(_classifierTimer); var el = document.getElementById('cfg-test-result'); if (!q.trim()) { el.textContent = ''; return; } _classifierTimer = setTimeout(async function() { var d = await callApi('POST', '/api/routing/classify', {query: q}); if (d) el.textContent = 'Route: [' + d.tier.toUpperCase() + '] \u00b7 model: ' + d.model; }, 200); }
async function cfgLoadPacks() { try { var d = await callApi('GET', '/api/packs'); if (!d || !d.packs) return; var el = document.getElementById('cfg-packs-list'); el.innerHTML = d.packs.map(function(p) { var st = p.installed ? '<span style="color:var(--lo-accent-2)">INSTALLED</span>' : '<span style="color:var(--lo-faint)">NOT INSTALLED</span>'; return '<div style="padding:8px 0;border-bottom:1px solid var(--lo-divider);cursor:pointer" onclick="cfgShowPack(\'' + escapeHtml(p.id) + '\')"><div style="display:flex;justify-content:space-between"><span style="color:var(--lo-ink);font-weight:500">' + escapeHtml(p.name) + '</span><span style="font-size:9px">' + st + ' \u00b7 ~' + p.estimated_size_mb + 'MB</span></div><div style="font-size:10px;color:var(--lo-dim);margin-top:2px">' + escapeHtml(p.description).substring(0,80) + '</div></div>'; }).join(''); } catch(e) {} }
async function cfgShowPack(id) { var d = await callApi('GET', '/api/packs/' + encodeURIComponent(id)); if (!d) return; var el = document.getElementById('cfg-pack-detail'); var c = document.getElementById('cfg-pack-detail-content'); el.style.display = ''; var actions = d.installed ? '<button class="btn btn-sm" onclick="cfgReinstPack(\'' + escapeHtml(id) + '\')">REINGEST</button> <button class="btn btn-sm" style="color:#c0392b;border-color:#c0392b" onclick="cfgUninstPack(\'' + escapeHtml(id) + '\')">UNINSTALL</button>' : '<button class="btn btn-primary btn-sm" onclick="cfgInstPack(\'' + escapeHtml(id) + '\')">INSTALL PACK</button>'; c.innerHTML = '<div style="font-weight:500;color:var(--lo-ink);margin-bottom:4px">' + escapeHtml(d.name) + ' v' + escapeHtml(d.version) + '</div><div style="font-size:10px;color:var(--lo-dim);margin-bottom:8px">' + (d.installed ? 'INSTALLED' : 'NOT INSTALLED') + ' \u00b7 ' + d.documents.length + ' docs</div><div>' + actions + '</div>'; }
async function cfgInstPack(id) { showToast('Installing...'); await callApi('POST', '/api/packs/' + encodeURIComponent(id) + '/install'); setTimeout(function() { cfgLoadPacks(); cfgShowPack(id); }, 5000); }
async function cfgUninstPack(id) { if (!confirm('Uninstall this pack?')) return; await callApi('POST', '/api/packs/' + encodeURIComponent(id) + '/uninstall'); showToast('Uninstalled'); cfgLoadPacks(); document.getElementById('cfg-pack-detail').style.display = 'none'; }
async function cfgReinstPack(id) { var d = await callApi('POST', '/api/packs/' + encodeURIComponent(id) + '/reingest'); if (d && d.ok) showToast('Re-ingested: ' + d.total_chunks + ' chunks'); }

// ─── Node Refresh ──────────────────────────────────────────────────────────

async function refreshNodes() {
  var btn = document.getElementById('hud-refresh-btn');
  btn.disabled = true; btn.textContent = 'SCANNING...';
  var d = await callApi('POST', '/api/nodes/refresh');
  btn.disabled = false; btn.textContent = 'SCAN MESH';
  if (d && d.ok) showToast('Found ' + d.node_count + ' nodes');
}

// ─── Init ──────────────────────────────────────────────────────────────────

initCanvas();
// Always show connect modal on load — poll will auto-hide if connected
showConnectModal();
poll();
setInterval(poll, 2000);
</script>
</body>
</html>"""
