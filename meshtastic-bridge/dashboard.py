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

from radio.backend import FeatureNotSupported

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
    # Device metrics (battery, voltage, etc.)
    if _bridge and hasattr(_bridge, "_device_metrics"):
        state["device_metrics"] = dict(_bridge._device_metrics)
    else:
        state["device_metrics"] = {}
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
    # Total unread + display metadata (custom_name / is_favorite) from DB
    if _bridge and hasattr(_bridge, "_contact_store"):
        try:
            state["total_unread"] = _bridge._contact_store.total_unread()
        except Exception:
            state["total_unread"] = 0
        try:
            state["contact_meta"] = _bridge._contact_store.get_display_meta()
        except Exception:
            state["contact_meta"] = {}
    else:
        state["total_unread"] = 0
        state["contact_meta"] = {}
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


@app.route("/api/serial/scan", methods=["GET"])
def api_serial_scan():
    """List available system COM / serial ports so users don't have to
    type the path by hand. Uses pyserial's cross-platform list_ports
    (meshtastic-python already depends on pyserial so no new deps).
    """
    try:
        from serial.tools import list_ports
    except ImportError:
        return jsonify({"error": "pyserial not installed", "ports": []}), 500
    try:
        ports = []
        for p in list_ports.comports():
            ports.append({
                "device": p.device,
                "description": (p.description or "").strip(),
                "manufacturer": (p.manufacturer or "").strip(),
                "vid": p.vid, "pid": p.pid,
                # Flag the entries that look like a LoRa radio so the UI can
                # highlight them over, say, a built-in Bluetooth serial port.
                "likely_radio": _looks_like_radio_port(p),
            })
        # Radio-shaped ports first, then alphabetical.
        ports.sort(key=lambda x: (not x["likely_radio"], x["device"]))
        return jsonify({"ports": ports})
    except Exception as e:
        return jsonify({"error": str(e), "ports": []}), 500


def _looks_like_radio_port(p) -> bool:
    """Heuristic: does a pyserial ListPortInfo look like a LoRa radio?"""
    blob = " ".join(
        str(x or "") for x in (p.device, p.description, p.manufacturer)
    ).lower()
    # Silicon Labs CP210x, CH340, FT232, and the common USB vendor names for
    # T-Beam / Heltec / RAK / NanoSG1 / MeshCore boards.
    hints = (
        "cp210", "ch340", "ch341", "ft232", "ftdi",
        "silicon labs", "wch", "qinheng",
        "tbeam", "heltec", "rak", "nanog1", "meshcore",
        "/dev/cu.usbserial", "/dev/ttyusb", "/dev/ttyacm", "/dev/cu.slab",
        "/dev/cu.wchusbserial",
    )
    return any(h in blob for h in hints)


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


@app.route("/api/backends/add", methods=["POST"])
def api_backends_add():
    """Attach a secondary MeshCore radio at runtime.

    Request JSON::

        { "transport": "serial" | "tcp" | "ble",
          "serial_port": "/dev/ttyUSB1",        # serial only
          "tcp_host": "192.168.1.50", "tcp_port": 4000,   # tcp only
          "ble_address": "AA:BB:...",           # ble only, optional
          "seed_bridge": true                   # default true — turn on
        }                                         bidirectional ch-0 relay

    Returns the backend-info dict on success (shape matches ``/api/radios``).
    Blocking: the MeshCore connect can take up to ~30s — the request runs
    it inline so the dashboard waits for a definitive success/fail.
    """
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    data = request.get_json(silent=True) or {}
    transport = (data.get("transport") or "").lower().strip()
    if transport not in ("serial", "tcp", "ble"):
        return jsonify({"error": "transport must be serial|tcp|ble"}), 400

    seed_bridge = data.get("seed_bridge", True)
    kwargs = {"transport": transport, "seed_bridge": bool(seed_bridge)}
    if transport == "serial":
        sp = data.get("serial_port") or data.get("address")
        if not sp:
            return jsonify({"error": "serial_port is required"}), 400
        kwargs["serial_port"] = sp
    elif transport == "tcp":
        kwargs["tcp_host"] = data.get("tcp_host") or data.get("host") or "192.168.1.1"
        port = data.get("tcp_port") or data.get("port") or 4000
        try:
            kwargs["tcp_port"] = int(port)
        except (TypeError, ValueError):
            return jsonify({"error": "tcp_port must be an integer"}), 400
    elif transport == "ble":
        kwargs["ble_address"] = data.get("ble_address") or data.get("address")

    try:
        info = _bridge.add_secondary_radio(**kwargs)
    except ImportError as e:
        return jsonify({"error": str(e)}), 501
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("Failed to add secondary radio")
        return jsonify({"error": f"Could not connect: {e}"}), 500
    return jsonify({"ok": True, "backend": info})


@app.route("/api/backends/remove", methods=["POST"])
def api_backends_remove():
    """Disconnect a secondary radio and clear persisted config.

    Accepts optional ``{"backend_id": "..."}``. If omitted, removes the
    first meshcore backend (there's practically ever only one).
    """
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    data = request.get_json(silent=True) or {}
    backend_id = data.get("backend_id")
    try:
        _bridge.remove_secondary_radio(backend_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


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


@app.route("/api/messages/search", methods=["GET"])
def api_messages_search():
    """Search messages across all threads."""
    if _bridge is None or not hasattr(_bridge, "_message_store"):
        return jsonify({"results": []})
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"results": []})
    try:
        rows = _bridge._message_store._db.execute(
            """SELECT m.text, m.timestamp, m.direction, m.contact_id,
                      c.short_name
               FROM messages m
               LEFT JOIN contacts c ON c.id = m.contact_id
               WHERE m.text LIKE ?
               ORDER BY m.timestamp DESC LIMIT 20""",
            (f"%{q}%",),
        ).fetchall()
        return jsonify({"results": [dict(r) for r in rows]})
    except Exception:
        return jsonify({"results": []})


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
    """Send a manual message to a contact.

    Routes through RadioManager so MeshCore (``mc:``) and Meshtastic (``mt:``
    / legacy unprefixed) thread IDs both work. Falls back to the primary
    Meshtastic interface only if no matching backend is available.
    """
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Empty message"}), 400

    # Derive protocol from the unified id prefix so an MC thread auto-creates
    # an mc-protocol contact. The legacy code hardcoded "meshtastic" here,
    # which meant newly-seen MC peers landed with the wrong protocol tag and
    # the later send path picked the wrong backend.
    inferred_protocol = "meshcore" if thread_id.startswith("mc:") else "meshtastic"

    # Auto-create contact if not in DB yet
    contact = _bridge._contact_store.get(thread_id)
    if contact is None:
        try:
            short = thread_id[-6:] if len(thread_id) > 6 else thread_id
            _bridge._contact_store.upsert(
                contact_id=thread_id, protocol=inferred_protocol,
                backend_id=thread_id, short_name=short,
            )
            contact = _bridge._contact_store.get(thread_id)
        except Exception:
            pass
    if contact is None:
        return jsonify({"error": "Contact not found"}), 404

    # Insert with status='sending' up-front so the message shows in history
    # immediately — the UI then transitions it to 'sent' or 'failed'.
    msg_id = _bridge._message_store.insert(
        contact_id=thread_id, direction="out", author="human",
        text=text, protocol=contact["protocol"],
        delivery_status="sending",
    )

    is_channel = "channel:" in thread_id
    # Normalise the outgoing id into a form RadioManager understands.
    # Thread IDs come in several shapes depending on how the contact was
    # created: ``mt:!abc`` / ``!abc`` / ``meshtastic:channel:0`` / ``mc:abcdef``
    # / ``mc:channel:1``. RadioManager's .send() expects ``mt:<native>`` or
    # ``mc:<native>`` (plus the is_dm / channel flags for broadcasts).
    routing_id = thread_id
    channel_num = 0
    if is_channel:
        try:
            channel_num = int(thread_id.split(":")[-1])
        except (ValueError, IndexError):
            channel_num = 0
        if thread_id.startswith("mc:"):
            routing_id = "mc:"
        else:
            routing_id = "mt:"
    elif not thread_id.startswith("mt:") and not thread_id.startswith("mc:"):
        # Legacy unprefixed meshtastic node id (``!abc``) — tag it so
        # RadioManager routes to the meshtastic backend.
        routing_id = "mt:" + thread_id

    try:
        # Prefer RadioManager whenever it has a connected matching backend —
        # that's the only way to reach the MeshCore side, and it keeps the
        # meshtastic side behaving the same as before.
        sent_via_manager = False
        try:
            mgr = getattr(_bridge, "_radio_manager", None)
            if mgr is not None:
                proto_short = "mc" if routing_id.startswith("mc:") else "mt"
                if mgr._find_backend_for_protocol(proto_short) is not None:
                    mgr.send(routing_id, text, channel=channel_num, is_dm=not is_channel)
                    sent_via_manager = True
        except Exception as e:
            logger.debug(f"RadioManager send failed, falling back: {e}")

        if not sent_via_manager:
            # Fallback: legacy Meshtastic-only path. MC threads without a
            # connected MC backend surface a clear error instead of silently
            # sending nowhere.
            if routing_id.startswith("mc:"):
                raise RuntimeError("MeshCore radio not connected — add a secondary radio first")
            if not _bridge.interface or not _bridge._is_interface_alive():
                raise RuntimeError("Radio not connected")
            want_ack = os.environ.get("DEBUG_WANT_ACK") == "1"
            if is_channel:
                from meshtastic import BROADCAST_ADDR
                _bridge.interface.sendText(text, destinationId=BROADCAST_ADDR, channelIndex=channel_num, wantAck=want_ack)
            else:
                native = thread_id[3:] if thread_id.startswith("mt:") else thread_id
                _bridge.interface.sendText(text, destinationId=native, wantAck=want_ack)

        _bridge._message_store.update_status(msg_id, "sent")
        record_message("out", thread_id, text)
        return jsonify({"ok": True, "msg_id": msg_id, "status": "sent"})
    except Exception as e:
        try:
            _bridge._message_store.update_status(msg_id, "failed")
        except Exception:
            pass
        return jsonify({"error": str(e), "msg_id": msg_id, "status": "failed"}), 500


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


@app.route("/api/threads/<path:thread_id>/favorite", methods=["POST"])
def api_thread_favorite(thread_id):
    """Toggle the is_favorite flag on a contact."""
    if _bridge is None or not hasattr(_bridge, "_contact_store"):
        return jsonify({"error": "Not initialized"}), 503
    if _bridge._contact_store.get(thread_id) is None:
        return jsonify({"error": "Contact not found"}), 404
    new_val = _bridge._contact_store.toggle_favorite(thread_id)
    return jsonify({"ok": True, "is_favorite": bool(new_val)})


_DASHBOARD_AI_NODE_ID = "__dashboard_ai__"


@app.route("/api/ai_chat", methods=["POST"])
def api_ai_chat():
    """Local AI chat from the dashboard AI tab. Proxies to Ollama with a
    dedicated conversation history keyed off a reserved node id so it doesn't
    mix with mesh traffic."""
    if _bridge is None or not getattr(_bridge, "ollama", None):
        return jsonify({"error": "Ollama not available"}), 503
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Empty message"}), 400
    if len(message) > 4000:
        return jsonify({"error": "Message too long (max 4000 chars)"}), 400
    try:
        response = _bridge.ollama.chat(
            node_id=_DASHBOARD_AI_NODE_ID,
            message=message,
        )
        return jsonify({"ok": True, "response": response, "model": _bridge.ollama.model})
    except Exception as e:
        logger.exception("AI chat error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/ai_chat/history", methods=["GET"])
def api_ai_chat_history():
    """Return the current dashboard AI conversation so the UI can re-render it on reload."""
    if _bridge is None or not getattr(_bridge, "ollama", None):
        return jsonify({"messages": []})
    try:
        hist = list(_bridge.ollama._history.get(_DASHBOARD_AI_NODE_ID, []))
        return jsonify({"messages": hist})
    except Exception:
        return jsonify({"messages": []})


@app.route("/api/ai_chat/clear", methods=["POST"])
def api_ai_chat_clear():
    """Reset the dashboard AI conversation history."""
    if _bridge is None or not getattr(_bridge, "ollama", None):
        return jsonify({"ok": True})
    try:
        _bridge.ollama.clear_history(_DASHBOARD_AI_NODE_ID)
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/api/threads/<path:thread_id>/rename", methods=["POST"])
def api_thread_rename(thread_id):
    """Set or clear a custom display name for a contact."""
    if _bridge is None or not hasattr(_bridge, "_contact_store"):
        return jsonify({"error": "Not initialized"}), 503
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if name is not None and not isinstance(name, str):
        return jsonify({"error": "name must be a string or null"}), 400
    if isinstance(name, str) and len(name) > 64:
        return jsonify({"error": "Name too long (max 64 chars)"}), 400
    if not _bridge._contact_store.set_custom_name(thread_id, name):
        return jsonify({"error": "Contact not found"}), 404
    contact = _bridge._contact_store.get(thread_id)
    return jsonify({"ok": True, "custom_name": contact.get("custom_name")})


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


# ─── Bridge endpoints (LORACLE v2 Phase 2) ──────────────────────────────────

@app.route("/api/bridge/config", methods=["GET"])
def api_bridge_config():
    """Return the current bridge-relay config (enabled + per-channel rules)."""
    if _bridge is None or not hasattr(_bridge, "_bridge_config"):
        return jsonify({"enabled": False, "rules": []})
    return jsonify(_bridge._bridge_config)


@app.route("/api/bridge/config", methods=["POST"])
def api_bridge_config_set():
    """Update the bridge-relay config. Hot-swaps the live Relay's policy."""
    if _bridge is None or not hasattr(_bridge, "_save_bridge_config"):
        return jsonify({"error": "Not initialized"}), 503
    data = request.get_json(silent=True) or {}
    # Minimal shape validation; deeper validation lives in bridge/config.py
    if not isinstance(data, dict):
        return jsonify({"error": "expected JSON object"}), 400
    cfg = {
        "enabled": bool(data.get("enabled", False)),
        "rules": data.get("rules") or [],
    }
    if not isinstance(cfg["rules"], list):
        return jsonify({"error": "rules must be a list"}), 400
    try:
        _bridge._save_bridge_config(cfg)
        return jsonify({"ok": True, "config": cfg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bridge/public-channel", methods=["POST"])
def api_bridge_public_channel():
    """One-click toggle for the "auto-relay public channel 0 both ways" pattern.

    Request JSON ``{"enabled": bool}``. Delegates to the bridge's seed /
    unseed helpers so custom rules (non-default channel, ai-gated, etc.)
    aren't clobbered. Returns the resulting config.
    """
    if _bridge is None or not hasattr(_bridge, "_seed_default_bridge_rules"):
        return jsonify({"error": "Not initialized"}), 503
    data = request.get_json(silent=True) or {}
    want_on = bool(data.get("enabled", True))
    try:
        if want_on:
            _bridge._seed_default_bridge_rules(force=True)
        else:
            _bridge._unseed_default_bridge_rules()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    cfg = getattr(_bridge, "_bridge_config", {}) or {}
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/bridge/stats", methods=["GET"])
def api_bridge_stats():
    """Return live relay counters (relayed, dropped, dedup cache size)."""
    if _bridge is None or not hasattr(_bridge, "_relay"):
        return jsonify({"relayed": 0, "dropped": 0, "dedup_size": 0})
    try:
        return jsonify(_bridge._relay.stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bridge/history", methods=["GET"])
def api_bridge_history():
    """Return persistent bridge-relay audit log (Phase 5 bridge_events table).

    Query params: ``limit`` (default 100, max 1000), ``since`` (unix ts,
    optional), ``outcome`` (relayed/blocked/rate_limited/etc, optional).
    """
    if _bridge is None or not hasattr(_bridge, "_bridge_event_store"):
        return jsonify({"events": [], "counts": {}})
    limit = min(int(request.args.get("limit", 100)), 1000)
    since_raw = request.args.get("since")
    outcome = request.args.get("outcome") or None
    try:
        since = float(since_raw) if since_raw else None
    except ValueError:
        since = None
    try:
        events = _bridge._bridge_event_store.recent(
            limit=limit, outcome=outcome, since=since
        )
        counts = _bridge._bridge_event_store.count(since=since)
        return jsonify({"events": events, "counts": counts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bridge/events", methods=["GET"])
def api_bridge_events():
    """Return the recent bridge events (ring buffer, up to last 200).

    Optional ``since`` query param (unix timestamp) returns only events
    newer than that — lets the BRIDGE tab poll incrementally without
    re-rendering the whole log.
    """
    if _bridge is None or not hasattr(_bridge, "_bridge_events"):
        return jsonify({"events": []})
    since_raw = request.args.get("since")
    try:
        since = float(since_raw) if since_raw else 0.0
    except ValueError:
        since = 0.0
    events = [e for e in _bridge._bridge_events if e.get("timestamp", 0) > since]
    return jsonify({"events": events})


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


@app.route("/api/traceroute", methods=["POST"])
def api_traceroute():
    """Send a traceroute probe.  Routes via RadioManager so it works for any
    backend that implements ``send_traceroute``.  MeshCore has no traceroute
    today → returns 501 with a clear message instead of silently failing."""
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    mgr = getattr(_bridge, "_radio_manager", None)
    if mgr is None or not mgr.has_connected_backend():
        return jsonify({"error": "No radio connected"}), 503
    data = request.get_json(silent=True) or {}
    dest = (data.get("dest") or "").strip()
    if not dest:
        return jsonify({"error": "Missing dest"}), 400
    try:
        mgr.send_traceroute(dest, hop_limit=7)
        return jsonify({"ok": True, "dest": dest})
    except FeatureNotSupported as e:
        return jsonify({
            "error": str(e),
            "feature_not_supported": True,
        }), 501
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/traceroute/result/<path:dest>", methods=["GET"])
def api_traceroute_result(dest):
    """Poll for traceroute results."""
    if _bridge is None:
        return jsonify({"result": None})
    try:
        result = _bridge._traceroute_results.get(dest)
        return jsonify({"result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/device/reboot", methods=["POST"])
def api_device_reboot():
    """Reboot the connected radio device."""
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    if not _bridge.interface or not _bridge._is_interface_alive():
        return jsonify({"error": "Radio not connected"}), 503
    try:
        _bridge.interface.reboot()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/device/shutdown", methods=["POST"])
def api_device_shutdown():
    """Shutdown the connected radio device."""
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    if not _bridge.interface or not _bridge._is_interface_alive():
        return jsonify({"error": "Radio not connected"}), 503
    try:
        _bridge.interface.shutdown()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/channels", methods=["GET"])
def api_channels():
    """Get all channel configurations from the radio."""
    if _bridge is None or not _bridge.interface:
        return jsonify({"channels": []})
    try:
        channels = []
        ch_list = getattr(_bridge.interface, "channels", None) or []
        for i, ch in enumerate(ch_list):
            settings = ch.settings if hasattr(ch, "settings") else ch
            role_val = ch.role if hasattr(ch, "role") else 0
            role = {0: "disabled", 1: "primary", 2: "secondary"}.get(role_val, "disabled")
            name = getattr(settings, "name", "") or ""
            psk = getattr(settings, "psk", b"")
            channels.append({
                "index": i,
                "name": name,
                "role": role,
                "has_psk": len(psk) > 0 if psk else False,
                "uplink_enabled": getattr(settings, "uplinkEnabled", False),
                "downlink_enabled": getattr(settings, "downlinkEnabled", False),
            })
        return jsonify({"channels": channels})
    except Exception as e:
        return jsonify({"error": str(e), "channels": []}), 500


@app.route("/api/radio/config", methods=["GET"])
def api_radio_config():
    """Get radio config from a specific backend (or primary if unspecified).

    Query param: ``?backend_id=<id>``.  MeshCore returns a read-only view
    (firmware/hw info) with ``read_only: true``; Meshtastic returns writable
    LoRa config.  Backends that expose nothing return 501."""
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    mgr = getattr(_bridge, "_radio_manager", None)
    if mgr is None or not mgr.has_connected_backend():
        return jsonify({"error": "No radio connected"}), 503
    backend_id = (request.args.get("backend_id") or "").strip() or None
    try:
        return jsonify(mgr.get_radio_config(backend_id=backend_id))
    except FeatureNotSupported as e:
        return jsonify({
            "error": str(e),
            "feature_not_supported": True,
        }), 501
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/radio/config", methods=["POST"])
def api_radio_config_set():
    """Update radio config on a specific backend (or primary if unspecified).

    Body may include ``backend_id`` alongside the config fields.  MeshCore is
    read-only → returns 501 instead of silently accepting writes."""
    if _bridge is None:
        return jsonify({"error": "Bridge not initialized"}), 503
    mgr = getattr(_bridge, "_radio_manager", None)
    if mgr is None or not mgr.has_connected_backend():
        return jsonify({"error": "No radio connected"}), 503
    data = request.get_json(silent=True) or {}
    backend_id = (data.pop("backend_id", None) or None)
    try:
        mgr.set_radio_config(data, backend_id=backend_id)
        return jsonify({"ok": True})
    except FeatureNotSupported as e:
        return jsonify({
            "error": str(e),
            "feature_not_supported": True,
        }), 501
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
.lo-bar .lo-conn { display: flex; align-items: center; gap: 14px; font-size: 10px; flex-shrink: 0; }
.lo-bar .lo-conn-row { display: flex; align-items: center; gap: 5px; flex-shrink: 0; }
.lo-bar .lo-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--lo-faint); transition: background 0.3s; display: inline-block; flex-shrink: 0; }
.lo-bar .lo-dot.on { background: var(--lo-accent-2); animation: loPulse 2s ease-in-out infinite; }
.lo-bar .lo-dot.mc { border-radius: 0; transform: rotate(45deg); }
.lo-bar .lo-dot.mc.on { background: #9b59b6; }
.lo-bar .lo-conn-add {
  font-family: inherit; font-size: 9px; letter-spacing: 0.1em; text-transform: uppercase;
  padding: 4px 8px; border: 1px solid var(--lo-divider); background: none;
  color: var(--lo-dim); cursor: pointer; border-radius: 2px;
  transition: color 0.15s, border-color 0.15s, background 0.15s; flex-shrink: 0;
}
.lo-bar .lo-conn-add:hover { color: var(--lo-ink); border-color: var(--lo-ink); }
.lo-bar .lo-conn-add.on { color: #9b59b6; border-color: #9b59b6; }
.lo-bar .lo-scope { display: flex; margin-left: 12px; }
.lo-bar .lo-scope button {
  padding: 4px 10px; font-family: inherit; font-size: 9px; letter-spacing: 0.12em;
  text-transform: uppercase; border: 1px solid var(--lo-divider); border-right: none;
  background: none; color: var(--lo-faint); cursor: pointer; transition: background 0.15s, color 0.15s, border-color 0.15s;
}
.lo-bar .lo-scope button:last-child { border-right: 1px solid var(--lo-divider); }
.lo-bar .lo-scope button:hover { color: var(--lo-ink); border-color: var(--lo-divider-strong); }
.lo-bar .lo-scope button.active { color: var(--lo-ink); border-color: var(--lo-ink); background: rgba(255,255,255,0.04); }
.lo-bar .lo-scope button.active[data-scope="mt"] { color: var(--lo-accent-2); border-color: var(--lo-accent-2); }
.lo-bar .lo-scope button.active[data-scope="mc"] { color: #9b59b6; border-color: #9b59b6; }
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
  width: 420px; max-height: 540px;
  background: var(--lo-bg);
  border: 1px solid var(--lo-divider-strong);
  box-shadow: 0 4px 12px rgba(0,0,0,0.25);
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
  flex: 1; overflow-y: auto; padding: 8px 12px; max-height: 340px; min-height: 80px;
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
.lo-msg-status { display: inline-block; margin-left: 4px; font-size: 9px; letter-spacing: 0; }
.lo-msg-status.sending { color: var(--lo-faint); animation: lo-msg-pulse 1.2s ease-in-out infinite; }
.lo-msg-status.sent { color: var(--lo-dim); }
.lo-msg-status.acked { color: var(--lo-accent-2); }
.lo-msg-status.delivered { color: var(--lo-accent-2); font-weight: 500; }
.lo-msg-status.failed { color: #e74c3c; }
@keyframes lo-msg-pulse { 0%, 100% { opacity: 0.4 } 50% { opacity: 1 } }
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
.btn.active { color: var(--lo-accent); border-color: var(--lo-accent); }

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
/* Dot rendered as a CSS shape (inline-block + vertical-align:middle) instead
   of the old Unicode \25C9 glyph, which drifted low and crowded the first
   letter. Kept inline so text-align:center on success-panel parents still
   works (flex display on the h3 would swallow that). */
.lo-connect-box h3::before {
  content: '';
  display: inline-block;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--lo-accent);
  margin-right: 10px;
  vertical-align: middle;
  position: relative;
  top: -1px;  /* optical centering against uppercase cap-height */
}
/* Success-panel dots: match their heading's protocol color — teal for MT,
   purple (and diamond-shaped) for MC — instead of the default orange prompt. */
#connect-modal-success h3::before { background: var(--lo-accent-2); }
#ar-success h3::before { background: #9b59b6; border-radius: 0; transform: rotate(45deg) translateY(-1px); }
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

/* ── Map View ─────────────────────────────────────────────────────────────── */
/* Flex child of .lo-app, sibling to .lo-canvas-wrap / .lo-ai-view / .lo-bridge-view
   — no hardcoded nav/ribbon offsets; flexbox handles the sizing. margin-top
   gives a small gap under the nav so the tab content doesn't sit flush. */
#map-view {
  position: relative; flex: 1; min-height: 0; margin-top: 12px; z-index: 5;
  background: var(--lo-bg-deep);
}
/* ── AI Chat View ─────────────────────────────────────────────────────── */
.lo-ai-view {
  position: relative; flex: 1; min-height: 0; margin-top: 12px; z-index: 5;
  background: var(--lo-bg); color: var(--lo-ink);
  display: flex; flex-direction: column;
}
.lo-ai-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 20px; border-bottom: 1px solid var(--lo-divider-strong);
  flex-shrink: 0;
}
.lo-ai-title { display: flex; align-items: baseline; gap: 10px; font-size: 12px; letter-spacing: 0.14em; }
.lo-ai-model { font-size: 10px; color: var(--lo-accent); letter-spacing: 0.08em; }
.lo-ai-actions { display: flex; gap: 6px; }
.lo-ai-messages {
  flex: 1; overflow-y: auto; padding: 14px 20px;
  display: flex; flex-direction: column; gap: 10px;
}
.lo-ai-empty { color: var(--lo-dim); font-size: 11px; text-align: center; padding: 40px 20px; }
.lo-ai-msg {
  max-width: 640px; padding: 8px 12px; border: 1px solid var(--lo-divider);
  font-size: 12px; line-height: 1.5; white-space: pre-wrap; word-break: break-word;
}
.lo-ai-msg.user { align-self: flex-end; border-color: var(--lo-divider-strong); background: var(--lo-bg-deep); }
.lo-ai-msg.ai { align-self: flex-start; border-left: 2px solid var(--lo-accent); }
.lo-ai-msg.ai.thinking { color: var(--lo-dim); font-style: italic; }
.lo-ai-msg.ai.error { border-left-color: #e74c3c; color: #e74c3c; }
.lo-ai-composer {
  display: flex; align-items: center; gap: 8px; padding: 10px 20px;
  border-top: 1px solid var(--lo-divider-strong); flex-shrink: 0;
}
.lo-ai-composer .lo-prompt { color: var(--lo-accent); font-size: 14px; font-weight: 500; }
.lo-ai-composer input {
  flex: 1; background: transparent; border: none; color: var(--lo-ink);
  font: inherit; font-size: 13px; outline: none; padding: 4px 0;
  caret-color: var(--lo-accent);
}
.lo-ai-composer input::placeholder { color: var(--lo-faint); }
.lo-ai-composer .lo-send {
  background: var(--lo-ink); color: var(--lo-bg); border: none; padding: 5px 14px;
  font-family: inherit; font-size: 10px; font-weight: 500; letter-spacing: 0.12em;
  text-transform: uppercase; cursor: pointer;
}
.lo-ai-composer .lo-send:disabled { opacity: 0.4; cursor: default; }

/* ── BRIDGE View (LORACLE v2) ─────────────────────────────────────────────── */
.lo-bridge-view { position: relative; flex: 1; min-height: 0; margin-top: 12px; z-index: 5; background: var(--lo-bg); color: var(--lo-ink); display: flex; flex-direction: column; overflow-y: auto; }
.lo-bridge-header { display: flex; align-items: center; justify-content: space-between; padding: 12px 20px; border-bottom: 1px solid var(--lo-divider-strong); flex-shrink: 0; }
.lo-bridge-title { display: flex; align-items: baseline; gap: 12px; font-size: 12px; letter-spacing: 0.14em; }
.lo-bridge-badge { font-size: 9px; letter-spacing: 0.12em; padding: 2px 8px; background: var(--lo-divider); color: var(--lo-ink); border-radius: 2px; }
.lo-bridge-badge.on { background: #27ae60; color: #fff; }
.lo-bridge-stats { display: flex; gap: 16px; font-size: 10px; color: var(--lo-dim); letter-spacing: 0.08em; }
.lo-bridge-stats b { color: var(--lo-ink); font-weight: 600; margin-left: 4px; }
.lo-bridge-panel { padding: 16px 20px; display: flex; flex-direction: column; gap: 18px; max-width: 900px; }
.lo-bridge-section { border: 1px solid var(--lo-divider); padding: 12px 14px; background: var(--lo-bg-deep); }
.lo-bridge-section-title { font-size: 10px; letter-spacing: 0.14em; color: var(--lo-dim); margin-bottom: 10px; }
.lo-bridge-row { display: flex; align-items: center; gap: 8px; font-size: 11px; }
.lo-bridge-hint { font-size: 10px; color: var(--lo-faint); line-height: 1.5; }
.lo-bridge-rule { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px dashed var(--lo-divider); font-size: 11px; }
.lo-bridge-rule:last-child { border-bottom: none; }
.lo-bridge-rule select, .lo-bridge-rule input[type=number] {
  background: var(--lo-bg); color: var(--lo-ink); border: 1px solid var(--lo-divider);
  font-family: var(--font-mono); font-size: 11px; padding: 3px 6px;
}
.lo-bridge-rule .lo-bridge-rule-del {
  background: none; border: 1px solid var(--lo-divider); color: var(--lo-faint);
  font-family: inherit; font-size: 10px; padding: 2px 7px; cursor: pointer; letter-spacing: 0.05em;
}
.lo-bridge-rule .lo-bridge-rule-del:hover { color: #e74c3c; border-color: #e74c3c; }
.lo-bridge-flow {
  max-height: 360px; overflow-y: auto; border: 1px solid var(--lo-divider);
  background: var(--lo-bg); padding: 6px 10px; font-family: var(--font-mono); font-size: 10px;
}
.lo-bridge-flow-row { padding: 4px 0; border-bottom: 1px dashed var(--lo-divider); color: var(--lo-dim); display: grid; grid-template-columns: 60px 70px auto 1fr; gap: 8px; }
.lo-bridge-flow-row:last-child { border-bottom: none; }
.lo-bridge-flow-row .time { color: var(--lo-faint); }
.lo-bridge-flow-row .dir { color: var(--lo-accent); font-weight: 600; }
.lo-bridge-flow-row .sender { color: var(--lo-ink); }
.lo-bridge-flow-row .text { color: var(--lo-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lo-bridge-flow-empty { color: var(--lo-faint); text-align: center; padding: 28px 10px; font-size: 10px; }

.lo-map-marker { width: 10px; height: 10px; border-radius: 50%; border: 2px solid var(--lo-accent-2); background: var(--lo-bg); cursor: pointer; }
.lo-map-marker.self { border-color: var(--lo-accent); background: var(--lo-accent); cursor: default; }
.lo-map-marker.fav { border-color: #f1c40f; box-shadow: 0 0 0 2px rgba(241,196,15,0.35); }
.lo-map-label { font-family: var(--font-mono); font-size: 9px; color: var(--lo-ink); background: var(--lo-bg); padding: 1px 4px; border: 1px solid var(--lo-divider); white-space: nowrap; margin-top: 2px; }

/* ── Node List Sidebar ────────────────────────────────────────────────────── */
.lo-node-sidebar { display: none; position: absolute; top: 0; right: 0; width: 260px; height: 100%; background: var(--lo-bg); border-left: 1px solid var(--lo-divider-strong); z-index: 90; flex-direction: column; overflow: hidden; }
.lo-node-sidebar.open { display: flex; }
.lo-ns-header { display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; border-bottom: 1px solid var(--lo-divider); font-size: 10px; letter-spacing: 0.1em; color: var(--lo-dim); }
.lo-ns-header input { width: 100px; background: var(--lo-bg-deep); border: 1px solid var(--lo-divider); color: var(--lo-ink); font-family: var(--font-mono); font-size: 10px; padding: 3px 6px; margin-left: 8px; }
.lo-ns-sort { display: flex; gap: 2px; padding: 6px 12px; border-bottom: 1px solid var(--lo-divider); }
.lo-ns-sort button { background: none; border: 1px solid var(--lo-divider); color: var(--lo-faint); font-family: var(--font-mono); font-size: 9px; padding: 2px 6px; cursor: pointer; letter-spacing: 0.05em; }
.lo-ns-sort button.active { color: var(--lo-ink); border-color: var(--lo-accent); }
.lo-ns-list { flex: 1; overflow-y: auto; }
.lo-ns-row { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-bottom: 1px solid var(--lo-divider); cursor: pointer; font-size: 10px; color: var(--lo-dim); }
.lo-ns-row:hover { background: var(--lo-bg-deep); }
.lo-ns-row .lo-ns-name { flex: 1; color: var(--lo-ink); font-weight: 500; }
.lo-ns-row .lo-ns-hops { color: var(--lo-faint); font-size: 9px; }
.lo-ns-row .lo-ns-heard { color: var(--lo-faint); font-size: 9px; }
.lo-ns-row .lo-ns-badge { background: var(--lo-accent); color: var(--lo-bg); font-size: 8px; font-weight: 500; padding: 1px 4px; min-width: 14px; text-align: center; }
.lo-ns-proto { display: inline-block; font-size: 8px; font-weight: 600; padding: 1px 4px; margin-right: 4px; border-radius: 2px; text-transform: uppercase; letter-spacing: 0.5px; vertical-align: baseline; }
.lo-ns-proto-mc { background: #9b59b6; color: #fff; }
.lo-ns-proto-mt { background: var(--lo-accent-2); color: var(--lo-bg-deep); }

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
  <span class="lo-conn" title="Radio backend status — meshtastic (circle) and meshcore (diamond)">
    <span class="lo-conn-row"><span class="lo-dot" id="hdr-mt-dot"></span><span id="hdr-mt-label">MT --</span></span>
    <span class="lo-conn-row"><span class="lo-dot mc" id="hdr-mc-dot"></span><span id="hdr-mc-label">MC --</span></span>
    <button class="lo-conn-add" id="hdr-add-radio" onclick="showAddRadioModal()" title="Add / manage secondary MeshCore radio">+ RADIO</button>
  </span>
  <div class="lo-scope" title="Show nodes from: ALL protocols, or filter to just MESHTASTIC / just MESHCORE">
    <button class="active" data-scope="all" onclick="setScope('all')">ALL</button>
    <button data-scope="mt" onclick="setScope('mt')">MT</button>
    <button data-scope="mc" onclick="setScope('mc')">MC</button>
  </div>
  <div class="lo-filters">
    <button class="active" data-view="mesh" onclick="setView('mesh')">MESH</button>
    <button data-view="traffic" onclick="setView('traffic')">TRAFFIC</button>
    <button data-view="map" onclick="setView('map')">MAP</button>
    <button data-view="ai" onclick="setView('ai')">AI</button>
    <button data-view="bridge" onclick="setView('bridge')">BRIDGE</button>
    <button data-view="config" onclick="setView('config')">CONFIG</button>
  </div>
  <div class="lo-tools">
    <button id="nodes-toggle" title="Node list" onclick="toggleNodeList()">&#9776;</button>
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
    <div style="margin-top:8px;display:flex;gap:4px;flex-wrap:wrap">
      <button class="btn btn-sm" onclick="refreshNodes()" id="hud-refresh-btn" style="pointer-events:auto">SCAN MESH</button>
      <button class="btn btn-sm" onclick="toggleHwColor()" id="hud-hwcolor-btn" style="pointer-events:auto" title="Color nodes by hardware model">HW COLOR</button>
    </div>
    <div id="hw-legend" style="display:none;margin-top:6px;font-size:9px;color:var(--lo-dim);letter-spacing:0.06em"></div>
    <div id="proto-legend" style="margin-top:8px;font-size:9px;color:var(--lo-dim);letter-spacing:0.06em;display:flex;gap:12px;align-items:center">
      <span style="display:flex;align-items:center;gap:5px"><span style="width:9px;height:9px;border-radius:50%;background:var(--lo-accent-2);display:inline-block"></span>MESHTASTIC</span>
      <span style="display:flex;align-items:center;gap:5px"><span style="width:9px;height:9px;background:#9b59b6;display:inline-block;transform:rotate(45deg)"></span>MESHCORE</span>
    </div>
  </div>

  <!-- Hop ring legend -->
  <div class="lo-hop-legend" id="hop-legend"></div>

  <!-- Floating node windows rendered here by JS -->
  <div id="float-windows" style="position:absolute;inset:0;pointer-events:none;z-index:80"></div>

  <!-- Node list sidebar -->
  <div class="lo-node-sidebar" id="node-sidebar">
    <div class="lo-ns-header">
      <span>NODES</span>
      <input type="text" id="ns-search" placeholder="filter..." oninput="renderNodeList()">
    </div>
    <div class="lo-ns-sort">
      <button class="active" onclick="setNodeSort('name',this)">NAME</button>
      <button onclick="setNodeSort('hops',this)">HOPS</button>
      <button onclick="setNodeSort('heard',this)">HEARD</button>
      <button onclick="setNodeSort('unread',this)">UNREAD</button>
    </div>
    <div class="lo-ns-list" id="ns-list"></div>
    <div style="border-top:2px solid var(--lo-divider-strong);padding:8px 12px">
      <input type="text" id="ns-msg-search" placeholder="search messages..." style="width:100%;background:var(--lo-bg-deep);border:1px solid var(--lo-divider);color:var(--lo-ink);font-family:var(--font-mono);font-size:10px;padding:4px 6px;box-sizing:border-box" oninput="debounceMessageSearch()">
      <div id="ns-msg-results" style="max-height:200px;overflow-y:auto;font-size:10px;color:var(--lo-dim);margin-top:4px"></div>
    </div>
  </div>
</div>

<!-- ── Map View ───────────────────────────────────────────────────────────── -->
<div id="map-view" style="display:none"></div>
<div id="map-controls" style="display:none;position:absolute;top:42px;right:12px;z-index:500">
  <button class="btn btn-sm" onclick="toggleCoverageLayer()" title="Toggle coverage heatmap">COVERAGE</button>
</div>

<!-- ── AI Chat View ───────────────────────────────────────────────────────── -->
<div id="ai-view" class="lo-ai-view" style="display:none">
  <div class="lo-ai-header">
    <div class="lo-ai-title">
      <span>LOCAL AI CHAT</span>
      <span class="lo-ai-model" id="ai-model-label">--</span>
    </div>
    <div class="lo-ai-actions">
      <button class="btn btn-sm" onclick="aiClearHistory()" title="Start a fresh conversation">CLEAR</button>
    </div>
  </div>
  <div class="lo-ai-messages" id="ai-messages">
    <div class="lo-ai-empty">
      <div>Chat with your local AI model running on this computer.</div>
      <div style="margin-top:4px;color:var(--lo-faint);font-size:10px">No radio needed — conversation stays entirely on-device.</div>
    </div>
  </div>
  <form class="lo-ai-composer" id="ai-composer" onsubmit="event.preventDefault(); aiSend();">
    <span class="lo-prompt">&gt;</span>
    <input type="text" id="ai-input" placeholder="ask anything..." autocomplete="off">
    <button type="submit" class="lo-send" id="ai-send-btn">SEND</button>
  </form>
</div>

<!-- ── BRIDGE View (LORACLE v2) ────────────────────────────────────────────── -->
<div id="bridge-view" class="lo-bridge-view" style="display:none">
  <div class="lo-bridge-header">
    <div class="lo-bridge-title">
      <span>CROSS-PROTOCOL BRIDGE</span>
      <span class="lo-bridge-badge" id="bridge-enabled-badge">OFF</span>
    </div>
    <div class="lo-bridge-stats" id="bridge-stats">
      <span>relayed: <b id="bridge-relayed">0</b></span>
      <span>dropped: <b id="bridge-dropped">0</b></span>
      <span>dedup: <b id="bridge-dedup">0</b></span>
    </div>
  </div>

  <div class="lo-bridge-panel">
    <!-- Simple one-click toggle for the common case — public channel 0 both ways. -->
    <div class="lo-bridge-section" style="border-color:var(--lo-accent)">
      <div class="lo-bridge-section-title" style="color:var(--lo-accent)">AUTO-BRIDGE PUBLIC CHANNEL</div>
      <label class="lo-bridge-row" style="align-items:center;gap:10px;cursor:pointer">
        <input type="checkbox" id="bridge-simple-toggle" onchange="bridgeSimpleToggle(this.checked)" style="width:16px;height:16px;accent-color:var(--lo-accent)">
        <span style="font-size:13px;color:var(--lo-ink);font-weight:500">Auto-relay public channel 0 between Meshtastic and MeshCore</span>
      </label>
      <div class="lo-bridge-hint" style="margin-top:8px;line-height:1.6">
        When on, every message sent on public channel&nbsp;0 on one radio is automatically retransmitted on the other, tagged <code style="background:var(--lo-bg);padding:0 4px">from meshtastic (Alice):&nbsp;…</code> or <code style="background:var(--lo-bg);padding:0 4px">from meshcore (…):&nbsp;…</code> so recipients on the other network see where it came from. DMs never cross.
      </div>
      <div id="bridge-simple-status" style="font-size:10px;color:var(--lo-faint);margin-top:6px"></div>
    </div>

    <details id="bridge-advanced" class="lo-bridge-section" style="padding:0;border-style:dashed">
      <summary style="padding:10px 14px;cursor:pointer;font-size:10px;letter-spacing:0.14em;color:var(--lo-dim);user-select:none">ADVANCED RULES (most users don't need this)</summary>
      <div style="padding:12px 14px;border-top:1px dashed var(--lo-divider)">
        <label class="lo-bridge-row">
          <input type="checkbox" id="bridge-enabled" onchange="bridgeMarkDirty()">
          <span>Master relay enabled</span>
          <span class="lo-bridge-hint">When off, no messages cross regardless of per-channel rules.</span>
        </label>
        <div class="lo-bridge-hint" style="margin:12px 0 8px">
          Each rule below decides whether channel broadcasts from one network cross to the other. DMs never relay. Use the simple toggle above for the normal case — this panel is only needed for multi-channel setups or the AI-gated urgency filter.
        </div>
        <div id="bridge-rules-list"></div>
        <button class="btn btn-sm" onclick="bridgeAddRule()" style="margin-top:8px">+ ADD RULE</button>
        <div style="margin-top:10px;display:flex;gap:8px">
          <button class="btn btn-sm" onclick="bridgeSaveConfig()" id="bridge-save-btn">APPLY</button>
          <button class="btn btn-sm" onclick="bridgeReloadConfig()">RELOAD</button>
          <span id="bridge-save-status" style="align-self:center;color:var(--lo-faint);font-size:11px"></span>
        </div>
      </div>
    </details>

    <div class="lo-bridge-section">
      <div class="lo-bridge-section-title">LIVE FLOW</div>
      <div class="lo-bridge-hint" style="margin-bottom:8px">Last 200 relay events. Newest on top.</div>
      <div id="bridge-flow-log" class="lo-bridge-flow"></div>
    </div>
  </div>
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
          <button class="btn" id="cfg-reboot-btn" onclick="cfgReboot()" style="display:none">REBOOT</button>
          <button class="btn" id="cfg-shutdown-btn" onclick="cfgShutdown()" style="display:none;border-color:#c0392b;color:#c0392b">SHUTDOWN</button>
        </div>
      </div>
    </div>
  </details>

  <!-- Channels -->
  <details class="lo-section">
    <summary class="lo-section-head">CHANNELS</summary>
    <div class="lo-section-body">
      <div id="cfg-channels-list" style="font-size:11px;color:var(--lo-dim)">Loading...</div>
      <div class="lo-form-row" style="margin-top:8px"><button class="btn btn-sm" onclick="cfgLoadChannels()">REFRESH</button></div>
    </div>
  </details>

  <!-- Radio Config -->
  <details class="lo-section">
    <summary class="lo-section-head">RADIO</summary>
    <div class="lo-section-body">
      <div class="lo-form-row"><span class="lo-form-label">REGION</span><select id="cfg-radio-region" onchange="markConfigDirty()"><option value="0">Unset</option><option value="1">US</option><option value="2">EU_433</option><option value="3">EU_868</option><option value="4">CN</option><option value="5">JP</option><option value="6">ANZ</option><option value="7">KR</option><option value="8">TW</option><option value="9">RU</option><option value="10">IN</option><option value="11">NZ_865</option><option value="12">TH</option><option value="13">LORA_24</option><option value="14">UA_433</option><option value="15">UA_868</option><option value="16">MY_433</option><option value="17">MY_919</option><option value="18">SG_923</option></select></div>
      <div class="lo-form-row"><span class="lo-form-label">MODEM PRESET</span><select id="cfg-radio-modem" onchange="markConfigDirty()"><option value="0">Long Fast</option><option value="1">Long Slow</option><option value="2">Very Long Slow</option><option value="3">Medium Slow</option><option value="4">Medium Fast</option><option value="5">Short Slow</option><option value="6">Short Fast</option><option value="7">Long Moderate</option></select></div>
      <div class="lo-form-row"><span class="lo-form-label">TX POWER</span><input type="number" id="cfg-radio-tx" min="0" max="30" style="width:60px" onchange="markConfigDirty()"> <span style="font-size:10px;color:var(--lo-faint)">dBm</span></div>
      <div class="lo-form-row"><span class="lo-form-label">MAX HOPS</span><input type="number" id="cfg-radio-hops" min="1" max="7" style="width:60px" onchange="markConfigDirty()"></div>
      <div class="lo-form-row"><span class="lo-form-label"></span><button id="cfg-radio-save" class="btn btn-primary" onclick="cfgSaveRadio()">SAVE RADIO CONFIG</button> <button class="btn btn-sm" onclick="cfgLoadRadio()">REFRESH</button></div>
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
    <h3 id="connect-modal-title">CONNECT A RADIO</h3>
    <p id="connect-modal-desc">No radio detected. Plug in a USB radio, or scan for nearby Bluetooth devices.</p>

    <!-- FORM PANEL — shown while the user picks protocol/transport/address -->
    <div id="connect-modal-form">
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
        <div style="flex:1;display:flex;gap:6px">
          <input type="text" id="connect-address" placeholder="auto-detect (or /dev/...)" style="flex:1">
          <button class="btn btn-sm" id="connect-serial-scan-btn" onclick="connectModalSerialScan()" style="display:none;white-space:nowrap">SCAN PORTS</button>
        </div>
      </div>
      <div class="lo-form-row" id="connect-serial-list-row" style="display:none">
        <span class="lo-form-label"></span>
        <div id="connect-serial-list" style="flex:1;font-size:10px;color:var(--lo-dim)"></div>
      </div>
      <div class="lo-form-row" id="connect-scan-row">
        <span class="lo-form-label">DEVICES</span>
        <div style="flex:1">
          <button class="btn btn-sm" id="connect-scan-btn" onclick="connectModalScan()">SCAN FOR DEVICES</button>
          <div id="connect-scan-status" style="font-size:10px;color:var(--lo-dim);margin-top:6px"></div>
          <div id="connect-scan-list" style="margin-top:6px"></div>
        </div>
      </div>
      <div id="connect-modal-wizard-step" style="display:none;font-size:10px;letter-spacing:0.12em;color:var(--lo-accent);margin-bottom:6px">STEP 1 OF 2 — PRIMARY RADIO</div>
      <div class="lo-form-row" style="justify-content:space-between;margin-top:8px">
        <button class="btn" id="connect-modal-dismiss-btn" onclick="dismissConnectModal()">DISMISS</button>
        <div style="display:flex;gap:6px">
          <button class="btn" id="connect-modal-skip-btn" onclick="wizardSkipPrimary()" style="display:none">SKIP — MESHCORE ONLY</button>
          <button class="btn btn-primary" id="connect-modal-btn" onclick="connectFromModal()">CONNECT</button>
        </div>
      </div>
      <div id="connect-modal-status" style="font-size:10px;color:var(--lo-dim);margin-top:8px"></div>
    </div>

    <!-- SUCCESS PANEL — swapped in after a successful primary connect during the wizard -->
    <div id="connect-modal-success" style="display:none;text-align:center;padding:10px 0">
      <div style="font-size:42px;color:var(--lo-accent-2);line-height:1">&#x2713;</div>
      <h3 style="margin:12px 0 8px 0;font-size:14px;color:var(--lo-accent-2);letter-spacing:0.1em">MESHTASTIC CONNECTED</h3>
      <p style="margin:0 0 16px 0;color:var(--lo-dim);font-size:11px;line-height:1.7">
        Your first radio is up. Want to reach the other network too? Add a MeshCore radio —
        <strong style="color:var(--lo-ink)">optional</strong>. With both connected, MT and MC
        peers can message each other through the auto-bridge.
      </p>
      <div id="connect-modal-success-detail" style="font-size:10px;color:var(--lo-faint);margin-bottom:14px"></div>
      <div style="display:flex;justify-content:space-between;gap:8px">
        <button class="btn" onclick="wizardPrimaryDone()">DONE — JUST MESHTASTIC</button>
        <button class="btn btn-primary" onclick="wizardAdvanceFromPrimarySuccess()">NEXT — ADD MESHCORE &rarr;</button>
      </div>
    </div>
  </div>
</div>

<!-- ── Add Secondary Radio Modal ─────────────────────────────────────────── -->
<div class="lo-connect-modal" id="add-radio-modal">
  <div class="lo-connect-box">
    <div id="ar-wizard-step" style="display:none;font-size:10px;letter-spacing:0.12em;color:var(--lo-accent);margin-bottom:6px">STEP 2 OF 2 — SECOND RADIO (OPTIONAL)</div>
    <h3 id="ar-title">ADD A SECOND RADIO</h3>
    <p id="ar-description">Attach a MeshCore radio alongside your first one. Once both are connected, public-channel (channel&nbsp;0) messages auto-bridge in both directions — each relay is tagged <code style="background:var(--lo-bg-deep);padding:0 4px">from meshcore (…)</code> or <code style="background:var(--lo-bg-deep);padding:0 4px">from meshtastic (…)</code> so recipients see which network it came from.</p>
    <div id="ar-active-row" style="display:none;margin-bottom:14px;padding:10px;background:var(--lo-bg-deep);font-size:11px">
      <div style="color:#9b59b6;font-weight:500;margin-bottom:4px;display:flex;align-items:center;gap:8px">
        <span style="display:inline-block;width:8px;height:8px;background:#9b59b6;transform:rotate(45deg);flex-shrink:0"></span>
        <span id="ar-active-label">MESHCORE CONNECTED</span>
      </div>
      <div id="ar-active-detail" style="color:var(--lo-dim)"></div>
      <button class="btn btn-sm" onclick="removeSecondaryRadio()" style="margin-top:8px;color:#c0392b;border-color:#c0392b">DISCONNECT</button>
    </div>

    <!-- FORM PANEL — collected into a div so we can swap a success panel in over the top. -->
    <div id="ar-form">
    <div class="lo-form-row">
      <span class="lo-form-label">PROTOCOL</span>
      <select id="ar-protocol" style="max-width:140px" disabled><option value="meshcore">MeshCore</option></select>
    </div>
    <div class="lo-form-row">
      <span class="lo-form-label">TRANSPORT</span>
      <select id="ar-transport" style="max-width:140px" onchange="arTransportChanged()">
        <option value="serial" selected>Serial (USB)</option>
        <option value="tcp">TCP</option>
        <option value="ble">Bluetooth (BLE)</option>
      </select>
    </div>
    <div class="lo-form-row" id="ar-serial-row">
      <span class="lo-form-label">DEVICE</span>
      <div style="flex:1;display:flex;gap:6px">
        <input type="text" id="ar-serial-port" placeholder="/dev/ttyUSB1 or COM4" style="flex:1">
        <button class="btn btn-sm" onclick="addRadioSerialScan()" style="white-space:nowrap">SCAN PORTS</button>
      </div>
    </div>
    <div class="lo-form-row" id="ar-serial-list-row" style="display:none">
      <span class="lo-form-label"></span>
      <div id="ar-serial-list" style="flex:1;font-size:10px;color:var(--lo-dim)"></div>
    </div>
    <div class="lo-form-row" id="ar-tcp-row" style="display:none">
      <span class="lo-form-label">HOST</span>
      <div style="flex:1;display:flex;gap:6px">
        <input type="text" id="ar-tcp-host" placeholder="192.168.1.50" style="flex:1">
        <input type="number" id="ar-tcp-port" placeholder="4000" value="4000" style="width:80px">
      </div>
    </div>
    <div class="lo-form-row" id="ar-ble-row" style="display:none">
      <span class="lo-form-label">BLE ADDR</span>
      <input type="text" id="ar-ble-address" placeholder="AA:BB:CC:DD:EE:FF (blank = scan)">
    </div>
    <div class="lo-form-row" style="align-items:center">
      <span class="lo-form-label">BRIDGE</span>
      <label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--lo-dim)">
        <input type="checkbox" id="ar-seed-bridge" checked>
        Auto-relay public channel 0 both directions
      </label>
    </div>
    <div class="lo-form-row" style="justify-content:space-between;margin-top:8px">
      <button class="btn" id="ar-cancel-btn" onclick="hideAddRadioModal()">CANCEL</button>
      <div style="display:flex;gap:6px">
        <button class="btn" id="ar-skip-btn" onclick="wizardSkipSecondary()" style="display:none">SKIP — SINGLE RADIO</button>
        <button class="btn btn-primary" id="ar-submit-btn" onclick="submitAddRadio()">CONNECT</button>
      </div>
    </div>
    <div id="ar-status" style="font-size:10px;color:var(--lo-dim);margin-top:8px"></div>
    </div><!-- /#ar-form -->

    <!-- SUCCESS PANEL — swapped in after a successful MC connect -->
    <div id="ar-success" style="display:none;text-align:center;padding:10px 0">
      <div style="font-size:42px;color:#9b59b6;line-height:1">&#x25C6;</div>
      <h3 style="margin:12px 0 8px 0;font-size:14px;color:#9b59b6;letter-spacing:0.1em">MESHCORE CONNECTED</h3>
      <p id="ar-success-desc" style="margin:0 0 12px 0;color:var(--lo-dim);font-size:11px;line-height:1.7">
        Both radios are up. Public channel 0 is auto-bridging in both directions —
        messages will be tagged <code style="background:var(--lo-bg-deep);padding:0 4px">from meshtastic (…)</code>
        or <code style="background:var(--lo-bg-deep);padding:0 4px">from meshcore (…)</code> on the other side.
      </p>
      <div id="ar-success-detail" style="font-size:10px;color:var(--lo-faint);margin-bottom:14px"></div>
      <div style="display:flex;justify-content:center;gap:8px">
        <button class="btn btn-primary" onclick="wizardFinishFromSecondarySuccess()">DONE &rarr;</button>
      </div>
    </div>
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
      <h3>WELCOME TO LORACLE BRIDGE</h3>
      <p>Offline AI over mesh radio. The orange dot is your radio. Green dots are peers on the mesh. Purple dots are MeshCore nodes. Connect your radio first, then explore.</p>
    </div>

    <!-- IMPORTANT: first-use disclaimer — users with fresh-from-factory radios will
         often have no region/frequency set and the mesh will look dead even after
         LORACLE successfully attaches. Surfacing this early saves a support loop. -->
    <div class="lo-ob-step" data-step="1">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <polygon points="180,18 220,92 140,92" fill="none" stroke="#d4a017" stroke-width="2"/>
        <text x="180" y="78" text-anchor="middle" fill="#d4a017" font-family="var(--font-mono)" font-size="30" font-weight="500">!</text>
      </svg>
      <h3 style="color:#d4a017">BEFORE YOU CONNECT</h3>
      <p><strong style="color:var(--lo-ink)">Do initial radio setup in the native app first.</strong> Install the official <strong>Meshtastic</strong> app (or <strong>MeshCore</strong> app) and pair your radio over BLE/USB. Set your region / frequency, pick a short name, configure any channel keys, and confirm the radio is talking on the mesh. LORACLE Bridge reads and drives the radio — it isn't a first-time-setup tool, and a radio without a region set will look connected but receive nothing.</p>
    </div>

    <div class="lo-ob-step" data-step="2">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <circle cx="180" cy="60" r="6" fill="var(--lo-accent)"/>
        <circle cx="120" cy="50" r="4" fill="var(--lo-accent-2)"/>
        <line x1="180" y1="60" x2="120" y2="50" stroke="var(--lo-accent-2)" stroke-width="0.8" opacity="0.5"/>
        <circle cx="70" cy="70" r="3" fill="var(--lo-accent-2)"/>
        <line x1="120" y1="50" x2="70" y2="70" stroke="var(--lo-accent-2)" stroke-width="0.8" opacity="0.5"/>
        <circle cx="250" cy="40" r="4" fill="var(--lo-accent-2)"/>
        <line x1="180" y1="60" x2="250" y2="40" stroke="var(--lo-accent-2)" stroke-width="0.8" opacity="0.5"/>
        <circle cx="310" cy="30" r="3" fill="var(--lo-accent-2)" opacity="0.5"/>
        <line x1="250" y1="40" x2="310" y2="30" stroke="var(--lo-accent-2)" stroke-width="0.8" opacity="0.3"/>
      </svg>
      <h3>ORGANIC MESH TOPOLOGY</h3>
      <p>Direct peers connect to your node. Multi-hop nodes chain through the radios they relay off of. Nodes with GPS are positioned geographically — the layout mirrors your actual network.</p>
    </div>

    <div class="lo-ob-step" data-step="3">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <circle cx="120" cy="60" r="5" fill="var(--lo-accent-2)"/>
        <text x="120" y="80" text-anchor="middle" fill="var(--lo-ink)" font-family="var(--font-mono)" font-size="7">FRESH</text>
        <circle cx="200" cy="60" r="4" fill="var(--lo-accent-2)" opacity="0.6"/>
        <text x="200" y="80" text-anchor="middle" fill="var(--lo-dim)" font-family="var(--font-mono)" font-size="7">30 MIN</text>
        <circle cx="280" cy="60" r="3" fill="var(--lo-accent-2)" opacity="0.3"/>
        <text x="280" y="80" text-anchor="middle" fill="var(--lo-faint)" font-family="var(--font-mono)" font-size="7">STALE</text>
      </svg>
      <h3>FRESH NODES GLOW</h3>
      <p>Recently heard nodes are bright and pulse gently. Nodes go dim as they age out. After 1 hour of silence, they fade to ghosts. It's easy to tell who's alive at a glance.</p>
    </div>

    <div class="lo-ob-step" data-step="4">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <rect x="120" y="20" width="120" height="80" fill="none" stroke="var(--lo-divider-strong)" stroke-width="1"/>
        <text x="180" y="40" text-anchor="middle" fill="var(--lo-ink)" font-family="var(--font-mono)" font-size="9" font-weight="500">NODE 3a7b</text>
        <text x="180" y="55" text-anchor="middle" fill="var(--lo-dim)" font-family="var(--font-mono)" font-size="7">HOPS: 2H</text>
        <text x="180" y="68" text-anchor="middle" fill="var(--lo-dim)" font-family="var(--font-mono)" font-size="7">BATT: 87%</text>
        <text x="180" y="85" text-anchor="middle" fill="var(--lo-faint)" font-family="var(--font-mono)" font-size="6">&gt; message...</text>
      </svg>
      <h3>CLICK ANY NODE</h3>
      <p>Opens a floating window with signal info, battery, GPS, hops, and a message thread. Send DMs, toggle AI auto-reply, run a traceroute, or view their chat history.</p>
    </div>

    <div class="lo-ob-step" data-step="5">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <circle cx="180" cy="60" r="12" fill="none" stroke="var(--lo-accent-2)" stroke-width="2"/>
        <text x="180" y="64" text-anchor="middle" fill="var(--lo-accent-2)" font-family="var(--font-mono)" font-size="7">\u25C9</text>
        <text x="180" y="95" text-anchor="middle" fill="var(--lo-accent-2)" font-family="var(--font-mono)" font-size="8" font-weight="500">PUBLIC</text>
        <circle cx="100" cy="45" r="3" fill="var(--lo-accent-2)"/>
        <circle cx="260" cy="40" r="3" fill="var(--lo-accent-2)"/>
        <circle cx="90" cy="85" r="3" fill="var(--lo-accent-2)"/>
        <circle cx="270" cy="80" r="3" fill="var(--lo-accent-2)"/>
      </svg>
      <h3>PUBLIC CHANNEL</h3>
      <p>The ringed circle labeled PUBLIC is channel 0 — everyone on the mesh can see broadcasts there. Click it to send a message that reaches every node in range.</p>
    </div>

    <div class="lo-ob-step" data-step="6">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <rect x="60" y="20" width="240" height="80" fill="none" stroke="var(--lo-divider)" stroke-width="0.5"/>
        <circle cx="100" cy="50" r="3" fill="var(--lo-accent-2)"/>
        <circle cx="160" cy="70" r="3" fill="var(--lo-accent)"/>
        <circle cx="230" cy="45" r="3" fill="var(--lo-accent-2)"/>
        <circle cx="270" cy="80" r="3" fill="var(--lo-accent-2)"/>
        <text x="180" y="110" text-anchor="middle" fill="var(--lo-dim)" font-family="var(--font-mono)" font-size="7">MAP VIEW</text>
      </svg>
      <h3>MAP, TRAFFIC &amp; SIDEBAR</h3>
      <p>MAP shows nodes on a real map with coverage heatmap. TRAFFIC dims quiet nodes so active ones pop. The \u2630 sidebar lists every node — sort, filter, and search messages across all threads.</p>
    </div>

    <div class="lo-ob-step" data-step="7">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <rect x="110" y="25" width="140" height="22" fill="var(--lo-accent)" opacity="0.15"/>
        <text x="180" y="40" text-anchor="middle" fill="var(--lo-accent)" font-family="var(--font-mono)" font-size="8" font-weight="500">!help</text>
        <rect x="90" y="55" width="180" height="22" fill="var(--lo-accent-2)" opacity="0.15"/>
        <text x="180" y="70" text-anchor="middle" fill="var(--lo-ink)" font-family="var(--font-mono)" font-size="7">LORACLE answers from local LLM</text>
        <text x="180" y="100" text-anchor="middle" fill="var(--lo-faint)" font-family="var(--font-mono)" font-size="6">fully offline \u00b7 grounded in your docs</text>
      </svg>
      <h3>LORACLE AI</h3>
      <p>Anyone on the mesh can DM your radio to chat with a local LLM. Drop PDFs in CONTEXT FILES/ for knowledge base grounding. Commands: !help, !nav, !triage, !brief, !drop.</p>
    </div>

    <div class="lo-ob-step" data-step="8">
      <svg viewBox="0 0 360 120" xmlns="http://www.w3.org/2000/svg">
        <rect x="80" y="20" width="200" height="16" fill="none" stroke="var(--lo-divider-strong)" stroke-width="0.5"/>
        <text x="85" y="32" fill="var(--lo-dim)" font-family="var(--font-mono)" font-size="7">CONNECTION</text>
        <rect x="80" y="44" width="200" height="16" fill="none" stroke="var(--lo-divider)" stroke-width="0.5"/>
        <text x="85" y="56" fill="var(--lo-dim)" font-family="var(--font-mono)" font-size="7">CHANNELS &amp; RADIO</text>
        <rect x="80" y="68" width="200" height="16" fill="none" stroke="var(--lo-divider)" stroke-width="0.5"/>
        <text x="85" y="80" fill="var(--lo-dim)" font-family="var(--font-mono)" font-size="7">MODEL ROUTING &amp; RAG</text>
        <rect x="80" y="92" width="200" height="16" fill="none" stroke="var(--lo-divider)" stroke-width="0.5"/>
        <text x="85" y="104" fill="var(--lo-dim)" font-family="var(--font-mono)" font-size="7">DEVICE ADMIN</text>
      </svg>
      <h3>CONFIG HAS EVERYTHING</h3>
      <p>Connection type, channel management, LoRa radio settings, AI model routing, knowledge packs, device reboot/shutdown. You can relaunch this tour any time from CONFIG &gt; APPEARANCE.</p>
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
  scope: (function() { try { return localStorage.getItem('loracle_scope') || 'all'; } catch(e) { return 'all'; } })(),  // 'all' | 'mt' | 'mc' — filters nodes/self by protocol across every view
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
  panX: 0,
  panY: 0,
  unreadCounts: {},
};

// Scope filter — treats MT and MC as equal citizens. A node is in-scope when:
//   'all' → always; 'mt' → node is meshtastic (not mc:); 'mc' → node is meshcore (mc:)
// Channels get classified by the same prefix rule: 'meshtastic:channel:0' is MT,
// 'mc:channel:1' is MC. Self-nodes use the backend's own protocol via n.isMC.
function nodeInScope(n) {
  if (!n) return true;
  var s = App.scope || 'all';
  if (s === 'all') return true;
  var isMC = !!n.isMC;
  return s === 'mc' ? isMC : !isMC;
}

function setScope(scope) {
  if (scope !== 'all' && scope !== 'mt' && scope !== 'mc') scope = 'all';
  App.scope = scope;
  try { localStorage.setItem('loracle_scope', scope); } catch(e) {}
  document.querySelectorAll('.lo-scope button').forEach(function(b) {
    b.classList.toggle('active', b.dataset.scope === scope);
  });
  // Canvas repaints every frame (nodeInScope is applied at draw-time), but the
  // sidebar list is event-driven so nudge it. Map view layers also need a refresh.
  try { renderNodeList(); } catch(e) {}
  try { if (typeof updateMapMarkers === 'function') updateMapMarkers(); } catch(e) {}
}

// Sync the scope button visual state on first paint so a stored preference
// reflects in the top bar immediately.
document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('.lo-scope button').forEach(function(b) {
    b.classList.toggle('active', b.dataset.scope === (App.scope || 'all'));
  });
});

// ─── Utilities ─────────────────────────────────────────────────────────────

function escapeHtml(s) { return s ? String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') : ''; }

// Render a delivery-status pill for outbound messages.
//  - sending: ⧗ in-flight
//  - sent:    → (single arrow)
//  - acked:   ✓ radio acked
//  - delivered: ✓✓ recipient confirmed
//  - failed:  ✗ send failed
// Inbound messages get no status.
function renderDeliveryStatus(m) {
  if (!m || m.direction !== 'out') return '';
  var ds = m.delivery_status;
  if (!ds) return '';
  var map = {
    'sending':   { glyph: '\u29d7', cls: 'sending', title: 'Sending…' },
    'sent':      { glyph: '\u2192',  cls: 'sent',    title: 'Sent over radio' },
    'acked':     { glyph: '\u2713',  cls: 'acked',   title: 'Radio ACK' },
    'delivered': { glyph: '\u2713\u2713', cls: 'delivered', title: 'Delivered' },
    'failed':    { glyph: '\u2717',  cls: 'failed',  title: 'Send failed' },
  };
  var info = map[ds];
  if (!info) return '';
  return ' <span class="lo-msg-status ' + info.cls + '" title="' + info.title + '">' + info.glyph + '</span>';
}
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
  var isCanvas = (view === 'mesh' || view === 'traffic');
  document.getElementById('canvas-wrap').style.display = isCanvas ? '' : 'none';
  document.getElementById('map-view').style.display = (view === 'map') ? '' : 'none';
  document.getElementById('map-controls').style.display = (view === 'map') ? '' : 'none';
  var aiView = document.getElementById('ai-view');
  if (aiView) aiView.style.display = (view === 'ai') ? '' : 'none';
  var bridgeView = document.getElementById('bridge-view');
  if (bridgeView) bridgeView.style.display = (view === 'bridge') ? '' : 'none';
  // Hide the ribbon on config / AI / bridge (none care about per-packet activity)
  document.querySelector('.lo-ribbon').style.display = (view === 'config' || view === 'ai' || view === 'bridge') ? 'none' : '';
  document.getElementById('config-view').classList.toggle('active', view === 'config');
  document.getElementById('hud').style.display = isCanvas ? '' : 'none';
  if (view === 'config' && !App.configLoaded) { App.configLoaded = true; loadConfigData(); }
  if (view === 'map') initMap();
  if (view === 'ai') aiActivate();
  if (view === 'bridge') bridgeActivate();
  if (isCanvas) resizeCanvas();
}

// ─── Canvas Setup ──────────────────────────────────────────────────────────

function initCanvas() {
  App.canvas = document.getElementById('mesh-canvas');
  App.ctx = App.canvas.getContext('2d');
  resizeCanvas();
  window.addEventListener('resize', resizeCanvas);
  App.canvas.addEventListener('mousedown', function(e) {
    if (e.button !== 0) return;
    _panStart = {x: e.clientX, y: e.clientY, panX: App.panX, panY: App.panY};
    _isPanning = false;
  });
  App.canvas.addEventListener('mousemove', function(e) {
    var rect = App.canvas.getBoundingClientRect();
    _mouseX = e.clientX - rect.left;
    _mouseY = e.clientY - rect.top;
    if (_panStart) {
      var dx = e.clientX - _panStart.x, dy = e.clientY - _panStart.y;
      if (Math.abs(dx) > 4 || Math.abs(dy) > 4) _isPanning = true;
      if (_isPanning) { App.panX = _panStart.panX + dx; App.panY = _panStart.panY + dy; }
    } else {
      _updateHoverCursor(_mouseX, _mouseY);
    }
  });
  App.canvas.addEventListener('mouseup', function() { _panStart = null; });
  App.canvas.addEventListener('mouseleave', function() { _panStart = null; });
  App.canvas.addEventListener('click', onCanvasClick);
  App.canvas.addEventListener('dblclick', function() { App.panX = 0; App.panY = 0; });
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
  // Collect every backend's self-node id so we can keep each "my radio" centered
  // and out of the peer list. Legacy selfId (first backend) is still used as a fallback.
  var selfIds = {};
  backends.forEach(function(b) { if (b.self_node_id) selfIds[b.self_node_id] = true; });
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

  // Compute GPS bounds for position-based layout
  var _gpsBounds = {minLat: 90, maxLat: -90, minLon: 180, maxLon: -180, latSpan: 0, lonSpan: 0};
  Object.keys(positions).forEach(function(k) {
    var p = positions[k];
    if (p.lat && p.lon) {
      if (p.lat < _gpsBounds.minLat) _gpsBounds.minLat = p.lat;
      if (p.lat > _gpsBounds.maxLat) _gpsBounds.maxLat = p.lat;
      if (p.lon < _gpsBounds.minLon) _gpsBounds.minLon = p.lon;
      if (p.lon > _gpsBounds.maxLon) _gpsBounds.maxLon = p.lon;
    }
  });
  _gpsBounds.latSpan = _gpsBounds.maxLat - _gpsBounds.minLat || 0.001;
  _gpsBounds.lonSpan = _gpsBounds.maxLon - _gpsBounds.minLon || 0.001;

  // Build node list — preserve existing positions if node existed before
  var oldNodeMap = {};
  (App.nodes || []).forEach(function(n) { oldNodeMap[n.id] = n; });

  var nodes = [];
  var nodeMap = {};

  // MY NODES at center — one per connected backend so a meshtastic + meshcore
  // dual-radio rig shows both "self" dots with distinct shape/color.
  var selfBackends = backends.slice(0);
  if (selfBackends.length === 0) {
    // No backend info yet. Pick the fallback protocol from App.scope when the
    // user has filtered to one — so an MC-only viewer doesn't see an empty-MT
    // placeholder in the middle of the canvas. Otherwise leave protocol blank
    // so downstream code doesn't assume MT is the "default".
    var fallbackProto = (App.scope === 'mc') ? 'mc' : (App.scope === 'mt' ? 'mt' : '');
    selfBackends.push({ id: 'primary', protocol: fallbackProto, connected: !!state.connected, self_node_id: null });
  }
  var selfOffsets = selfBackends.length === 1
    ? [{dx: 0, dy: 0}]
    : [{dx: -18, dy: 0}, {dx: 18, dy: 0}];
  var primarySelfKey = null;
  selfBackends.forEach(function(b, i) {
    var protoLc = String(b.protocol || '').toLowerCase();
    var isMC = (protoLc === 'mc' || protoLc === 'meshcore');
    var key = '__self_' + (b.id || (isMC ? 'mc' : 'mt')) + '__';
    if (i === 0) primarySelfKey = key;
    var off = selfOffsets[i] || {dx: 0, dy: 0};
    var selfNode = oldNodeMap[key] || oldNodeMap['__self__'] || { id: key, x: cx + off.dx, y: cy + off.dy };
    selfNode.id = key;
    selfNode.fx = cx + off.dx; selfNode.fy = cy + off.dy;
    selfNode.isSelf = true;
    selfNode.isMC = isMC;
    selfNode.isChannel = false;
    // When only one radio is connected, both protocols show the same generic label;
    // when two are connected, both get protocol-specific labels so MT/MC are equals.
    selfNode.label = (selfBackends.length > 1)
      ? (isMC ? 'MY MC' : 'MY MT')
      : 'MY NODE';
    selfNode.hops = 0;
    selfNode.selfBackendId = b.id || null;
    selfNode.selfProtocol = isMC ? 'mc' : 'mt';
    selfNode.selfNodeId = b.self_node_id || null;
    selfNode.selfConnected = !!b.connected;
    nodes.push(selfNode);
    nodeMap[key] = selfNode;
  });
  // Back-compat alias so existing references to the '__self__' key still resolve.
  nodeMap['__self__'] = nodeMap[primarySelfKey];

  var contactMeta = (state.contact_meta) || {};
  Object.keys(allIds).forEach(function(nid) {
    if (selfIds[nid]) return;
    if (selfId && nid === selfId) return;
    var m = meta[nid] || {};
    var pos = positions[nid] || {};
    var hops = (typeof m.hops === 'number') ? m.hops : null;
    var shortId = nid.length > 8 ? nid.slice(-4) : nid;
    var isChannel = nid.indexOf('channel:') !== -1;
    var isMC = nid.indexOf('mc:') === 0;
    if (isChannel) {
      var chNum = nid.split(':').pop() || '0';
      shortId = (chNum === '0') ? 'PUBLIC' : 'CH ' + chNum;
    }
    var cm = contactMeta[nid] || {};
    var displayLabel = cm.custom_name || m.long_name || m.short_name || shortId;
    var isFav = !!cm.is_favorite;

    // Reuse existing position if available
    var existing = oldNodeMap[nid];
    var node;
    if (existing) {
      node = existing;
      node.hops = hops; node.isChannel = isChannel; node.isMC = isMC; node.label = displayLabel;
      node.isFavorite = isFav;
      node.rssi = m.rssi || pos.rssi || null;
      node.snr = m.snr || pos.snr || null;
      node.lat = pos.lat || null; node.lon = pos.lon || null;
      node.lastHeard = pos.last_update || null;
    } else {
      // New node — place using GPS if available, else hash scatter
      var startX, startY;
      if (pos.lat && pos.lon && _gpsBounds.latSpan > 0) {
        startX = 60 + ((pos.lon - _gpsBounds.minLon) / _gpsBounds.lonSpan) * (App.width - 120);
        startY = 60 + (1 - (pos.lat - _gpsBounds.minLat) / _gpsBounds.latSpan) * (App.height - 120);
      } else {
        var idx = hashStr(nid);
        var angle = idx * 2.399963;
        var spread = 80 + (hashStr(nid + 'r') % 120);
        startX = cx + Math.cos(angle) * spread;
        startY = cy + Math.sin(angle) * spread;
      }
      node = {
        id: nid, label: displayLabel, hops: hops, isChannel: isChannel, isMC: isMC,
        isFavorite: isFav,
        birthTime: performance.now(),
        x: startX, y: startY,
        rssi: m.rssi || pos.rssi || null, snr: m.snr || pos.snr || null,
        lat: pos.lat || null, lon: pos.lon || null,
        lastHeard: pos.last_update || null,
      };
    }
    nodes.push(node);
    nodeMap[nid] = node;
  });

  // Links — tree topology, one parent per node
  // Strategy: sort all peers by hop count, link each to the single
  // geographically closest node that's already linked (fewer hops preferred).
  // This builds a clean tree with no crossing — O(n log n) via pre-sorted arrays.
  // When both MT and MC self-nodes exist, peers prefer a same-protocol ancestor
  // so the two meshes render as visually-separate sub-trees.
  var links = [];
  var linked = {};
  var linkedList = [];
  var selfRoots = nodes.filter(function(n) { return n.isSelf; });
  selfRoots.forEach(function(n) { linked[n.id] = true; linkedList.push(n); });
  var mtRoot = selfRoots.find(function(n) { return !n.isMC; }) || selfRoots[0];
  var mcRoot = selfRoots.find(function(n) { return n.isMC; }) || selfRoots[0];

  function gpsDeg(a, b) {
    if (!a.lat || !b.lat) return Infinity;
    var dlat = a.lat - b.lat, dlon = a.lon - b.lon;
    return dlat * dlat + dlon * dlon;
  }

  // Sort peers: known hops first (ascending), unknowns last
  var peers = nodes.filter(function(n) { return !n.isSelf && !n.isChannel; });
  peers.sort(function(a, b) {
    var ha = (a.hops !== null) ? a.hops : 999;
    var hb = (b.hops !== null) ? b.hops : 999;
    return ha - hb;
  });

  peers.forEach(function(n) {
    var preferredRoot = n.isMC ? mcRoot : mtRoot;
    var best = preferredRoot, bestScore = Infinity;
    // Only check the last 20 linked nodes (nearest in link order = closest hops)
    var check = linkedList.slice(-20);
    check.forEach(function(c) {
      // Prefer same-protocol ancestors so MT and MC trees stay untangled.
      if (!c.isSelf && !!c.isMC !== !!n.isMC) return;
      var d = gpsDeg(n, c);
      if (d < bestScore) { best = c; bestScore = d; }
    });
    // No GPS on either side — hash-assign to a linked same-proto node, falling back to root
    if (bestScore === Infinity) {
      var sameProto = linkedList.filter(function(c) { return c.isSelf || !!c.isMC === !!n.isMC; });
      best = sameProto[hashStr(n.id) % sameProto.length] || preferredRoot;
    }
    links.push({ source: best, target: n });
    linked[n.id] = true;
    linkedList.push(n);
  });

  // Channels link to MY NODE (primary self-root)
  nodes.forEach(function(n) {
    if (n.isChannel) links.push({ source: mtRoot, target: n });
  });

  App.nodes = nodes;
  App.links = links;

  // Rebuild simulation — uniform short link distance for clean tree.
  // Tuned for a "breathing, alive" feel: longer cool-down, lighter damping,
  // and a small jitter force so nodes drift gently even at rest.
  if (App.simulation) App.simulation.stop();
  App.simulation = d3.forceSimulation(nodes)
    .force('charge', d3.forceManyBody().strength(-60).distanceMax(300))
    .force('center', d3.forceCenter(cx, cy).strength(0.02))
    .force('link', d3.forceLink(links).distance(60).strength(0.4))
    .force('collision', d3.forceCollide(25))
    .force('jitter', _jitterForce(0.35))
    .alphaDecay(0.012)          // slower cool-down → longer visible motion
    .velocityDecay(0.32)         // less damping → momentum carries farther
    .alphaMin(0.002)             // keep simulating at very low heat instead of freezing
    .on('tick', function() {});
  // Kick the simulation back to life periodically so the graph always feels alive.
  if (!App._reheatTimer) {
    App._reheatTimer = setInterval(function() {
      if (App.simulation && App.view !== 'config' && App.view !== 'ai') {
        // Small, continuous reheat — not a full restart — so nodes drift without thrashing.
        if (App.simulation.alpha() < 0.08) App.simulation.alpha(0.12).restart();
      }
    }, 6000);
  }
}

// Custom force: per-tick random-walk drift on non-self nodes. Keeps the graph
// alive even when topology hasn't changed. Strength is velocity per tick.
function _jitterForce(strength) {
  var nodes;
  function force(alpha) {
    if (!nodes) return;
    var s = strength * alpha;
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (n.isSelf || n.fx !== undefined) continue;
      n.vx = (n.vx || 0) + (Math.random() - 0.5) * s;
      n.vy = (n.vy || 0) + (Math.random() - 0.5) * s;
    }
  }
  force.initialize = function(_nodes) { nodes = _nodes; };
  return force;
}

function hashStr(s) { var h=0; for(var i=0;i<s.length;i++) h=((h<<5)-h+s.charCodeAt(i))|0; return Math.abs(h); }

// Is this a built-in self-node id (one of '__self__', '__self_<backendId>__')?
function isSelfId(id) { return typeof id === 'string' && id.indexOf('__self') === 0; }

// Draw a diamond (rotated square) at (cx, cy) with half-diagonal r. Used for MeshCore
// nodes so the protocol is readable at a glance even in monochrome.
function drawDiamond(ctx, cx, cy, r) {
  ctx.beginPath();
  ctx.moveTo(cx, cy - r);
  ctx.lineTo(cx + r, cy);
  ctx.lineTo(cx, cy + r);
  ctx.lineTo(cx - r, cy);
  ctx.closePath();
}

// Returns true if any animation needs 60fps rendering (recent message pulse or node entrance <2s old).
function _hasActiveAnimation(nowMs) {
  if (App.state && App.state.messages) {
    var nowS = nowMs / 1000;  // performance.now vs Date.now alignment isn't perfect but good enough
    var walk = Date.now() / 1000;
    for (var i = 0; i < App.state.messages.length; i++) {
      if (walk - App.state.messages[i].ts < 3) return true;
    }
  }
  if (App.nodes) {
    for (var j = 0; j < App.nodes.length; j++) {
      var n = App.nodes[j];
      if (n.birthTime && (nowMs - n.birthTime) < 2000) return true;
    }
  }
  return false;
}

// ─── Canvas Rendering ──────────────────────────────────────────────────────

var _lastRender = 0;
function renderLoop() {
  App.animFrame = requestAnimationFrame(renderLoop);
  // Adaptive framerate: 60fps while animations are active, 30fps at rest.
  var now = performance.now();
  var active = _hasActiveAnimation(now);
  var frameBudget = active ? 16 : 33;
  if (now - _lastRender < frameBudget) return;
  _lastRender = now;

  App.breathPhase += 0.03;
  // Mouse magnetism — only for meshes under 40 nodes
  if (_mouseX > 0 && _mouseY > 0 && !_panStart && App.nodes.length < 40) {
    var wmx = _mouseX - App.panX, wmy = _mouseY - App.panY;
    var closest = null, closestDist = 100;
    App.nodes.forEach(function(n) {
      if (n.isSelf) return;
      if (!nodeInScope(n)) return;
      var dx = wmx - n.x, dy = wmy - n.y;
      var dist = Math.sqrt(dx*dx + dy*dy);
      if (dist < closestDist) { closest = n; closestDist = dist; }
    });
    if (closest) {
      var dx = wmx - closest.x, dy = wmy - closest.y;
      var dist = Math.sqrt(dx*dx + dy*dy);
      if (dist > 5) {
        closest.x += dx * 0.005;
        closest.y += dy * 0.005;
      }
    }
  }
  renderCanvas();
}

var _colorCache = {}, _colorCacheTick = 0;
function getColor(varName) {
  var tick = Math.floor(App.breathPhase / 5); // refresh every ~4 seconds
  if (tick !== _colorCacheTick) { _colorCache = {}; _colorCacheTick = tick; }
  if (!_colorCache[varName]) _colorCache[varName] = getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
  return _colorCache[varName];
}

// Hardware-model palette — applied to node fill when App.colorByHwModel is on.
// Self node (accent) and MeshCore (mcColor) always override these.
var HW_COLORS = {
  'TBEAM': '#3498db', 'TBEAM_V0_7': '#3498db', 'TBEAM_S3_CORE': '#3498db',
  'HELTEC_V3': '#2ecc71', 'HELTEC_V2_1': '#2ecc71', 'HELTEC_V2_0': '#2ecc71',
  'HELTEC_WIRELESS_TRACKER': '#27ae60', 'HELTEC_WSL_V3': '#27ae60',
  'RAK4631': '#f39c12', 'RAK11200': '#f39c12', 'RAK11310': '#f39c12',
  'T_DECK': '#e74c3c', 'T_WATCH_S3': '#e74c3c',
  'STATION_G1': '#1abc9c', 'STATION_G2': '#1abc9c',
  'NANO_G1': '#9b59b6', 'NANO_G1_EXPLORER': '#9b59b6', 'NANO_G2_ULTRA': '#9b59b6',
  'LILYGO_TBEAM_S3_CORE': '#3498db', 'TLORA_V2_1_1P6': '#16a085', 'TLORA_V2_1_1P8': '#16a085',
};
function hwColorFor(nodeId, fallback) {
  var dm = (App.state && App.state.device_metrics) ? App.state.device_metrics[nodeId] : null;
  if (!dm || !dm.hw_model) return fallback;
  var key = String(dm.hw_model).toUpperCase().replace(/[^A-Z0-9_]/g, '_');
  return HW_COLORS[key] || fallback;
}
function nodeFillColor(node, accent2, mcColor) {
  if (node.isMC) return mcColor;  // MeshCore always purple
  if (App.colorByHwModel) return hwColorFor(node.id, accent2);
  return accent2;
}

function toggleHwColor() {
  App.colorByHwModel = !App.colorByHwModel;
  try { localStorage.setItem('loracle-hwcolor', App.colorByHwModel ? '1' : '0'); } catch(e) {}
  var btn = document.getElementById('hud-hwcolor-btn');
  if (btn) btn.classList.toggle('active', App.colorByHwModel);
  renderHwLegend();
}

function renderHwLegend() {
  var el = document.getElementById('hw-legend');
  if (!el) return;
  if (!App.colorByHwModel) { el.style.display = 'none'; el.innerHTML = ''; return; }
  // Build legend from hw_models actually present in the current node set.
  var seen = {};
  if (App.state && App.state.device_metrics) {
    Object.keys(App.state.device_metrics).forEach(function(k) {
      var hw = (App.state.device_metrics[k] || {}).hw_model;
      if (!hw) return;
      var key = String(hw).toUpperCase().replace(/[^A-Z0-9_]/g, '_');
      var color = HW_COLORS[key];
      if (color && !seen[key]) seen[key] = { color: color, label: hw };
    });
  }
  var rows = Object.keys(seen).map(function(k) {
    var s = seen[k];
    return '<div style="display:flex;align-items:center;gap:4px;padding:1px 0">' +
      '<span style="display:inline-block;width:8px;height:8px;background:' + s.color + ';border-radius:50%"></span>' +
      '<span>' + escapeHtml(s.label) + '</span></div>';
  });
  if (rows.length === 0) {
    el.innerHTML = '<div style="color:var(--lo-faint)">No HW models reported yet</div>';
  } else {
    el.innerHTML = rows.join('');
  }
  el.style.display = 'block';
}

// HW color is ON by default — localStorage is only consulted to let a user turn it OFF.
(function() {
  try {
    var v = localStorage.getItem('loracle-hwcolor');
    App.colorByHwModel = (v === null) ? true : (v === '1');
  } catch(e) {
    App.colorByHwModel = true;
  }
})();

// Base node radius — keep in sync with the sizing logic in renderCanvas so
// the link shortener never overshoots the visible circle.
function nodeRadius(node) {
  if (!node) return 3;
  if (node.isSelf) return 8;
  if (node.isChannel) return 7;
  var freshness = 1.0;
  var nowSecs = Date.now() / 1000;
  if (node.lastHeard) freshness = Math.max(0.25, Math.min(1.0, 1.0 - (nowSecs - node.lastHeard) / 3600));
  else freshness = 0.25;
  return 3 + Math.round(freshness * 3);
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
  ctx.save();
  ctx.translate(App.panX, App.panY);

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

  // Subtle crosshair at center
  ctx.strokeStyle = divider; ctx.lineWidth = 0.3; ctx.setLineDash([4, 8]);
  ctx.beginPath(); ctx.moveTo(cx - 30, cy); ctx.lineTo(cx + 30, cy); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(cx, cy - 30); ctx.lineTo(cx, cy + 30); ctx.stroke();
  ctx.setLineDash([]);

  // Build a quick lookup of recent messages per node (last 8s for pulse animations)
  var recentMsgs = {};
  if (App.state && App.state.messages) {
    var ns = Date.now() / 1000;
    App.state.messages.forEach(function(m) {
      var age = ns - m.ts;
      if (age < 8) {
        if (!recentMsgs[m.node] || recentMsgs[m.node].age > age) {
          recentMsgs[m.node] = { age: age, dir: m.dir };
        }
      }
    });
  }

  // Build node->link map for quick path lookup (target -> link)
  var linkByTarget = {};
  App.links.forEach(function(l) { linkByTarget[l.target.id] = l; });

  // Draw links — shortened by node radius + 2px so lines don't bleed through nodes.
  App.links.forEach(function(link) {
    var s = link.source, t = link.target;
    if (!nodeInScope(s) || !nodeInScope(t)) return;
    var dx = t.x - s.x, dy = t.y - s.y;
    var len = Math.sqrt(dx*dx + dy*dy) || 1;
    var ux = dx / len, uy = dy / len;
    var sr = nodeRadius(s) + 2;
    var tr = nodeRadius(t) + 2;
    if (sr + tr >= len) return;  // nodes overlap — skip the link entirely
    var x1 = s.x + ux * sr, y1 = s.y + uy * sr;
    var x2 = t.x - ux * tr, y2 = t.y - uy * tr;
    ctx.beginPath();
    ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
    var linkActive = isTraffic && (activeNodes[t.id] || false);
    ctx.strokeStyle = linkActive ? accent : (t.isMC ? '#9b59b6' : accent2);
    ctx.lineWidth = linkActive ? 2 : 0.8;
    ctx.globalAlpha = isTraffic ? (linkActive ? 0.7 : 0.05) : 0.25;
    ctx.stroke();
    ctx.globalAlpha = 1;
  });

  // Signal pulses along links — ~2s to travel from end to end
  Object.keys(recentMsgs).forEach(function(nodeId) {
    var msg = recentMsgs[nodeId];
    // Walk from the triggered node back to self, animating along each link in the path
    var current = nodeId;
    var segments = [];
    var safety = 10;
    while (current && !isSelfId(current) && safety-- > 0) {
      var link = linkByTarget[current];
      if (!link) break;
      segments.push(link);
      current = link.source.id;
    }
    if (segments.length === 0) return;

    // Each segment takes ~0.8s, total path duration
    var perSeg = 0.8;
    var totalDur = segments.length * perSeg;
    if (msg.age > totalDur) return;

    // Which segment is the pulse currently on?
    var segIdx = Math.floor(msg.age / perSeg);
    if (segIdx >= segments.length) return;
    var segProgress = (msg.age % perSeg) / perSeg;

    // Direction: 'in' = incoming (from node toward self), 'out' = outgoing (from self toward node)
    var seg = segments[segIdx];
    var from, to;
    if (msg.dir === 'in') {
      // Start from triggered node, head toward self — walk segments in order
      from = seg.target; to = seg.source;
    } else {
      // Outgoing: start from self, head toward node — reverse segment order
      var outIdx = segments.length - 1 - segIdx;
      seg = segments[outIdx];
      from = seg.source; to = seg.target;
      segProgress = (msg.age % perSeg) / perSeg;
    }

    // Ease-out: pulse accelerates out, settles at destination (t * (2 - t))
    var eased = segProgress * (2 - segProgress);
    var px = from.x + (to.x - from.x) * eased;
    var py = from.y + (to.y - from.y) * eased;
    var pulseColor = msg.dir === 'in' ? accent2 : accent;
    var pulseAlpha = Math.max(0, 1 - msg.age / totalDur);

    ctx.globalAlpha = pulseAlpha;
    ctx.beginPath(); ctx.arc(px, py, 4, 0, Math.PI * 2);
    ctx.fillStyle = pulseColor; ctx.fill();
    // Glow trail
    ctx.globalAlpha = pulseAlpha * 0.4;
    ctx.beginPath(); ctx.arc(px, py, 8, 0, Math.PI * 2);
    ctx.fill();
    ctx.globalAlpha = 1;
  });

  // Draw nodes
  var nowSecs = Date.now() / 1000;
  App.nodes.forEach(function(node, i) {
    if (!nodeInScope(node)) return;
    ctx.globalAlpha = 1;

    // Freshness: 1.0 = just heard, 0.25 = 1hr+ stale
    var freshness = 1.0;
    if (!node.isSelf && !node.isChannel && node.lastHeard) {
      freshness = Math.max(0.25, Math.min(1.0, 1.0 - (nowSecs - node.lastHeard) / 3600));
    } else if (!node.isSelf && !node.isChannel) {
      freshness = 0.25;
    }

    var mcColor = '#9b59b6';
    var nodeAlpha = node.isSelf || node.isChannel ? 1 : (0.3 + 0.7 * freshness);
    if (isTraffic && !node.isSelf && !activeNodes[node.id]) nodeAlpha = 0.1;

    // Entrance fade-in (opacity only — safer than scale animation)
    var entranceAlpha = 1;
    var entranceProg = 1;  // 0..1, used for the ring flash below
    if (node.birthTime && !node.isSelf) {
      var ageMs = performance.now() - node.birthTime;
      var dur = freshness > 0.5 ? 1200 : 600;
      if (ageMs < dur) {
        entranceProg = ageMs / dur;
        entranceAlpha = entranceProg;
      }
    }
    nodeAlpha *= entranceAlpha;

    // Breathing — fresh nodes only. Radius ±1.8px, alpha ±0.08
    var breathPx = 0;
    var breathAlpha = 0;
    if (!node.isSelf && !node.isChannel && freshness > 0.5) {
      var phase = App.breathPhase + i * 0.6;
      breathPx = Math.sin(phase) * 1.8 * freshness;
      breathAlpha = Math.sin(phase) * 0.08 * freshness;
    }
    var baseR = node.isSelf ? 8 : (node.isChannel ? 7 : 3 + Math.round(freshness * 3));
    var r = Math.max(2, Math.min(12, baseR + breathPx));

    ctx.globalAlpha = Math.max(0, Math.min(1, nodeAlpha + breathAlpha));

    // Diamond-shape MeshCore nodes need ~15% larger half-diagonal to look the
    // same visual weight as a circle of radius r.
    var shapeR = node.isMC ? r * 1.15 : r;

    if (node.isChannel) {
      ctx.beginPath(); ctx.arc(node.x, node.y, r, 0, Math.PI * 2);
      ctx.strokeStyle = accent2; ctx.lineWidth = 2; ctx.stroke();
    } else if (node.isSelf) {
      // Sonar ring — smooth radial dissolve (no pop)
      var sonarPhase = (App.breathPhase * 2) % 4;  // 0..4
      var sonarT = sonarPhase / 4;  // 0..1
      var sonarR = 10 + sonarT * 22;  // 10..32
      var sonarAlpha = (1 - sonarT) * (1 - sonarT);  // ease-out quadratic
      if (sonarAlpha > 0.01) {
        ctx.globalAlpha = sonarAlpha * 0.6;
        ctx.beginPath(); ctx.arc(node.x, node.y, sonarR, 0, Math.PI * 2);
        ctx.strokeStyle = node.isMC ? mcColor : accent; ctx.lineWidth = 1; ctx.stroke();
      }
      ctx.globalAlpha = 1;
      // Self fills with the protocol's accent so a dual-radio rig has two
      // clearly-distinct "my radio" dots — teal circle for MT, purple diamond for MC.
      var selfFill = node.isMC ? mcColor : accent;
      if (node.isMC) { drawDiamond(ctx, node.x, node.y, shapeR); }
      else { ctx.beginPath(); ctx.arc(node.x, node.y, r, 0, Math.PI * 2); }
      ctx.fillStyle = selfFill; ctx.fill();
    } else if (node.isMC) {
      drawDiamond(ctx, node.x, node.y, shapeR);
      ctx.fillStyle = nodeFillColor(node, accent2, mcColor);
      ctx.fill();
    } else {
      ctx.beginPath(); ctx.arc(node.x, node.y, r, 0, Math.PI * 2);
      ctx.fillStyle = nodeFillColor(node, accent2, mcColor);
      ctx.fill();
    }

    // Entrance ring flash — expanding ring announces a newly detected node.
    if (entranceProg < 1 && !node.isSelf) {
      var ringR = baseR + entranceProg * 18;
      var ringAlpha = (1 - entranceProg) * 0.8;
      ctx.globalAlpha = ringAlpha;
      ctx.beginPath(); ctx.arc(node.x, node.y, ringR, 0, Math.PI * 2);
      ctx.strokeStyle = accent; ctx.lineWidth = 1.2; ctx.stroke();
      ctx.globalAlpha = nodeAlpha;  // restore for anything drawn after
    }

    // Label
    ctx.font = '500 9px "IBM Plex Mono"';
    ctx.textAlign = 'center';
    ctx.fillStyle = node.isSelf ? accent : (node.isChannel ? accent2 : (node.isMC ? mcColor : dim));
    ctx.fillText(node.isChannel ? '\u25C9 ' + node.label : node.label, node.x, node.y + r + 12);

    // Hop label
    if (!node.isSelf && node.hops !== null) {
      ctx.font = '400 8px "IBM Plex Mono"';
      ctx.fillStyle = faint;
      ctx.fillText(node.hops === 0 ? 'direct' : node.hops + 'h', node.x, node.y + r + 21);
    }

    // Selected highlight
    if (App.selectedNode && App.selectedNode === node.id) {
      ctx.beginPath(); ctx.arc(node.x, node.y, baseR + 10, 0, Math.PI * 2);
      ctx.strokeStyle = accent; ctx.lineWidth = 1.5; ctx.setLineDash([3, 3]);
      ctx.stroke(); ctx.setLineDash([]);
    }
    // Low battery indicator
    if (!node.isSelf && App.state && App.state.device_metrics) {
      var ndm = App.state.device_metrics[node.id];
      if (ndm && ndm.battery !== undefined && ndm.battery <= 20) {
        ctx.globalAlpha = 1;
        ctx.beginPath(); ctx.arc(node.x - r - 2, node.y - r - 2, 3, 0, Math.PI * 2);
        ctx.fillStyle = '#c0392b'; ctx.fill();
      }
    }

    // Unread badge
    if (!node.isSelf && App.unreadCounts[node.id] > 0) {
      ctx.globalAlpha = 1;
      var uc = App.unreadCounts[node.id];
      var bx = node.x + r + 2, by = node.y - r - 2;
      ctx.beginPath(); ctx.arc(bx, by, 6, 0, Math.PI * 2);
      ctx.fillStyle = accent; ctx.fill();
      ctx.font = '500 7px "IBM Plex Mono"';
      ctx.textAlign = 'center'; ctx.fillStyle = bg;
      ctx.fillText(uc > 9 ? '9+' : String(uc), bx, by + 2.5);
    }

    // Favorite star
    if (node.isFavorite && !node.isSelf) {
      ctx.globalAlpha = 1;
      ctx.font = '500 11px "IBM Plex Mono"';
      ctx.textAlign = 'center';
      ctx.fillStyle = '#f1c40f';
      ctx.fillText('\u2605', node.x - r - 4, node.y - r + 1);
    }

    ctx.globalAlpha = 1; // reset after each node
  });
  ctx.restore();
}

// ─── Canvas Interaction + Floating Windows ────────────────────────────────

var _mouseX = 0, _mouseY = 0, _panStart = null, _isPanning = false;

function onCanvasClick(e) {
  if (_isPanning) { _isPanning = false; return; }
  var rect = App.canvas.getBoundingClientRect();
  var mx = e.clientX - rect.left - App.panX, my = e.clientY - rect.top - App.panY;
  var closest = null, closestDist = Infinity;
  App.nodes.forEach(function(n) {
    if (!nodeInScope(n)) return;
    var dx = n.x - mx, dy = n.y - my;
    var dist = Math.sqrt(dx*dx + dy*dy);
    // Click radius scales with node size so larger nodes are easier to hit.
    if (dist < 60 && dist < closestDist) { closest = n; closestDist = dist; }
  });
  if (closest) {
    openFloatWindow(closest);
  }
}

// Hover-cursor hit test, called from the main mousemove handler.
function _updateHoverCursor(mxRel, myRel) {
  var mx = mxRel - App.panX, my = myRel - App.panY;
  var hit = false;
  for (var i = 0; i < App.nodes.length; i++) {
    var n = App.nodes[i];
    if (!nodeInScope(n)) continue;
    var dx = n.x - mx, dy = n.y - my;
    if (Math.sqrt(dx*dx + dy*dy) < 60) { hit = true; break; }
  }
  if (App.canvas) App.canvas.style.cursor = hit ? 'pointer' : '';
}

// ─── Floating Node Windows ────────────────────────────────────────────────

var _openWindows = {};

async function openFloatWindow(node) {
  if (!node) return;
  if (node.isSelf) return openSelfWindow(node);
  App.selectedNode = node.id;  // drives the on-canvas selection ring
  if (_openWindows[node.id]) {
    // Already open — refresh data and bring this window to the front.
    loadFloatData(node.id);
    _bringWinToFront(_openWindows[node.id]);
    return;
  }
  var win = document.createElement('div');
  win.className = 'lo-float-win';
  win.dataset.nodeId = node.id;
  // Panels CASCADE when multiple are open so both (e.g. an MT thread and
  // an MC thread) stay visible — previously each open call closed the
  // other panels, which made switching between protocols feel like a
  // mutex. Users close with the × button or drag panels wherever they want.
  var winW = 420;
  var baseX = Math.min(Math.max(App.width - winW - 20, 10), App.width - winW - 10);
  var baseY = 60;
  var existing = Object.keys(_openWindows).length;
  var wx = baseX - existing * 28;
  var wy = baseY + existing * 28;
  if (wx < 10) { wx = 10 + ((existing * 28) % 80); }  // wrap if off-screen left
  win.style.left = wx + 'px'; win.style.top = wy + 'px';
  var label = node.label || node.id.slice(-6);
  var eid = node.id.replace(/[^a-zA-Z0-9]/g, '_');
  win.innerHTML =
    '<div class="lo-fw-header" onmousedown="startDragWin(event,this.parentElement)">' +
      '<span class="lo-fw-name" id="fw-title-' + eid + '" ondblclick="floatStartRename(\'' + escapeHtml(node.id) + '\')" title="Double-click to rename">' + escapeHtml(label) + '</span>' +
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
  _bringWinToFront(win);
  // Click anywhere in the window body raises it — useful when two panels
  // overlap and the user wants to interact with the one underneath.
  win.addEventListener('mousedown', function() { _bringWinToFront(win); });
  await loadFloatData(node.id);
}

// Self-node panel: no thread history, no composer — just live backend telemetry
// (battery / voltage / hw / uptime / node counts) pulled straight from App.state.
function openSelfWindow(node) {
  if (!node) return;
  var key = node.id;
  App.selectedNode = key;
  if (_openWindows[key]) {
    loadSelfData(key);
    _bringWinToFront(_openWindows[key]);
    return;
  }
  var win = document.createElement('div');
  win.className = 'lo-float-win';
  win.dataset.nodeId = key;
  var winW = 420;
  var existing = Object.keys(_openWindows).length;
  var wx = Math.min(Math.max(App.width - winW - 20, 10), App.width - winW - 10) - existing * 28;
  var wy = 60 + existing * 28;
  if (wx < 10) { wx = 10 + ((existing * 28) % 80); }
  win.style.left = wx + 'px'; win.style.top = wy + 'px';
  var eid = key.replace(/[^a-zA-Z0-9]/g, '_');
  var label = node.label || 'MY RADIO';
  win.innerHTML =
    '<div class="lo-fw-header" onmousedown="startDragWin(event,this.parentElement)">' +
      '<span class="lo-fw-name">' + escapeHtml(label) + '</span>' +
      '<button class="lo-fw-close" onclick="closeFloatWin(\'' + escapeHtml(key) + '\')">\u00d7</button>' +
    '</div>' +
    '<div class="lo-fw-meta" id="fw-m-' + eid + '">Loading...</div>';
  document.getElementById('float-windows').appendChild(win);
  _openWindows[key] = win;
  _bringWinToFront(win);
  win.addEventListener('mousedown', function() { _bringWinToFront(win); });
  loadSelfData(key);
}

function loadSelfData(key) {
  var eid = key.replace(/[^a-zA-Z0-9]/g, '_');
  var metaEl = document.getElementById('fw-m-' + eid);
  if (!metaEl) return;
  var s = App.state || {};
  var backends = s.backends || [];
  var dm = s.device_metrics || {};
  var selfNode = (App.nodes || []).find(function(n) { return n.id === key; });
  var backendId = selfNode && selfNode.selfBackendId;
  // Pick the backend matching this self-node; fall back to the first backend.
  var b = backends.find(function(x) { return x.id === backendId; }) || backends[0] || {};
  var protoLc = String(b.protocol || '').toLowerCase();
  var isMC = (protoLc === 'mc' || protoLc === 'meshcore');
  var protoLabel = isMC ? 'MESHCORE' : 'MESHTASTIC';
  var protoColor = isMC ? '#9b59b6' : 'var(--lo-accent-2)';
  // Shape rendered via CSS (rotated square for MC, circle for MT) + flex-aligned
  // with the label so the icon vertically centers with the uppercase letters.
  var shapeStyle = 'display:inline-block;width:9px;height:9px;background:' + protoColor +
    ';flex-shrink:0;' + (isMC ? 'transform:rotate(45deg);' : 'border-radius:50%;');
  var lines = [];
  lines.push('<div style="color:' + protoColor + ';font-weight:500;font-size:12px;margin-bottom:6px;' +
             'display:flex;align-items:center;gap:8px">' +
             '<span style="' + shapeStyle + '"></span>' + protoLabel + '</div>');
  lines.push('<div>STATUS: ' + (b.connected ? '<span style="color:var(--lo-accent-2)">CONNECTED</span>' : '<span style="color:#c0392b">DISCONNECTED</span>') + '</div>');
  if (b.transport) lines.push('<div>TRANSPORT: ' + escapeHtml(String(b.transport).toUpperCase()) + '</div>');
  var selfNodeId = b.self_node_id || (selfNode && selfNode.selfNodeId) || null;
  if (selfNodeId) lines.push('<div>NODE ID: ' + escapeHtml(selfNodeId) + '</div>');
  // Device metrics — keyed by the real unified node id, not our synthetic '__self_*__'
  var myDm = selfNodeId ? dm[selfNodeId] : null;
  if (myDm) {
    var parts = [];
    if (myDm.battery !== undefined) parts.push(myDm.battery + '%');
    if (myDm.voltage !== undefined) parts.push(myDm.voltage + 'V');
    if (parts.length) {
      var warn = (myDm.battery !== undefined && myDm.battery <= 20) ? ' <span style="color:#c0392b">\u26a0 LOW BATTERY</span>' : '';
      lines.push('<div>BATTERY: ' + parts.join(' / ') + warn + '</div>');
    }
    if (myDm.temperature !== undefined) lines.push('<div>TEMP: ' + myDm.temperature + '\u00b0C</div>');
    if (myDm.humidity !== undefined) lines.push('<div>HUMIDITY: ' + myDm.humidity + '%</div>');
    if (myDm.ch_util !== undefined) lines.push('<div>CH UTIL: ' + myDm.ch_util + '%</div>');
    if (myDm.hw_model) lines.push('<div>HW: ' + escapeHtml(myDm.hw_model) + '</div>');
  } else if (selfNodeId) {
    lines.push('<div style="color:var(--lo-dim)">BATTERY: (no telemetry yet)</div>');
  }
  lines.push('<div style="border-top:1px solid var(--lo-divider);margin-top:8px;padding-top:8px">UPTIME: ' + formatUptime(s.uptime || 0) + '</div>');
  lines.push('<div>NODES SEEN: ' + (s.node_count || 0) + '</div>');
  lines.push('<div>MESSAGES: ' + (s.message_count || 0) + '</div>');
  if (s.model) lines.push('<div>LLM: ' + escapeHtml(String(s.model)) + '</div>');
  metaEl.innerHTML = lines.join('');
}

async function loadFloatData(nodeId) {
  if (isSelfId(nodeId)) { loadSelfData(nodeId); return; }
  var eid = nodeId.replace(/[^a-zA-Z0-9]/g, '_');
  var metaEl = document.getElementById('fw-m-' + eid);
  var actEl = document.getElementById('fw-a-' + eid);
  var msgsEl = document.getElementById('fw-g-' + eid);
  if (!metaEl) return;
  var canvasNode = App.nodes.find(function(n) { return n.id === nodeId; });
  var d = await callApi('GET', '/api/threads/' + encodeURIComponent(nodeId));
  var contact = (d && d.contact) || {};
  var msgs = (d && d.messages) || [];
  // Prefer contact.protocol, but fall back to the unified-id prefix so nodes
  // without a contact record yet still show the right protocol.
  var protoSrc = contact.protocol || (nodeId.indexOf('mc:') === 0 ? 'mc' : 'mt');
  var proto = (protoSrc === 'mc' || protoSrc === 'meshcore') ? 'MC' : 'MT';
  var lines = ['<div>ID: ' + escapeHtml(nodeId) + '</div>', '<div>PROTOCOL: ' + proto + '</div>'];
  var hops = contact.last_hops;
  if (hops !== null && hops !== undefined) lines.push('<div>HOPS: ' + (hops === 0 ? 'DIRECT' : hops) + '</div>');
  if (contact.last_rssi) lines.push('<div>RSSI: ' + contact.last_rssi + ' dBm</div>');
  if (contact.last_snr) lines.push('<div>SNR: ' + contact.last_snr + ' dB</div>');
  if (contact.last_heard) lines.push('<div>HEARD: ' + relativeTime(contact.last_heard) + '</div>');
  if (canvasNode && canvasNode.lat) lines.push('<div>GPS: ' + canvasNode.lat.toFixed(4) + ', ' + canvasNode.lon.toFixed(4) + '</div>');
  // Device metrics (battery, voltage, temperature, etc.)
  var dm = (App.state && App.state.device_metrics) ? App.state.device_metrics[nodeId] : null;
  if (dm) {
    var parts = [];
    if (dm.battery !== undefined) parts.push(dm.battery + '%');
    if (dm.voltage !== undefined) parts.push(dm.voltage + 'V');
    if (parts.length) {
      var warn = (dm.battery !== undefined && dm.battery <= 20) ? ' <span style="color:#c0392b">\u26a0 LOW BATTERY</span>' : '';
      lines.push('<div>BATTERY: ' + parts.join(' / ') + warn + '</div>');
    }
    if (dm.temperature !== undefined) lines.push('<div>TEMP: ' + dm.temperature + '\u00b0C</div>');
    if (dm.humidity !== undefined) lines.push('<div>HUMIDITY: ' + dm.humidity + '%</div>');
    if (dm.ch_util !== undefined) lines.push('<div>CH UTIL: ' + dm.ch_util + '%</div>');
    if (dm.hw_model) lines.push('<div>HW: ' + escapeHtml(dm.hw_model) + '</div>');
  }
  metaEl.innerHTML = lines.join('');
  var aiVal = contact.ai_enabled;
  var aiLabel = aiVal === 1 ? 'AI: ON' : aiVal === 0 ? 'AI: OFF' : 'AI: AUTO';
  var aiClass = aiVal === 1 ? ' on' : '';
  var isChannel = nodeId.indexOf('channel:') !== -1;
  var favOn = !!contact.is_favorite;
  var favLabel = favOn ? '\u2605 FAV' : '\u2606 FAV';
  var favClass = favOn ? ' on' : '';
  actEl.innerHTML = '<button class="' + aiClass + '" onclick="floatToggleAi(\'' + escapeHtml(nodeId) + '\')">' + aiLabel + '</button>' +
    (isChannel ? '' : ' <button class="' + favClass + '" onclick="floatToggleFavorite(\'' + escapeHtml(nodeId) + '\')">' + favLabel + '</button>') +
    (isChannel ? '' : ' <button onclick="floatStartRename(\'' + escapeHtml(nodeId) + '\')">RENAME</button>') +
    (isChannel ? '' : ' <button onclick="floatTrace(\'' + escapeHtml(nodeId) + '\')">TRACE</button>');
  // Use custom_name in the header if set
  var titleEl = document.getElementById('fw-title-' + eid);
  if (titleEl && contact.custom_name) titleEl.textContent = contact.custom_name;
  if (msgs.length === 0) { msgsEl.innerHTML = '<div class="lo-fw-empty">NO MESSAGES YET</div>'; }
  else {
    // Show full history (API already limits to 50 most recent on the server).
    msgsEl.innerHTML = msgs.map(function(m) {
      var ac = m.direction === 'in' ? 'in' : (m.author === 'ai' ? 'ai' : 'out');
      var arrow = m.direction === 'in' ? '\u2190' : '\u2192';
      return '<div class="lo-fw-msg"><span class="lo-fw-msg-time">' + formatTime(m.timestamp) +
        '</span><span class="lo-fw-msg-arrow ' + ac + '">' + arrow +
        '</span><span class="lo-fw-msg-body">' + escapeHtml(m.text) +
        renderDeliveryStatus(m) + '</span></div>';
    }).join('');
    msgsEl.scrollTop = msgsEl.scrollHeight;
  }
  try { callApi('POST', '/api/threads/' + encodeURIComponent(nodeId) + '/open'); } catch(e) {}
}

function closeFloatWin(nodeId) {
  var win = _openWindows[nodeId];
  if (win && win.parentNode) win.parentNode.removeChild(win);
  delete _openWindows[nodeId];
  if (App.selectedNode === nodeId) App.selectedNode = null;
}

// Called from the poll loop's soft-refresh path. If the window is open,
// re-fetch its data; otherwise no-op (selection persists but nothing to refresh).
function openNodePanel(nodeId) {
  if (_openWindows[nodeId]) loadFloatData(nodeId);
}

async function floatSend(nodeId, inputEl) {
  var text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = '';
  // Optimistic render so the message appears instantly with a "sending" pill.
  var eid = nodeId.replace(/[^a-zA-Z0-9]/g, '_');
  var msgsEl = document.getElementById('fw-g-' + eid);
  var optimisticId = 'opt-' + Date.now();
  if (msgsEl) {
    var empty = msgsEl.querySelector('.lo-fw-empty');
    if (empty) empty.remove();
    var nowS = Date.now() / 1000;
    var row = document.createElement('div');
    row.className = 'lo-fw-msg';
    row.id = optimisticId;
    row.innerHTML = '<span class="lo-fw-msg-time">' + formatTime(nowS) +
      '</span><span class="lo-fw-msg-arrow out">\u2192</span>' +
      '<span class="lo-fw-msg-body">' + escapeHtml(text) +
      renderDeliveryStatus({direction: 'out', delivery_status: 'sending'}) + '</span>';
    msgsEl.appendChild(row);
    msgsEl.scrollTop = msgsEl.scrollHeight;
  }
  var d = await callApi('POST', '/api/threads/' + encodeURIComponent(nodeId) + '/send', {text: text});
  if (d && d.ok) {
    var isChannel = nodeId.indexOf('channel:') !== -1;
    showToast(isChannel ? 'Broadcast on CH ' + (nodeId.split(':').pop() || '0') : 'Sent to ' + nodeId.slice(-6));
  } else {
    // callApi already showed an error toast — restore the text, mark the optimistic row as failed.
    inputEl.value = text;
    var failedRow = document.getElementById(optimisticId);
    if (failedRow) {
      var body = failedRow.querySelector('.lo-fw-msg-body');
      if (body) body.innerHTML = escapeHtml(text) + renderDeliveryStatus({direction: 'out', delivery_status: 'failed'});
    }
  }
  loadFloatData(nodeId);  // reconciles with server truth (replaces optimistic row)
}

async function floatTrace(nodeId) {
  var eid = nodeId.replace(/[^a-zA-Z0-9]/g, '_');
  var metaEl = document.getElementById('fw-m-' + eid);
  var d = await callApi('POST', '/api/traceroute', {dest: nodeId});
  if (!d || !d.ok) return;
  showToast('Traceroute sent — waiting for response...');
  if (metaEl) metaEl.innerHTML += '<div id="fw-trace-' + eid + '" style="color:var(--lo-accent);margin-top:4px">TRACE: waiting for response...</div>';
  // Poll for result (up to 30s)
  var attempts = 0;
  var poll = setInterval(async function() {
    attempts++;
    try {
      var r = await fetch('/api/traceroute/result/' + encodeURIComponent(nodeId));
      var rd = await r.json();
      if (rd && rd.result) {
        clearInterval(poll);
        var route = rd.result.route || [];
        var el = document.getElementById('fw-trace-' + eid);
        if (el) el.innerHTML = 'TRACE: ' + (route.length === 0 ? 'DIRECT (no hops)' : 'YOU \u2192 ' + route.map(function(h) { return h.slice(-4); }).join(' \u2192 ') + ' \u2192 ' + nodeId.slice(-4));
        showToast('Traceroute complete: ' + (route.length === 0 ? 'direct' : route.length + ' hops'));
      }
    } catch(e) {}
    if (attempts >= 15) { clearInterval(poll); var el = document.getElementById('fw-trace-' + eid); if (el) el.textContent = 'TRACE: no response (timed out)'; }
  }, 2000);
}

async function floatToggleAi(nodeId) {
  await callApi('POST', '/api/threads/' + encodeURIComponent(nodeId) + '/ai-toggle');
  loadFloatData(nodeId);
}

async function floatToggleFavorite(nodeId) {
  var d = await callApi('POST', '/api/threads/' + encodeURIComponent(nodeId) + '/favorite');
  if (d && d.ok) {
    showToast(d.is_favorite ? '\u2605 Favorited' : 'Unfavorited');
    // Optimistically update the canvas node so the star appears instantly
    var n = App.nodes.find(function(x) { return x.id === nodeId; });
    if (n) n.isFavorite = d.is_favorite;
  }
  loadFloatData(nodeId);
}

function floatStartRename(nodeId) {
  var eid = nodeId.replace(/[^a-zA-Z0-9]/g, '_');
  var titleEl = document.getElementById('fw-title-' + eid);
  if (!titleEl) return;
  if (titleEl.querySelector('input')) return;  // already editing
  var current = titleEl.textContent;
  var input = document.createElement('input');
  input.type = 'text';
  input.value = current;
  input.maxLength = 64;
  input.style.cssText = 'background:transparent;border:1px solid var(--lo-accent);color:var(--lo-ink);font:inherit;font-size:12px;padding:0 4px;width:140px;outline:none';
  input.onkeydown = function(e) {
    if (e.key === 'Enter') { e.preventDefault(); floatCommitRename(nodeId, input.value, current); }
    else if (e.key === 'Escape') { e.preventDefault(); titleEl.textContent = current; }
  };
  input.onblur = function() { floatCommitRename(nodeId, input.value, current); };
  titleEl.innerHTML = '';
  titleEl.appendChild(input);
  input.focus(); input.select();
}

async function floatCommitRename(nodeId, newName, oldName) {
  var eid = nodeId.replace(/[^a-zA-Z0-9]/g, '_');
  var titleEl = document.getElementById('fw-title-' + eid);
  var trimmed = (newName || '').trim();
  // Send null to clear, otherwise the new name (empty → clears)
  var payload = { name: trimmed ? trimmed : null };
  var d = await callApi('POST', '/api/threads/' + encodeURIComponent(nodeId) + '/rename', payload);
  if (d && d.ok) {
    var finalLabel = d.custom_name || oldName;  // oldName may include short_name fallback
    if (titleEl) titleEl.textContent = finalLabel;
    // Update the canvas node label
    var n = App.nodes.find(function(x) { return x.id === nodeId; });
    if (n) n.label = d.custom_name || n.label;
    showToast(d.custom_name ? 'Renamed to ' + d.custom_name : 'Name cleared');
  } else if (titleEl) {
    titleEl.textContent = oldName;
  }
}

var _dragWin = null, _dragOff = {x:0, y:0}, _winZ = 100;

// Monotonic z-index bump for float windows so a clicked / dragged panel
// always surfaces above its siblings. Keeps overlapping MT + MC threads
// usable — click either one and it comes to the front.
function _bringWinToFront(win) {
  if (!win) return;
  _winZ += 1;
  win.style.zIndex = _winZ;
}

function startDragWin(e, win) {
  _dragWin = win; _dragOff.x = e.clientX - win.offsetLeft; _dragOff.y = e.clientY - win.offsetTop;
  _bringWinToFront(win);
  e.preventDefault();
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

// ─── AI Chat Tab ───────────────────────────────────────────────────────────

var _aiSending = false;

function aiActivate() {
  // First-time activation: focus the input and sync the model label.
  var modelEl = document.getElementById('ai-model-label');
  if (modelEl && App.state && App.state.model) modelEl.textContent = App.state.model;
  var input = document.getElementById('ai-input');
  if (input) setTimeout(function() { input.focus(); }, 50);
  // Pull any existing history once per session load.
  if (!App._aiHistoryLoaded) {
    App._aiHistoryLoaded = true;
    aiLoadHistory();
  }
}

async function aiLoadHistory() {
  try {
    var r = await fetch('/api/ai_chat/history');
    var d = await r.json();
    var msgs = (d && d.messages) || [];
    if (msgs.length === 0) return;
    var box = document.getElementById('ai-messages');
    var empty = box.querySelector('.lo-ai-empty');
    if (empty) empty.remove();
    msgs.forEach(function(m) {
      _aiAppend(m.role === 'user' ? 'user' : 'ai', m.content);
    });
    box.scrollTop = box.scrollHeight;
  } catch(e) {}
}

function _aiAppend(role, text, opts) {
  opts = opts || {};
  var box = document.getElementById('ai-messages');
  if (!box) return null;
  var empty = box.querySelector('.lo-ai-empty');
  if (empty) empty.remove();
  var div = document.createElement('div');
  div.className = 'lo-ai-msg ' + role + (opts.thinking ? ' thinking' : '') + (opts.error ? ' error' : '');
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div;
}

async function aiSend() {
  if (_aiSending) return;
  var input = document.getElementById('ai-input');
  var btn = document.getElementById('ai-send-btn');
  var text = (input.value || '').trim();
  if (!text) return;
  _aiSending = true;
  btn.disabled = true;
  input.value = '';
  _aiAppend('user', text);
  var thinking = _aiAppend('ai', 'Thinking...', {thinking: true});
  try {
    var r = await fetch('/api/ai_chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text}),
    });
    var d = await r.json();
    if (thinking) thinking.remove();
    if (r.ok && d && d.response) {
      _aiAppend('ai', d.response);
    } else {
      _aiAppend('ai', (d && d.error) || 'AI request failed', {error: true});
    }
  } catch(e) {
    if (thinking) thinking.remove();
    _aiAppend('ai', 'Network error contacting Ollama', {error: true});
  } finally {
    _aiSending = false;
    btn.disabled = false;
    input.focus();
  }
}

async function aiClearHistory() {
  if (!confirm('Clear the local AI chat history?')) return;
  try {
    await fetch('/api/ai_chat/clear', {method: 'POST'});
    var box = document.getElementById('ai-messages');
    box.innerHTML = '<div class="lo-ai-empty"><div>Conversation cleared.</div><div style="margin-top:4px;color:var(--lo-faint);font-size:10px">Start a new chat below.</div></div>';
    showToast('AI history cleared');
  } catch(e) { showToast('Clear failed', 'error'); }
}

// ─── BRIDGE Tab (LORACLE v2) ────────────────────────────────────────────────

var _bridgeState = { cfg: null, dirty: false, lastEventTs: 0, pollTimer: null };

function bridgeActivate() {
  if (!_bridgeState.cfg) bridgeReloadConfig();
  bridgePollStats();
  bridgePollEvents();
  if (!_bridgeState.pollTimer) {
    _bridgeState.pollTimer = setInterval(function() {
      if (App.view !== 'bridge') { clearInterval(_bridgeState.pollTimer); _bridgeState.pollTimer = null; return; }
      bridgePollStats();
      bridgePollEvents();
    }, 2500);
  }
}

async function bridgeReloadConfig() {
  try {
    var r = await fetch('/api/bridge/config');
    var d = await r.json();
    _bridgeState.cfg = { enabled: !!d.enabled, rules: Array.isArray(d.rules) ? d.rules.slice() : [] };
    _bridgeState.dirty = false;
    document.getElementById('bridge-enabled').checked = _bridgeState.cfg.enabled;
    bridgeUpdateBadge();
    bridgeRenderRules();
    bridgeSyncSimpleToggle();
    bridgeSetStatus('');
  } catch(e) { bridgeSetStatus('load failed: ' + e, 'error'); }
}

// Sync the "Auto-bridge public channel" checkbox with the underlying config.
// The toggle is "on" iff (enabled && both default rules present); anything
// else is "off" — including partial states from the advanced editor.
function bridgeSyncSimpleToggle() {
  var cb = document.getElementById('bridge-simple-toggle');
  if (!cb || !_bridgeState.cfg) return;
  var rules = _bridgeState.cfg.rules || [];
  var hasMT = rules.some(function(r) { return r.source === 'meshtastic' && r.channel === 0 && r.mode === 'always'; });
  var hasMC = rules.some(function(r) { return r.source === 'meshcore' && r.channel === 0 && r.mode === 'always'; });
  cb.checked = !!(_bridgeState.cfg.enabled && hasMT && hasMC);
  // Surface the status in plain English under the checkbox so the user
  // can sanity-check at a glance without expanding the advanced panel.
  var st = document.getElementById('bridge-simple-status');
  if (st) {
    if (cb.checked) {
      st.textContent = 'Public channel is bridging both ways.';
      st.style.color = 'var(--lo-accent-2)';
    } else if (rules.length) {
      st.textContent = 'Off — custom rules are configured in ADVANCED.';
      st.style.color = 'var(--lo-faint)';
    } else {
      st.textContent = 'Off.';
      st.style.color = 'var(--lo-faint)';
    }
  }
}

async function bridgeSimpleToggle(enabled) {
  var st = document.getElementById('bridge-simple-status');
  if (st) { st.textContent = 'Saving\u2026'; st.style.color = 'var(--lo-faint)'; }
  try {
    var r = await fetch('/api/bridge/public-channel', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: !!enabled})
    });
    var d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || ('HTTP ' + r.status));
    // Reload the underlying state so the advanced editor + badge catch up.
    await bridgeReloadConfig();
    showToast(enabled ? 'Public-channel bridge ON' : 'Public-channel bridge OFF', 'info');
  } catch (e) {
    if (st) { st.textContent = 'Could not save: ' + e; st.style.color = '#c0392b'; }
    // Flip the checkbox back to reflect server truth.
    await bridgeReloadConfig();
  }
}

function bridgeUpdateBadge() {
  var badge = document.getElementById('bridge-enabled-badge');
  var on = _bridgeState.cfg && _bridgeState.cfg.enabled;
  badge.textContent = on ? 'ON' : 'OFF';
  badge.classList.toggle('on', !!on);
}

function bridgeMarkDirty() {
  _bridgeState.dirty = true;
  bridgeSetStatus('unsaved changes — press APPLY');
  // Reflect toggle immediately in the in-memory cfg so add-rule works
  if (_bridgeState.cfg) _bridgeState.cfg.enabled = document.getElementById('bridge-enabled').checked;
  bridgeUpdateBadge();
}

function bridgeRenderRules() {
  var list = document.getElementById('bridge-rules-list');
  if (!_bridgeState.cfg || !_bridgeState.cfg.rules.length) {
    list.innerHTML = '<div class="lo-bridge-hint">No rules yet — click + ADD RULE to start relaying channel traffic across networks.</div>';
    return;
  }
  list.innerHTML = _bridgeState.cfg.rules.map(function(r, i) {
    var src = r.source === 'meshcore' ? 'meshcore' : 'meshtastic';
    var chan = (r.channel === null || r.channel === undefined) ? '' : String(r.channel);
    var mode = r.mode === 'always' || r.mode === 'ai-gated' ? r.mode : 'off';
    return (
      '<div class="lo-bridge-rule" data-idx="' + i + '">' +
        '<select onchange="bridgeUpdateRule(' + i + ', \'source\', this.value)">' +
          '<option value="meshtastic"' + (src === 'meshtastic' ? ' selected' : '') + '>meshtastic</option>' +
          '<option value="meshcore"' + (src === 'meshcore' ? ' selected' : '') + '>meshcore</option>' +
        '</select>' +
        '<span style="color:var(--lo-faint)">ch</span>' +
        '<input type="number" min="0" max="7" style="width:48px" value="' + escapeHtml(chan) + '" ' +
          'placeholder="any" oninput="bridgeUpdateRule(' + i + ', \'channel\', this.value)">' +
        '<select onchange="bridgeUpdateRule(' + i + ', \'mode\', this.value)">' +
          '<option value="off"' + (mode === 'off' ? ' selected' : '') + '>off</option>' +
          '<option value="always"' + (mode === 'always' ? ' selected' : '') + '>always</option>' +
          '<option value="ai-gated"' + (mode === 'ai-gated' ? ' selected' : '') + '>ai-gated</option>' +
        '</select>' +
        '<button class="lo-bridge-rule-del" onclick="bridgeDeleteRule(' + i + ')">\u00d7</button>' +
      '</div>'
    );
  }).join('');
}

function bridgeAddRule() {
  if (!_bridgeState.cfg) _bridgeState.cfg = { enabled: false, rules: [] };
  _bridgeState.cfg.rules.push({ source: 'meshtastic', channel: 0, mode: 'always' });
  bridgeMarkDirty();
  bridgeRenderRules();
}

function bridgeDeleteRule(idx) {
  _bridgeState.cfg.rules.splice(idx, 1);
  bridgeMarkDirty();
  bridgeRenderRules();
}

function bridgeUpdateRule(idx, field, value) {
  var rule = _bridgeState.cfg.rules[idx];
  if (!rule) return;
  if (field === 'channel') {
    if (value === '' || value === null) rule.channel = null;
    else {
      var n = parseInt(value, 10);
      rule.channel = isNaN(n) ? null : n;
    }
  } else {
    rule[field] = value;
  }
  bridgeMarkDirty();
}

async function bridgeSaveConfig() {
  if (!_bridgeState.cfg) return;
  bridgeSetStatus('saving...');
  try {
    var r = await fetch('/api/bridge/config', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(_bridgeState.cfg),
    });
    var d = await r.json();
    if (d.error) throw new Error(d.error);
    _bridgeState.cfg = d.config || _bridgeState.cfg;
    _bridgeState.dirty = false;
    bridgeSetStatus('applied \u2713');
    bridgeUpdateBadge();
    bridgeRenderRules();
  } catch(e) { bridgeSetStatus('save failed: ' + e, 'error'); }
}

function bridgeSetStatus(msg, level) {
  var el = document.getElementById('bridge-save-status');
  if (!el) return;
  el.textContent = msg;
  el.style.color = (level === 'error') ? '#e74c3c' : 'var(--lo-faint)';
}

async function bridgePollStats() {
  try {
    var r = await fetch('/api/bridge/stats');
    var d = await r.json();
    document.getElementById('bridge-relayed').textContent = d.relayed || 0;
    document.getElementById('bridge-dropped').textContent = d.dropped || 0;
    document.getElementById('bridge-dedup').textContent = d.dedup_size || 0;
  } catch(e) {}
}

async function bridgePollEvents() {
  try {
    var r = await fetch('/api/bridge/events?since=' + encodeURIComponent(_bridgeState.lastEventTs || 0));
    var d = await r.json();
    var events = d.events || [];
    if (events.length) {
      events.forEach(function(e) {
        if (e.timestamp > _bridgeState.lastEventTs) _bridgeState.lastEventTs = e.timestamp;
      });
      bridgeRenderFlow(events);
    } else if (!_bridgeState.lastEventTs && !document.getElementById('bridge-flow-log').children.length) {
      document.getElementById('bridge-flow-log').innerHTML = '<div class="lo-bridge-flow-empty">No relays yet. Messages will appear here when the bridge forwards them.</div>';
    }
  } catch(e) {}
}

function bridgeRenderFlow(events) {
  var box = document.getElementById('bridge-flow-log');
  // Clear empty-state if present
  if (box.querySelector('.lo-bridge-flow-empty')) box.innerHTML = '';
  // Prepend newest events
  var html = events.slice().reverse().map(function(e) {
    var time = formatTime(e.timestamp);
    var dir = (e.source === 'meshtastic' ? 'mt\u2192mc' : 'mc\u2192mt');
    var sender = e.sender_display || e.sender || '';
    var text = (e.text || '').replace(/^\[.+?\]\s*/, '');
    return (
      '<div class="lo-bridge-flow-row">' +
        '<span class="time">' + escapeHtml(time) + '</span>' +
        '<span class="dir">' + dir + '</span>' +
        '<span class="sender">' + escapeHtml(sender) + '</span>' +
        '<span class="text">' + escapeHtml(text) + '</span>' +
      '</div>'
    );
  }).join('');
  box.insertAdjacentHTML('afterbegin', html);
  // Cap at 200 rows to match server-side ring buffer
  var rows = box.querySelectorAll('.lo-bridge-flow-row');
  for (var i = 200; i < rows.length; i++) rows[i].remove();
}

// Submit on Enter (Shift+Enter for newline handled by browser if we ever use textarea)
document.addEventListener('keydown', function(e) {
  if (App.view !== 'ai') return;
  if (e.key !== 'Enter') return;
  var input = document.getElementById('ai-input');
  if (document.activeElement !== input) return;
  e.preventDefault();
  aiSend();
});

// ─── Poll Loop ─────────────────────────────────────────────────────────────

async function poll() {
  try {
    var r = await fetch('/api/state');
    var d = await r.json();
    App.state = d;

    // Connection — show each radio backend independently so a dual-radio rig
    // can see at a glance which one(s) are up.
    var backends = d.backends || [];
    function findBackend(tags) {
      for (var i = 0; i < backends.length; i++) {
        var p = String(backends[i].protocol || '').toLowerCase();
        if (tags.indexOf(p) !== -1) return backends[i];
      }
      return null;
    }
    var mt = findBackend(['mt', 'meshtastic']);
    var mc = findBackend(['mc', 'meshcore']);
    function paintDot(dotEl, labelEl, b, code) {
      if (!dotEl || !labelEl) return;
      var baseClass = code === 'mc' ? 'lo-dot mc' : 'lo-dot';
      if (!b) { dotEl.className = baseClass; labelEl.textContent = code.toUpperCase() + ' --'; return; }
      dotEl.className = baseClass + (b.connected ? ' on' : '');
      labelEl.textContent = code.toUpperCase() + (b.connected ? ' ON' : ' OFF');
    }
    paintDot(document.getElementById('hdr-mt-dot'), document.getElementById('hdr-mt-label'), mt, 'mt');
    paintDot(document.getElementById('hdr-mc-dot'), document.getElementById('hdr-mc-label'), mc, 'mc');
    // Keep the top-bar "+ RADIO" button label in sync with whether a MC radio
    // is attached — makes the button read as "manage MC" once connected.
    var addBtn = document.getElementById('hdr-add-radio');
    if (addBtn) {
      var mcOn = !!(mc && mc.connected);
      addBtn.textContent = mcOn ? '\u25C6 MC' : '+ RADIO';
      addBtn.classList.toggle('on', mcOn);
    }
    // Single "any backend up" flag preserves legacy modal + toast behavior.
    var connected = (mt && mt.connected) || (mc && mc.connected);
    if (!connected) { try { connected = !!d.connected; } catch(e) {} }
    // Disconnect alert — fire once on connected → disconnected transition
    if (App._lastConnected === true && !connected) {
      var ct = (d.connection_type || 'radio').toUpperCase();
      showToast('\u26a0 RADIO DISCONNECTED — ' + ct + ' connection lost', 'error');
    } else if (App._lastConnected === false && connected) {
      showToast('\u2713 Radio reconnected', 'info');
    }
    App._lastConnected = connected;
    checkConnectionForModal(connected);

    // New node detected toast — fires on nodes that appear mid-session (not on initial load).
    var currIds = d.known_nodes || [];
    var prevIds = App._lastKnownNodeIds;
    if (prevIds && prevIds.size > 0) {
      var freshIds = [];
      for (var ni = 0; ni < currIds.length; ni++) {
        var nid = currIds[ni];
        if (typeof nid === 'string' && nid.indexOf('channel:') !== 0 && !prevIds.has(nid)) {
          freshIds.push(nid);
        }
      }
      if (freshIds.length === 1) {
        showToast('\u2713 New node detected: ' + freshIds[0].slice(-4), 'info');
      } else if (freshIds.length > 1) {
        showToast('\u2713 ' + freshIds.length + ' new nodes detected', 'info');
      }
    }
    App._lastKnownNodeIds = new Set(currIds);

    // HUD
    document.getElementById('hud-nodes').textContent = d.node_count || 0;
    document.getElementById('hud-msgs').textContent = d.message_count || 0;
    document.getElementById('hud-model').textContent = d.model || '--';
    document.getElementById('hud-uptime').textContent = formatUptime(d.uptime || 0);
    if (App.colorByHwModel) renderHwLegend();

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
      var cfgReboot = document.getElementById('cfg-reboot-btn');
      var cfgShutdown = document.getElementById('cfg-shutdown-btn');
      if (cfgReboot) cfgReboot.style.display = connected ? '' : 'none';
      if (cfgShutdown) cfgShutdown.style.display = connected ? '' : 'none';
      var cfgUp = document.getElementById('cfg-uptime');
      if (cfgUp) cfgUp.textContent = formatUptime(d.uptime || 0);
    }

    // Rebuild graph / update map
    if (App.view === 'mesh' || App.view === 'traffic') {
      buildGraph(d);
      updateRibbon(d);
    } else if (App.view === 'map') {
      updateMapMarkers();
      updateRibbon(d);
    }

    // Fetch unread counts every 5 polls (~10s)
    if (!App._unreadTick) App._unreadTick = 0;
    if (++App._unreadTick % 5 === 0) {
      try {
        var tr = await fetch('/api/threads');
        var td = await tr.json();
        var uc = {};
        (td.contacts || []).forEach(function(c) { if (c.unread > 0) uc[c.contact_id] = c.unread; });
        App.unreadCounts = uc;
      } catch(e) {}
    }

    // Refresh open panel
    if (App.selectedNode) {
      if (isSelfId(App.selectedNode) && _openWindows[App.selectedNode]) {
        // Self panel reads straight from App.state — refresh every poll (free).
        loadSelfData(App.selectedNode);
      } else {
        // Peer panels hit /api/threads — soft refresh every 5 polls (~10s)
        if (!App._panelRefreshCount) App._panelRefreshCount = 0;
        App._panelRefreshCount++;
        if (App._panelRefreshCount % 5 === 0) openNodePanel(App.selectedNode);
      }
    }
  } catch(e) {}
}

// ─── Connect Modal ─────────────────────────────────────────────────────────

var _connectModalDismissed = false, _disconnectedSince = 0, _wasConnected = false, _userAckedModal = false;
// ── First-run wizard ─────────────────────────────────────────────────────
// Chains the primary (meshtastic) and secondary (meshcore) connect modals
// on the very first dashboard load, so a new user gets asked about BOTH
// radios — either is optional. A successful run marks "setup complete" in
// localStorage so subsequent reconnect dialogs behave exactly as before.
var _wizardActive = false;
try { _wizardActive = !localStorage.getItem('loracle-setup-complete'); } catch(e) {}

function wizardComplete() {
  _wizardActive = false;
  try { localStorage.setItem('loracle-setup-complete', '1'); } catch(e) {}
  _applyWizardChromePrimary(false);
  _applyWizardChromeSecondary(false);
  // Reset any success panels back to their form state so a future disconnect
  // shows the normal primary-connect form, not a stale success screen.
  _resetPrimaryPanels();
  _resetSecondaryPanels();
}

// ── Wizard success-panel helpers ─────────────────────────────────────────

function _showPrimarySuccessPanel() {
  var form = document.getElementById('connect-modal-form');
  var ok = document.getElementById('connect-modal-success');
  if (form) form.style.display = 'none';
  if (ok) ok.style.display = '';
  // Fill in a small detail line so the user can see WHAT connected.
  try {
    var addr = (document.getElementById('connect-address') || {}).value || '';
    var t = (document.getElementById('connect-type') || {}).value || '';
    var d = [t ? t.toUpperCase() : '', addr].filter(Boolean).join(' \u2014 ');
    var det = document.getElementById('connect-modal-success-detail');
    if (det) det.textContent = d;
  } catch(e) {}
}

function _resetPrimaryPanels() {
  var form = document.getElementById('connect-modal-form');
  var ok = document.getElementById('connect-modal-success');
  if (form) form.style.display = '';
  if (ok) ok.style.display = 'none';
}

function _showSecondarySuccessPanel(backend) {
  var form = document.getElementById('ar-form');
  var ok = document.getElementById('ar-success');
  if (form) form.style.display = 'none';
  if (ok) ok.style.display = '';
  var det = document.getElementById('ar-success-detail');
  if (det && backend) {
    var parts = [];
    if (backend.transport) parts.push(String(backend.transport).toUpperCase());
    if (backend.self_node_id) parts.push(backend.self_node_id);
    det.textContent = parts.join(' \u2014 ');
  }
}

function _resetSecondaryPanels() {
  var form = document.getElementById('ar-form');
  var ok = document.getElementById('ar-success');
  if (form) form.style.display = '';
  if (ok) ok.style.display = 'none';
}

function wizardAdvanceFromPrimarySuccess() {
  // User clicked NEXT on the MT success screen.
  document.getElementById('connect-modal').classList.remove('open');
  _connectModalDismissed = true;
  _userAckedModal = true;
  _resetPrimaryPanels();
  showAddRadioModal();
}

function wizardPrimaryDone() {
  // User clicked "DONE — JUST MESHTASTIC" on the MT success screen.
  document.getElementById('connect-modal').classList.remove('open');
  _connectModalDismissed = true;
  _userAckedModal = true;
  wizardComplete();  // also resets panels
}

function wizardFinishFromSecondarySuccess() {
  // User clicked DONE on the MC success screen.
  document.getElementById('add-radio-modal').classList.remove('open');
  wizardComplete();
}

function _applyWizardChromePrimary(active) {
  var step = document.getElementById('connect-modal-wizard-step');
  var skip = document.getElementById('connect-modal-skip-btn');
  var title = document.getElementById('connect-modal-title');
  var desc = document.getElementById('connect-modal-desc');
  if (step) step.style.display = active ? '' : 'none';
  if (skip) skip.style.display = active ? '' : 'none';
  if (active) {
    // Step 1 hosts the Meshtastic-connect flow today (the /api/connection/switch
    // endpoint is MT-only). Keep the title honest about that, but frame it as
    // "pick your first radio" rather than "MT is mandatory, MC is an add-on."
    if (title) title.textContent = 'CONNECT YOUR FIRST RADIO';
    if (desc) desc.textContent = 'Pair your first mesh radio. If you have both, either protocol works — MeshTastic and MeshCore are equal citizens on the canvas. Skip this step if your first radio is MeshCore.';
  }
}

function _applyWizardChromeSecondary(active) {
  var step = document.getElementById('ar-wizard-step');
  var skip = document.getElementById('ar-skip-btn');
  var cancel = document.getElementById('ar-cancel-btn');
  var title = document.getElementById('ar-title');
  var desc = document.getElementById('ar-description');
  if (step) step.style.display = active ? '' : 'none';
  if (skip) skip.style.display = active ? '' : 'none';
  if (cancel) cancel.textContent = active ? 'BACK' : 'CANCEL';
  if (active && title) title.textContent = 'ADD A SECOND RADIO (OPTIONAL)';
  if (active && desc) desc.textContent = 'Add a MeshCore radio alongside your first one. When both are up, public channel 0 auto-bridges in both directions so MT and MC peers can talk to each other. Skip if you\u2019re running a single network.';
}

function wizardSkipPrimary() {
  // User skipped step 1 — go straight to the MeshCore prompt.
  document.getElementById('connect-modal').classList.remove('open');
  _connectModalDismissed = true;
  _userAckedModal = true;
  showAddRadioModal();  // still wizard-active; chrome re-applies inside
}

function wizardSkipSecondary() {
  // Close directly (NOT via hideAddRadioModal — that routes CANCEL back to
  // step 1 for mid-wizard BACK navigation). The explicit SKIP button exits
  // the wizard entirely.
  document.getElementById('add-radio-modal').classList.remove('open');
  document.getElementById('ar-status').textContent = '';
  wizardComplete();
}

function showConnectModal() {
  if (_connectModalDismissed) return;
  _resetPrimaryPanels();  // clean slate if previously in success mode
  _applyWizardChromePrimary(_wizardActive);
  document.getElementById('connect-modal').classList.add('open');
}
function dismissConnectModal() {
  _userAckedModal = true;
  _connectModalDismissed = true;
  document.getElementById('connect-modal').classList.remove('open');
  // DISMISS mid-wizard ends the wizard — user decided to set up manually.
  if (_wizardActive) wizardComplete();
}
function hideConnectModal() { document.getElementById('connect-modal').classList.remove('open'); }

function connectModalTypeChanged() {
  var sel = document.getElementById('connect-type');
  var isSerial = sel.value === 'serial';
  document.getElementById('connect-address-row').style.display = sel.value === 'ble' ? 'none' : '';
  document.getElementById('connect-scan-row').style.display = sel.value === 'ble' ? '' : 'none';
  // SCAN PORTS button only makes sense for serial transport.
  var btn = document.getElementById('connect-serial-scan-btn');
  if (btn) btn.style.display = isSerial ? '' : 'none';
  var listRow = document.getElementById('connect-serial-list-row');
  if (listRow && !isSerial) listRow.style.display = 'none';
  if (isSerial) document.getElementById('connect-address').placeholder = 'auto-detect (or /dev/...)';
  else if (sel.value === 'tcp') document.getElementById('connect-address').placeholder = '192.168.1.1:4403';
}

// ── Serial-port scan — shared between the primary and secondary modals ────

async function _fetchSerialPorts() {
  var r = await fetch('/api/serial/scan');
  var d = await r.json();
  return (d && d.ports) || [];
}

function _renderSerialPorts(listEl, ports, onPick) {
  if (!ports.length) {
    listEl.innerHTML = '<div style="padding:6px 0;color:var(--lo-faint)">No serial devices found. Plug in a USB radio and click SCAN PORTS again.</div>';
    return;
  }
  listEl.innerHTML = ports.map(function(p, i) {
    var pid = (p.vid ? ' VID:' + p.vid.toString(16).toUpperCase().padStart(4,'0') : '') +
              (p.pid ? ' PID:' + p.pid.toString(16).toUpperCase().padStart(4,'0') : '');
    var tag = p.likely_radio ? '<span style="color:var(--lo-accent-2)">\u25CF</span> ' : '';
    var desc = [p.description, p.manufacturer].filter(function(x){return x}).join(' \u2014 ');
    return '<div class="lo-serial-port" data-idx="' + i + '" style="padding:5px 8px;margin:2px 0;background:var(--lo-bg-deep);cursor:pointer;font-family:var(--font-mono)">' +
      tag + '<strong style="color:var(--lo-ink)">' + escapeHtml(p.device) + '</strong>' +
      (desc ? ' <span style="color:var(--lo-dim)">\u2014 ' + escapeHtml(desc) + '</span>' : '') +
      (pid ? ' <span style="color:var(--lo-faint);font-size:9px">' + escapeHtml(pid) + '</span>' : '') +
      '</div>';
  }).join('');
  Array.from(listEl.querySelectorAll('.lo-serial-port')).forEach(function(row) {
    row.addEventListener('click', function() {
      var idx = parseInt(row.dataset.idx, 10);
      onPick(ports[idx]);
    });
  });
}

async function connectModalSerialScan() {
  var listRow = document.getElementById('connect-serial-list-row');
  var listEl = document.getElementById('connect-serial-list');
  listRow.style.display = '';
  listEl.innerHTML = '<span style="color:var(--lo-dim)">Scanning\u2026</span>';
  try {
    var ports = await _fetchSerialPorts();
    _renderSerialPorts(listEl, ports, function(p) {
      document.getElementById('connect-address').value = p.device;
      listEl.innerHTML = '<div style="color:var(--lo-accent-2)">Selected: ' + escapeHtml(p.device) + '</div>';
    });
  } catch(e) {
    listEl.innerHTML = '<span style="color:#c0392b">Scan failed: ' + escapeHtml(String(e)) + '</span>';
  }
}

async function addRadioSerialScan() {
  var listRow = document.getElementById('ar-serial-list-row');
  var listEl = document.getElementById('ar-serial-list');
  listRow.style.display = '';
  listEl.innerHTML = '<span style="color:var(--lo-dim)">Scanning\u2026</span>';
  try {
    var ports = await _fetchSerialPorts();
    _renderSerialPorts(listEl, ports, function(p) {
      document.getElementById('ar-serial-port').value = p.device;
      listEl.innerHTML = '<div style="color:var(--lo-accent-2)">Selected: ' + escapeHtml(p.device) + '</div>';
    });
  } catch(e) {
    listEl.innerHTML = '<span style="color:#c0392b">Scan failed: ' + escapeHtml(String(e)) + '</span>';
  }
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
  // Same reasoning as connectFromModal: don't pre-ack the modal — the poll
  // loop needs to see an un-acked modal to paint the wizard success panel
  // instead of silently closing.
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
  // IMPORTANT: we deliberately do NOT set _userAckedModal=true here. In wizard
  // mode the poll loop needs to see an un-acked modal so it can swap in the
  // success panel (if we set it true, the next poll would just call
  // hideConnectModal() and skip the success panel entirely — the user would
  // see the modal silently close and have to find the "+ RADIO" button to get
  // to step 2). In non-wizard mode the 3s auto-dismiss timer flips this flag
  // once the user has had time to read "RADIO CONNECTED". See the matching
  // branch in checkConnectionForModal.
  var type = document.getElementById('connect-type').value;
  var addr = document.getElementById('connect-address').value.trim();
  var payload = {type: type};
  if (type === 'tcp' && addr) {
    if (addr.indexOf(':') !== -1) { var p = addr.split(':'); payload.host = p[0]; payload.port = parseInt(p[1]); }
    else payload.host = addr;
  } else { payload.address = addr || null; }
  var statusMsg = _wizardActive
    ? 'Connecting\u2026 this screen will update when the radio comes up.'
    : 'Connecting... (modal will close when connected)';
  document.getElementById('connect-modal-status').textContent = statusMsg;
  // Fire and forget — poll loop handles the rest
  fetch('/api/connection/switch', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  }).catch(function() {});
}

// ── Add Secondary Radio modal ─────────────────────────────────────────────

function showAddRadioModal() {
  _resetSecondaryPanels();  // clean slate in case the previous session left success-panel visible
  _applyWizardChromeSecondary(_wizardActive);
  refreshAddRadioModal();
  document.getElementById('add-radio-modal').classList.add('open');
}
function hideAddRadioModal() {
  document.getElementById('add-radio-modal').classList.remove('open');
  document.getElementById('ar-status').textContent = '';
  // CANCEL during wizard = go back to step 1.
  if (_wizardActive) {
    _connectModalDismissed = false;
    _userAckedModal = false;
    showConnectModal();
  }
}
function arTransportChanged() {
  var t = document.getElementById('ar-transport').value;
  document.getElementById('ar-serial-row').style.display = (t === 'serial') ? '' : 'none';
  document.getElementById('ar-tcp-row').style.display    = (t === 'tcp')    ? '' : 'none';
  document.getElementById('ar-ble-row').style.display    = (t === 'ble')    ? '' : 'none';
}
function refreshAddRadioModal() {
  // Show current MC backend status at the top of the modal (if any).
  var backends = (App.state && App.state.backends) || [];
  var mc = null;
  for (var i = 0; i < backends.length; i++) {
    var p = String(backends[i].protocol || '').toLowerCase();
    if (p === 'mc' || p === 'meshcore') { mc = backends[i]; break; }
  }
  var active = document.getElementById('ar-active-row');
  if (mc && mc.connected) {
    active.style.display = '';
    var parts = [];
    if (mc.transport) parts.push(String(mc.transport).toUpperCase());
    if (mc.self_node_id) parts.push(mc.self_node_id);
    document.getElementById('ar-active-detail').textContent = parts.join(' \u2014 ');
  } else {
    active.style.display = 'none';
  }
  // Update the top-bar button label so it telegraphs "manage MC" vs "add MC"
  var btn = document.getElementById('hdr-add-radio');
  if (btn) {
    btn.textContent = (mc && mc.connected) ? '\u25C6 MC' : '+ RADIO';
    btn.classList.toggle('on', !!(mc && mc.connected));
  }
}
async function submitAddRadio() {
  var transport = document.getElementById('ar-transport').value;
  var body = {transport: transport, seed_bridge: document.getElementById('ar-seed-bridge').checked};
  if (transport === 'serial') {
    body.serial_port = document.getElementById('ar-serial-port').value.trim();
    if (!body.serial_port) { arSetStatus('Enter the serial device path', 'error'); return; }
  } else if (transport === 'tcp') {
    body.tcp_host = document.getElementById('ar-tcp-host').value.trim();
    body.tcp_port = parseInt(document.getElementById('ar-tcp-port').value, 10) || 4000;
    if (!body.tcp_host) { arSetStatus('Enter the TCP host', 'error'); return; }
  } else if (transport === 'ble') {
    body.ble_address = document.getElementById('ar-ble-address').value.trim() || null;
  }
  var btn = document.getElementById('ar-submit-btn');
  btn.disabled = true;
  arSetStatus('Connecting\u2026 (this can take up to 30s)');
  try {
    var r = await fetch('/api/backends/add', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    var d = await r.json();
    if (!r.ok || d.error) {
      arSetStatus(d.error || ('HTTP ' + r.status), 'error');
    } else {
      arSetStatus('\u2713 Connected', 'ok');
      showToast('MeshCore radio connected');
      if (_wizardActive) {
        // Paced wizard success screen — user clicks DONE to finish.
        _showSecondarySuccessPanel(d && d.backend);
      } else {
        // Normal path (post-wizard "+ RADIO" flow): brief confirmation then close.
        setTimeout(hideAddRadioModal, 900);
      }
    }
  } catch (e) {
    arSetStatus(String(e), 'error');
  } finally {
    btn.disabled = false;
  }
}
async function removeSecondaryRadio() {
  if (!confirm('Disconnect the MeshCore radio?')) return;
  try {
    var r = await fetch('/api/backends/remove', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
    var d = await r.json();
    if (!r.ok || d.error) {
      arSetStatus(d.error || ('HTTP ' + r.status), 'error');
    } else {
      showToast('MeshCore radio disconnected');
      hideAddRadioModal();
    }
  } catch (e) {
    arSetStatus(String(e), 'error');
  }
}
function arSetStatus(msg, level) {
  var el = document.getElementById('ar-status');
  el.textContent = msg || '';
  el.style.color = (level === 'error') ? '#c0392b' : ((level === 'ok') ? 'var(--lo-accent-2)' : 'var(--lo-dim)');
}

function checkConnectionForModal(connected) {
  if (connected) {
    _wasConnected = true;
    _disconnectedSince = 0;
    if (_userAckedModal) {
      hideConnectModal();
    } else if (document.getElementById('connect-modal').classList.contains('open')) {
      if (_wizardActive) {
        // Paced wizard success: swap the form out for a confirmation panel
        // with explicit NEXT / DONE buttons. User clicks to advance — no
        // auto-close, so they can read the success message.
        _showPrimarySuccessPanel();
      } else {
        // Normal (post-wizard) flow: brief confirmation, auto-close.
        document.getElementById('connect-modal-title').textContent = 'RADIO CONNECTED';
        document.getElementById('connect-modal-desc').textContent = 'Auto-connected successfully. You can dismiss this dialog or change connection settings.';
        document.getElementById('connect-modal-status').textContent = '';
        if (!checkConnectionForModal._autoDismissTimer) {
          checkConnectionForModal._autoDismissTimer = setTimeout(function() {
            checkConnectionForModal._autoDismissTimer = null;
            if (!_userAckedModal) { _userAckedModal = true; hideConnectModal(); }
          }, 3000);
        }
      }
    }
    return;
  }
  if (checkConnectionForModal._autoDismissTimer) {
    clearTimeout(checkConnectionForModal._autoDismissTimer);
    checkConnectionForModal._autoDismissTimer = null;
  }
  if (!_wasConnected && !_connectModalDismissed) {
    document.getElementById('connect-modal-title').textContent = 'CONNECT A RADIO';
    document.getElementById('connect-modal-desc').textContent = 'No radio detected. Plug in a USB radio, or scan for nearby Bluetooth devices.';
    showConnectModal();
    return;
  }
  if (_wasConnected && !_connectModalDismissed) {
    if (!_disconnectedSince) _disconnectedSince = Date.now();
    if (Date.now() - _disconnectedSince > 10000) { _connectModalDismissed = false; showConnectModal(); }
  }
}

// ─── Map View ──────────────────────────────────────────────────────────────

var _map = null, _mapMarkers = {}, _mapLabels = {}, _covLayer = null;
function initMap() {
  if (_map) { updateMapMarkers(); return; }
  _map = L.map('map-view').setView([39.8, -98.5], 4);
  L.tileLayer('/tiles/{z}/{x}/{y}.png', {
    maxZoom: 18,
    attribution: '\u00a9 OpenStreetMap'
  }).addTo(_map);
  updateMapMarkers();
}

function updateMapMarkers() {
  if (!_map || !App.state) return;
  var positions = App.state.node_positions || {};
  var meta = App.state.node_meta || {};
  var dm = App.state.device_metrics || {};
  var backends = App.state.backends || [];
  var selfId = (backends.length > 0 && backends[0].self_node_id) ? backends[0].self_node_id : null;
  var bounds = [];

  // Protocol check: MC nodes have ids prefixed with 'mc:'. Respect App.scope.
  function _idInScope(id) {
    var s = App.scope || 'all';
    if (s === 'all') return true;
    var isMC = (id || '').indexOf('mc:') === 0;
    return s === 'mc' ? isMC : !isMC;
  }

  // Remove stale markers and labels (gone from positions, OR dropped out of scope)
  Object.keys(_mapMarkers).forEach(function(id) {
    if (!positions[id] || !_idInScope(id)) {
      _map.removeLayer(_mapMarkers[id]); delete _mapMarkers[id];
      if (_mapLabels[id]) { _map.removeLayer(_mapLabels[id]); delete _mapLabels[id]; }
    }
  });

  Object.keys(positions).forEach(function(nid) {
    if (!_idInScope(nid)) return;
    var pos = positions[nid];
    if (!pos.lat || !pos.lon) return;
    var ll = [pos.lat, pos.lon];
    bounds.push(ll);
    var isSelf = (selfId && nid === selfId);
    var m = meta[nid] || {};
    var d = dm[nid] || {};
    // Use the resolved canvas label (custom_name → long → short) so map and mesh agree
    var canvasNode = (App.nodes || []).find(function(x) { return x.id === nid; });
    var label = (canvasNode && canvasNode.label) || (nid.length > 8 ? nid.slice(-6) : nid);
    var isFav = !!(canvasNode && canvasNode.isFavorite);
    var hops = (typeof m.hops === 'number') ? (m.hops === 0 ? 'direct' : m.hops + 'h') : '';
    var batt = (d.battery !== undefined) ? ' ' + d.battery + '%' : '';
    var tooltipHtml = (isFav ? '\u2605 ' : '') + escapeHtml(label) +
      (hops ? ' \u00b7 ' + hops : '') + batt +
      (isSelf ? '' : '\n(click to open)');

    if (_mapMarkers[nid]) {
      _mapMarkers[nid].setLatLng(ll);
      _mapMarkers[nid].setTooltipContent(tooltipHtml);
      // Update click binding so rename/favorite changes are reflected
      _mapMarkers[nid].off('click');
      if (!isSelf) _mapMarkers[nid].on('click', function() { _openMapNode(nid); });
    } else {
      var icon = L.divIcon({
        className: 'lo-map-marker' + (isSelf ? ' self' : '') + (isFav ? ' fav' : ''),
        iconSize: [10, 10], iconAnchor: [5, 5]
      });
      var marker = L.marker(ll, {icon: icon}).addTo(_map);
      marker.bindTooltip(tooltipHtml, {direction: 'top', offset: [0, -6]});
      if (!isSelf) marker.on('click', function() { _openMapNode(nid); });
      var labelMarker = L.marker(ll, {
        icon: L.divIcon({className: 'lo-map-label', html: escapeHtml(label), iconSize: null, iconAnchor: [-6, -2]}),
        interactive: false
      }).addTo(_map);
      _mapMarkers[nid] = marker;
      _mapLabels[nid] = labelMarker;
    }
  });

  if (bounds.length > 0 && !App._mapFitted) {
    _map.fitBounds(bounds, {padding: [40, 40], maxZoom: 14});
    App._mapFitted = true;
  }
}

// Open the same thread panel used on the mesh canvas when a map marker is clicked.
// If buildGraph hasn't run yet for this node, synthesize a minimal node object.
function _openMapNode(nid) {
  var node = (App.nodes || []).find(function(x) { return x.id === nid; });
  if (!node) {
    node = { id: nid, label: nid.length > 8 ? nid.slice(-6) : nid, isSelf: false, isChannel: false };
  }
  openFloatWindow(node);
}

async function toggleCoverageLayer() {
  if (_covLayer) { _map.removeLayer(_covLayer); _covLayer = null; return; }
  try {
    var r = await fetch('/api/coverage/samples?limit=5000');
    var d = await r.json();
    if (!d || !d.samples || d.samples.length === 0) { showToast('No coverage data yet'); return; }
    var points = d.samples.map(function(s) { return [s.lat, s.lon, Math.max(0.2, (s.rssi + 120) / 80)]; });
    _covLayer = L.heatLayer(points, {radius: 20, blur: 15, maxZoom: 17, gradient: {0.2: 'blue', 0.5: 'lime', 0.8: 'yellow', 1.0: 'red'}}).addTo(_map);
  } catch(e) { showToast('Coverage load error', 'error'); }
}

// ─── Node List Sidebar ─────────────────────────────────────────────────────

var _nodeSort = 'name';
function toggleNodeList() { document.getElementById('node-sidebar').classList.toggle('open'); renderNodeList(); }
function setNodeSort(sort, btn) {
  _nodeSort = sort;
  document.querySelectorAll('.lo-ns-sort button').forEach(function(b) { b.classList.remove('active'); });
  if (btn) btn.classList.add('active');
  renderNodeList();
}
function renderNodeList() {
  var list = document.getElementById('ns-list');
  var filter = (document.getElementById('ns-search').value || '').toLowerCase();
  var items = App.nodes.filter(function(n) {
    if (n.isSelf) return false;
    if (!nodeInScope(n)) return false;
    if (filter && n.label.toLowerCase().indexOf(filter) === -1 && n.id.toLowerCase().indexOf(filter) === -1) return false;
    return true;
  }).map(function(n) {
    return { id: n.id, label: n.label, hops: n.hops, lastHeard: n.lastHeard || 0, unread: App.unreadCounts[n.id] || 0, isFavorite: !!n.isFavorite, isMC: !!n.isMC };
  });
  items.sort(function(a, b) {
    // Favorites always float to the top within the chosen sort
    if (a.isFavorite !== b.isFavorite) return a.isFavorite ? -1 : 1;
    if (_nodeSort === 'hops') return (a.hops === null ? 99 : a.hops) - (b.hops === null ? 99 : b.hops);
    if (_nodeSort === 'heard') return (b.lastHeard || 0) - (a.lastHeard || 0);
    if (_nodeSort === 'unread') return b.unread - a.unread;
    return a.label.localeCompare(b.label);
  });
  list.innerHTML = items.map(function(n) {
    var hops = n.hops !== null ? (n.hops === 0 ? 'direct' : n.hops + 'h') : '--';
    var heard = n.lastHeard ? relativeTime(n.lastHeard) : '--';
    var badge = n.unread > 0 ? '<span class="lo-ns-badge">' + n.unread + '</span>' : '';
    var star = n.isFavorite ? '<span style="color:#f1c40f;margin-right:3px">\u2605</span>' : '';
    // Protocol badge — both protocols get an equal badge so neither feels "default".
    // MT: teal circle-dot, MC: purple diamond — mirrors the canvas and header icons.
    var protoTag = n.isMC
      ? '<span class="lo-ns-proto lo-ns-proto-mc" title="MeshCore">mc</span>'
      : '<span class="lo-ns-proto lo-ns-proto-mt" title="Meshtastic">mt</span>';
    return '<div class="lo-ns-row" onclick="openFloatWindow(App.nodes.find(function(x){return x.id===\'' + escapeHtml(n.id) + '\'}))">' +
      '<span class="lo-ns-name">' + star + protoTag + escapeHtml(n.label) + '</span>' +
      '<span class="lo-ns-hops">' + hops + '</span>' +
      '<span class="lo-ns-heard">' + heard + '</span>' +
      badge + '</div>';
  }).join('');
}

// ─── Message Search ────────────────────────────────────────────────────────

var _msgSearchTimer = null;
function debounceMessageSearch() {
  clearTimeout(_msgSearchTimer);
  _msgSearchTimer = setTimeout(doMessageSearch, 400);
}
async function doMessageSearch() {
  var q = (document.getElementById('ns-msg-search').value || '').trim();
  var el = document.getElementById('ns-msg-results');
  if (q.length < 2) { el.innerHTML = ''; return; }
  try {
    var r = await fetch('/api/messages/search?q=' + encodeURIComponent(q));
    var d = await r.json();
    if (!d.results || d.results.length === 0) { el.innerHTML = '<div style="padding:4px;color:var(--lo-faint)">No results</div>'; return; }
    el.innerHTML = d.results.map(function(m) {
      var name = m.short_name || (m.contact_id || '').slice(-6);
      var arrow = m.direction === 'in' ? '\u2190' : '\u2192';
      var time = formatTime(m.timestamp);
      return '<div style="padding:3px 0;border-bottom:1px solid var(--lo-divider);cursor:pointer" onclick="openFloatWindow(App.nodes.find(function(x){return x.id===\'' + escapeHtml(m.contact_id) + '\'}))">' +
        '<span style="color:var(--lo-faint)">' + time + '</span> ' + arrow + ' <span style="color:var(--lo-ink)">' + escapeHtml(name) + '</span><br>' +
        '<span>' + escapeHtml(m.text.substring(0, 80)) + '</span></div>';
    }).join('');
  } catch(e) { el.innerHTML = ''; }
}

// ─── Help + Theme ──────────────────────────────────────────────────────────

document.getElementById('help-toggle').addEventListener('click', function() { document.getElementById('help-popover').classList.toggle('open'); });
document.addEventListener('click', function(e) { if (!e.target.closest('#help-popover') && !e.target.closest('#help-toggle')) document.getElementById('help-popover').classList.remove('open'); });
document.getElementById('theme-toggle').addEventListener('click', function() { var c = document.documentElement.getAttribute('data-theme') || 'light'; setTheme(c === 'light' ? 'dark' : 'light'); });

// ─── Onboarding ────────────────────────────────────────────────────────────

var _obStep = 0, _obTotal = 9;
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
  cfgLoadRagDocs(); cfgLoadDbStats(); cfgLoadRouting(); cfgLoadPacks(); cfgLoadChannels(); cfgLoadRadio();
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
async function cfgLoadChannels() {
  var d = await callApi('GET', '/api/channels');
  var el = document.getElementById('cfg-channels-list');
  if (!d || !d.channels || d.channels.length === 0) { el.textContent = 'No channels (radio not connected)'; return; }
  el.innerHTML = d.channels.map(function(ch) {
    if (ch.role === 'disabled') return '';
    var psk = ch.has_psk ? '\u{1f512}' : '\u{1f513}';
    var name = ch.name || ('Channel ' + ch.index);
    var up = ch.uplink_enabled ? '\u2191' : '';
    var dn = ch.downlink_enabled ? '\u2193' : '';
    return '<div style="padding:4px 0;border-bottom:1px solid var(--lo-divider)">' +
      '<span style="color:var(--lo-ink);font-weight:500">' + ch.index + '. ' + escapeHtml(name) + '</span> ' +
      '<span style="color:var(--lo-faint);font-size:10px">' + ch.role.toUpperCase() + ' ' + psk + ' ' + up + dn + '</span></div>';
  }).filter(Boolean).join('') || 'No active channels';
}
var _radioCfgReadOnly = false;  // true when the primary backend is MC (read-only)
async function cfgLoadRadio() {
  var d = await callApi('GET', '/api/radio/config');
  if (!d || d.error) return;
  _radioCfgReadOnly = !!d.read_only;
  // MeshCore has no writable LoRa config — show a notice and disable save
  // so the user isn't misled into thinking they can tune region/tx here.
  var saveBtn = document.getElementById('cfg-radio-save');
  if (saveBtn) saveBtn.disabled = _radioCfgReadOnly;
  var noticeId = 'cfg-radio-notice';
  var notice = document.getElementById(noticeId);
  if (_radioCfgReadOnly) {
    if (!notice && saveBtn && saveBtn.parentNode) {
      notice = document.createElement('div');
      notice.id = noticeId;
      notice.style.cssText = 'font-size:10px;color:var(--lo-faint);margin:6px 0;padding:6px;border:1px dashed var(--lo-divider)';
      notice.textContent = 'This radio (' + (d.protocol || 'non-mt').toUpperCase() +
        ') exposes a read-only config. LoRa tuning is Meshtastic-only.';
      saveBtn.parentNode.insertBefore(notice, saveBtn);
    }
    // Populate only the fields MC can fill from its device view, leave MT fields blank
    return;
  } else if (notice) {
    notice.remove();
  }
  document.getElementById('cfg-radio-region').value = d.region || 0;
  document.getElementById('cfg-radio-modem').value = d.modem_preset || 0;
  document.getElementById('cfg-radio-tx').value = d.tx_power || 0;
  document.getElementById('cfg-radio-hops').value = d.hop_limit || 3;
}
async function cfgSaveRadio() {
  if (_radioCfgReadOnly) {
    showToast('This radio is read-only — cannot save LoRa config', 'error');
    return;
  }
  if (!confirm('Save radio config? The radio may restart.')) return;
  var d = await callApi('POST', '/api/radio/config', {
    region: parseInt(document.getElementById('cfg-radio-region').value),
    modem_preset: parseInt(document.getElementById('cfg-radio-modem').value),
    tx_power: parseInt(document.getElementById('cfg-radio-tx').value),
    hop_limit: parseInt(document.getElementById('cfg-radio-hops').value),
  });
  if (d && d.ok) showToast('Radio config saved');
  _configDirty = false;
}
async function cfgReboot() { if (!confirm('Reboot the radio device?')) return; var d = await callApi('POST', '/api/device/reboot'); if (d && d.ok) showToast('Reboot command sent'); }
async function cfgShutdown() { if (!confirm('Shutdown the radio device? You will need to manually power it back on.')) return; var d = await callApi('POST', '/api/device/shutdown'); if (d && d.ok) showToast('Shutdown command sent'); }
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
// Sync HW-color button with persisted preference
(function() {
  var btn = document.getElementById('hud-hwcolor-btn');
  if (btn && App.colorByHwModel) btn.classList.add('active');
  renderHwLegend();
})();
// Always show connect modal on load — poll will auto-hide if connected
showConnectModal();
poll();
setInterval(poll, 2000);
</script>
</body>
</html>"""
