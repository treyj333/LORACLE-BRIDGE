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
    """Inject addon tabs into the dashboard HTML at serve time."""
    if not _addon_tabs:
        return html

    # Inject tab buttons before </nav>
    tab_buttons = ""
    tab_sections = ""
    tab_js = ""
    for tab in _addon_tabs:
        tab_buttons += (
            f'  <button class="tab-btn" data-tab="{tab["id"]}">'
            f'{tab["label"]}</button>\n'
        )
        tab_sections += (
            f'<section id="tab-{tab["id"]}" class="tab-panel">\n'
            f'{tab["html"]}\n'
            f'</section>\n\n'
        )
        tab_js += f'\n// ─── Addon: {tab["label"]} ───\n{tab["js"]}\n'

    # Inject at markers
    html = html.replace("</nav>", tab_buttons + "</nav>")
    html = html.replace("</main>", tab_sections + "</main>")
    html = html.replace("</script>\n</body>", tab_js + "\n</script>\n</body>")

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
    # Node positions for map
    if _bridge and hasattr(_bridge, "_node_positions"):
        state["node_positions"] = _bridge._node_positions
        state["node_positions_count"] = len(_bridge._node_positions)
        state["node_meta"] = getattr(_bridge, "_node_meta", {}) or {}
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
    return jsonify(state)


@app.route("/api/coverage/stats")
def api_coverage_stats():
    """Return summary stats about the coverage log."""
    if _bridge is None or not hasattr(_bridge, "coverage"):
        return jsonify({"count": 0, "nodes": 0, "time_start": None, "time_end": None, "bbox": None})
    return jsonify(_bridge.coverage.stats())


@app.route("/api/coverage/samples")
def api_coverage_samples():
    """Return raw coverage samples. Optional ?limit=N (default 5000)."""
    if _bridge is None or not hasattr(_bridge, "coverage"):
        return jsonify({"samples": []})
    try:
        limit = int(request.args.get("limit", 5000))
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
    keys = list(_bridge.ollama._history.keys())
    for k in keys:
        _bridge.ollama._history[k].clear()
    return jsonify({"ok": True, "cleared": len(keys)})


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
    limit = int(request.args.get("limit", 200))
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
        from standalone_bridge import _dedup_cache
        info["dedup_cache_size"] = len(_dedup_cache)
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
    channel = int(data.get("channel", 0))
    try:
        from meshtastic import BROADCAST_ADDR
        is_broadcast = (not node_id or node_id.lower() == "broadcast")
        dest = BROADCAST_ADDR if is_broadcast else node_id
        _bridge.interface.sendText(
            text, destinationId=dest, channelIndex=channel, wantAck=False,
        )
        direction = "broadcast" if is_broadcast else f"DM to {node_id}"
        record_message("out", "dashboard", text, direction=direction)
        return jsonify({"ok": True, "direction": direction})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    tmp_path = os.path.join(tempfile.gettempdir(), f.filename)
    try:
        f.save(tmp_path)
        result = _bridge.rag_engine.ingest_file(tmp_path)
        return jsonify({"ok": True, "filename": result.get("filename", f.filename),
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
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LORACLE BRIDGE</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
<style>
:root {
  --bg-primary: #181a16;
  --bg-secondary: #1f211c;
  --bg-tertiary: #272a24;
  --bg-input: #141613;
  --border: #3d3f38;
  --border-subtle: #2e302a;
  --text-primary: #ffcc00;
  --text-secondary: #33ff33;
  --text-muted: #7a8a6a;
  --text-dim: #4a4f40;
  --accent-blue: #ffcc00;
  --accent-green: #00ff41;
  --accent-red: #ff3333;
  --accent-yellow: #ffaa00;
  --accent-purple: #88aaff;
  --accent-orange: #ff6600;
  --radius: 0px;
  --radius-sm: 0px;
  --font-mono: 'Share Tech Mono', 'Courier New', 'Consolas', monospace;
  --font-sans: 'Share Tech Mono', 'Courier New', 'Consolas', monospace;
  --border-width: 2px;
  --shadow-raised: 2px 2px 0px #0a0f06, inset 0 1px 0 rgba(255,204,0,0.1);
  --shadow-inset: inset 2px 2px 4px #0a0f06, inset -1px -1px 0 rgba(255,204,0,0.05);
  --glow-green: 0 0 8px rgba(0,255,65,0.4);
  --glow-amber: 0 0 8px rgba(255,204,0,0.4);
  --glow-red: 0 0 8px rgba(255,51,51,0.5);
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: var(--font-sans); background: var(--bg-primary); color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px; }
body::after {
  content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 9999;
  background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px);
}
button { cursor: pointer; font-family: var(--font-sans); }
input, select, textarea { font-family: var(--font-sans); text-transform: none; }

/* ─── Top Bar ─── */
#top-bar {
  position: sticky; top: 0; z-index: 100;
  background: var(--bg-secondary); border-top: 3px solid var(--accent-blue);
  border-bottom: var(--border-width) solid var(--border);
  padding: 10px 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
}
.logo { display: flex; align-items: center; gap: 8px; font-weight: 400; font-size: 1.05em; color: var(--text-primary); white-space: nowrap; letter-spacing: 3px; text-shadow: var(--glow-amber); }
.logo svg { flex-shrink: 0; }
.top-badges { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-left: auto; }
.badge {
  display: flex; align-items: center; gap: 5px;
  background: var(--bg-tertiary); border: var(--border-width) solid var(--border);
  border-radius: 0; padding: 3px 10px; font-size: 0.78em; white-space: nowrap;
}
.badge .label { color: var(--text-muted); }
.badge .val { color: var(--text-primary); font-weight: 400; }
.conn-dot {
  width: 10px; height: 10px; border-radius: 50%; display: inline-block; flex-shrink: 0;
  border: 1px solid rgba(255,255,255,0.1);
}
.conn-dot.on { background: var(--accent-green); box-shadow: var(--glow-green); animation: pulse 2s ease-in-out infinite; }
.conn-dot.off { background: var(--accent-red); box-shadow: var(--glow-red); }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
@keyframes nodePing {
  0%   { transform: scale(0.6); opacity: 0.9; }
  80%  { transform: scale(2.4); opacity: 0.0; }
  100% { transform: scale(2.4); opacity: 0.0; }
}
.node-marker {
  position: relative;
  width: 28px;
  height: 28px;
  pointer-events: auto;
}
.node-marker .ring {
  position: absolute; inset: 0;
  border-radius: 50%;
  border: 2px solid #00ff41;
  box-shadow: 0 0 16px rgba(0, 255, 65, 0.9), 0 0 4px rgba(0, 255, 65, 1) inset;
  animation: nodePing 2.2s ease-out infinite;
}
.node-marker .core {
  position: absolute;
  left: 50%; top: 50%;
  width: 14px; height: 14px;
  margin-left: -7px; margin-top: -7px;
  border-radius: 50%;
  background: #00ff41;
  border: 3px solid #0a0f06;
  box-shadow:
    0 0 14px rgba(0, 255, 65, 1),
    0 0 28px rgba(0, 255, 65, 0.6),
    0 0 2px #000 inset;
}
.node-marker .label {
  position: absolute;
  top: 30px;
  left: 50%;
  transform: translateX(-50%);
  font-family: 'Share Tech Mono', monospace;
  font-size: 11px;
  font-weight: bold;
  color: #00ff41;
  background: rgba(10, 15, 6, 0.92);
  border: 1px solid #00ff41;
  padding: 2px 6px;
  white-space: nowrap;
  text-shadow: 0 0 4px rgba(0, 255, 65, 0.8);
  box-shadow: 0 0 6px rgba(0, 255, 65, 0.4);
  letter-spacing: 0.5px;
}
.node-marker.stale .ring    { border-color: #d6c100; box-shadow: 0 0 12px rgba(214, 193, 0, 0.7); animation-duration: 4s; }
.node-marker.stale .core    { background: #d6c100; box-shadow: 0 0 10px rgba(214, 193, 0, 0.9), 0 0 2px #000 inset; }
.node-marker.stale .label   { color: #d6c100; border-color: #d6c100; text-shadow: 0 0 4px rgba(214, 193, 0, 0.8); }

/* ─── Tabs ─── */
#tab-nav {
  display: flex; background: var(--bg-secondary); border-bottom: var(--border-width) solid var(--border);
  overflow-x: auto; -webkit-overflow-scrolling: touch;
}
.tab-btn {
  background: none; border: none; border-bottom: 3px solid transparent;
  color: var(--text-muted); padding: 10px 18px; font-size: 0.85em; font-weight: 400;
  white-space: nowrap; text-transform: uppercase; letter-spacing: 1px;
}
.tab-btn:hover { color: var(--text-secondary); }
.tab-btn.active { color: var(--accent-blue); border-bottom-color: var(--accent-blue); background: var(--bg-tertiary); }

/* ─── Content ─── */
#tab-content { max-width: 1100px; margin: 0 auto; padding: 20px; }
.tab-panel { display: none; }
.tab-panel.active { display: block; }

/* ─── Cards ─── */
.cards {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px; margin-bottom: 20px;
}
.card {
  background: var(--bg-secondary); border: var(--border-width) solid var(--border);
  border-radius: var(--radius); padding: 14px 16px; box-shadow: var(--shadow-inset);
}
.card-label { font-size: 0.72em; text-transform: uppercase; color: var(--text-muted); margin-bottom: 6px; letter-spacing: 1.5px; border-bottom: 1px solid var(--border-subtle); padding-bottom: 4px; }
.card-value { font-size: 1.5em; font-weight: 400; color: var(--text-primary); text-shadow: var(--glow-amber); }
.card-sub { font-size: 0.78em; color: var(--text-muted); margin-top: 4px; }
.card-value.green { color: var(--accent-green); }
.card-value.red { color: var(--accent-red); }
.card-value.blue { color: var(--accent-blue); }

/* ─── Section headers ─── */
.section-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  margin-bottom: 12px;
}
.section-title { font-size: 0.78em; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.5px; }

/* ─── Message table ─── */
.msg-controls { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
.filter-btn {
  background: var(--bg-tertiary); border: 1px solid var(--border-subtle);
  color: var(--text-muted); border-radius: var(--radius-sm); padding: 4px 10px;
  font-size: 0.78em;
}
.filter-btn.active { background: var(--accent-blue); color: #fff; border-color: var(--accent-blue); }
.search-input {
  background: var(--bg-input); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 4px 10px; color: var(--text-primary);
  font-size: 0.82em; flex: 1; min-width: 120px;
}
.search-input::placeholder { color: var(--text-dim); }
.msg-scroll { max-height: 500px; overflow-y: auto; border: var(--border-width) solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow-inset); }
.msg-table { width: 100%; border-collapse: collapse; font-size: 0.82em; }
.msg-table th {
  text-align: left; color: var(--text-muted); font-weight: 500; font-size: 0.9em;
  padding: 8px 10px; border-bottom: 1px solid var(--border);
  background: var(--bg-secondary); position: sticky; top: 0; z-index: 1;
}
.msg-table td { padding: 7px 10px; border-bottom: 1px solid var(--border-subtle); vertical-align: top; }
.msg-table tr:hover { background: var(--bg-tertiary); }
.dir-in { color: var(--accent-green); }
.dir-out { color: var(--accent-blue); }
.msg-text { max-width: 420px; word-wrap: break-word; font-family: var(--font-mono); font-size: 0.92em; }
.meta { color: var(--text-muted); font-size: 0.92em; }
.node-id { font-family: var(--font-mono); color: var(--text-primary); font-size: 0.92em; }
.empty { color: var(--text-dim); text-align: center; padding: 40px 20px; }

/* ─── Recent feed (dashboard tab) ─── */
.feed-item {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 8px 12px; border-bottom: 1px solid var(--border-subtle);
  font-size: 0.82em;
}
.feed-item:last-child { border-bottom: none; }
.feed-dir { font-size: 1.1em; flex-shrink: 0; margin-top: 1px; }
.feed-text { color: var(--text-secondary); flex: 1; font-family: var(--font-mono); font-size: 0.92em; word-break: break-word; }
.feed-meta { color: var(--text-dim); font-size: 0.85em; white-space: nowrap; flex-shrink: 0; }

/* ─── Controls ─── */
.ctrl-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
.ctrl-card {
  background: var(--bg-secondary); border: var(--border-width) solid var(--border);
  border-radius: var(--radius); padding: 16px; border-top: 3px solid var(--accent-blue);
}
.ctrl-card h3 { font-size: 0.88em; color: var(--text-primary); margin-bottom: 12px; }
.ctrl-row { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.ctrl-row:last-child { margin-bottom: 0; }
.ctrl-label { font-size: 0.8em; color: var(--text-muted); min-width: 100px; }
.ctrl-select, .ctrl-input {
  background: var(--bg-input); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 6px 10px; color: var(--text-primary);
  font-size: 0.85em; flex: 1; min-width: 0;
}
.ctrl-textarea {
  background: var(--bg-input); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 8px 10px; color: var(--text-primary);
  font-family: var(--font-mono); font-size: 0.82em; width: 100%;
  resize: vertical; min-height: 80px;
}
.ctrl-range { flex: 1; accent-color: var(--accent-blue); }
.ctrl-range-val { font-family: var(--font-mono); font-size: 0.82em; color: var(--text-primary); min-width: 50px; text-align: right; }
.btn {
  background: var(--accent-blue); color: #0a0f06; border: var(--border-width) solid var(--border);
  border-radius: var(--radius-sm); padding: 6px 14px; font-size: 0.82em; font-weight: 400;
  box-shadow: var(--shadow-raised); text-transform: uppercase; letter-spacing: 1px;
}
.btn:hover { box-shadow: var(--shadow-inset); transform: translate(1px, 1px); }
.btn:active { box-shadow: var(--shadow-inset); transform: translate(2px, 2px); }
.btn-sm { padding: 4px 10px; font-size: 0.78em; }
.btn-danger { background: var(--accent-red); }
.btn-secondary { background: var(--bg-tertiary); border: var(--border-width) solid var(--border); color: var(--text-secondary); }

/* Toggle switch */
.toggle { position: relative; display: inline-block; width: 40px; height: 22px; }
.toggle input { opacity: 0; width: 0; height: 0; }
.toggle-slider {
  position: absolute; inset: 0; background: var(--bg-tertiary); border: var(--border-width) solid var(--border);
  border-radius: 0; transition: 0.2s; cursor: pointer;
}
.toggle-slider::before {
  content: ''; position: absolute; width: 16px; height: 16px; left: 2px; bottom: 2px;
  background: var(--text-muted); border-radius: 0; transition: 0.2s;
}
.toggle input:checked + .toggle-slider { background: var(--accent-blue); border-color: var(--accent-blue); }
.toggle input:checked + .toggle-slider::before { transform: translateX(18px); background: #fff; }

/* ─── Debug ─── */
.log-controls { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; flex-wrap: wrap; }
.log-viewer {
  background: var(--bg-secondary); border: var(--border-width) solid var(--border);
  border-radius: var(--radius); padding: 10px; box-shadow: var(--shadow-inset);
  font-family: var(--font-mono); font-size: 0.75em; line-height: 1.6;
  max-height: 400px; overflow-y: auto; white-space: pre-wrap; word-break: break-all;
  text-shadow: 0 0 2px rgba(51,255,51,0.3);
}
.log-line { padding: 1px 0; }
.log-DEBUG { color: var(--accent-purple); }
.log-INFO { color: var(--text-secondary); }
.log-WARNING { color: var(--accent-yellow); }
.log-ERROR { color: var(--accent-red); }
.debug-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-top: 16px; }
.debug-card {
  background: var(--bg-secondary); border: var(--border-width) solid var(--border);
  border-radius: var(--radius); padding: 14px; box-shadow: var(--shadow-inset);
}
.debug-card h4 { font-size: 0.8em; color: var(--text-muted); text-transform: uppercase; margin-bottom: 8px; }
.debug-row { display: flex; justify-content: space-between; font-size: 0.82em; padding: 3px 0; }
.debug-row .dk { color: var(--text-muted); }
.debug-row .dv { color: var(--text-primary); font-family: var(--font-mono); }

/* ─── Guide ─── */
.guide-section {
  background: var(--bg-secondary); border: 1px solid var(--border);
  border-radius: var(--radius); margin-bottom: 12px;
}
.guide-section summary {
  padding: 12px 16px; cursor: pointer; font-weight: 600; font-size: 0.9em;
  color: var(--text-primary); list-style: none; display: flex; align-items: center; gap: 8px;
}
.guide-section summary::before { content: '▸'; color: var(--text-muted); transition: transform 0.2s; }
.guide-section[open] summary::before { transform: rotate(90deg); }
.guide-content { padding: 0 16px 14px 16px; font-size: 0.85em; line-height: 1.7; color: var(--text-secondary); }
.guide-content p { margin-bottom: 10px; }
.guide-content code {
  background: var(--bg-tertiary); padding: 1px 6px; border-radius: 3px;
  font-family: var(--font-mono); font-size: 0.92em; color: var(--accent-blue);
}
.guide-content pre {
  background: var(--bg-tertiary); border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm); padding: 10px 12px; margin: 8px 0;
  font-family: var(--font-mono); font-size: 0.88em; overflow-x: auto;
  color: var(--text-primary); line-height: 1.5;
}
.guide-content table { width: 100%; border-collapse: collapse; margin: 8px 0; }
.guide-content th, .guide-content td {
  text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border-subtle);
  font-size: 0.95em;
}
.guide-content th { color: var(--text-muted); font-weight: 500; }
.guide-content td code { font-size: 0.95em; }
.guide-step {
  display: flex; gap: 12px; margin-bottom: 14px; align-items: flex-start;
}
.step-num {
  background: var(--accent-blue); color: #0a0f06; width: 24px; height: 24px;
  border-radius: 0; display: flex; align-items: center; justify-content: center;
  font-size: 0.78em; font-weight: 400; flex-shrink: 0; margin-top: 1px;
}
.step-text { flex: 1; }
.step-text strong { color: var(--text-primary); }

/* ─── Nodes ─── */
.node-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.node-tag {
  background: var(--bg-tertiary); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm);
  padding: 2px 8px; font-size: 0.78em; font-family: var(--font-mono); color: var(--text-primary);
}

/* ─── Toast ─── */
/* ─── Chat ─── */
.chat-msg { padding: 8px 12px; font-size: 0.85em; border-bottom: 1px solid var(--border-subtle); }
.chat-msg:last-child { border-bottom: none; }
.chat-msg .chat-role { font-weight: 600; font-size: 0.8em; margin-bottom: 3px; }
.chat-msg .chat-role.you { color: var(--accent-blue); }
.chat-msg .chat-role.ai { color: var(--accent-green); }
.chat-msg .chat-text { font-family: var(--font-mono); font-size: 0.92em; color: var(--text-secondary); word-break: break-word; }
.chat-msg .chat-meta { font-size: 0.75em; color: var(--text-dim); margin-top: 3px; }
.chat-loading { color: var(--text-muted); font-style: italic; padding: 8px 12px; font-size: 0.82em; }

#toast-container { position: fixed; bottom: 20px; right: 20px; z-index: 999; display: flex; flex-direction: column; gap: 8px; }
.toast {
  background: var(--bg-secondary); border: var(--border-width) solid var(--border); border-left: 4px solid var(--accent-blue);
  border-radius: var(--radius-sm); padding: 10px 16px; font-size: 0.82em; color: var(--text-primary);
  animation: none; min-width: 200px; max-width: 360px; box-shadow: 3px 3px 0 #0a0f06;
}
.toast-success { border-left-color: var(--accent-green); }
.toast-error { border-left-color: var(--accent-red); }
.toast.fade-out { opacity: 0; transition: opacity 0.3s; }
@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

/* ─── Addon stat cards ─── */
.stat-card {
  background: var(--bg-secondary); border: var(--border-width) solid var(--border);
  padding: 14px 16px; box-shadow: var(--shadow-inset);
}
.stat-label {
  font-size: 0.72em; text-transform: uppercase; color: var(--text-muted);
  letter-spacing: 1.5px; margin-bottom: 6px;
  border-bottom: 1px solid var(--border-subtle); padding-bottom: 4px;
}
.stat-value { font-size: 1.5em; color: var(--text-primary); text-shadow: var(--glow-amber); }

/* ─── Responsive ─── */
@media (max-width: 768px) {
  #tab-content { padding: 12px; }
  .cards { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
  .ctrl-grid { grid-template-columns: 1fr; }
  .msg-text { max-width: 200px; }
  .debug-grid { grid-template-columns: 1fr; }
}
@media (max-width: 480px) {
  #top-bar { padding: 8px 12px; }
  .top-badges { margin-left: 0; }
  .badge { font-size: 0.72em; }
}
</style>
</head>
<body>

<!-- ═══ Top Bar ═══ -->
<header id="top-bar">
  <div class="logo">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#ffcc00" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M12 2L12 8"/><path d="M12 16L12 22"/>
      <circle cx="12" cy="12" r="3"/>
      <path d="M4.93 4.93l4.24 4.24"/><path d="M14.83 14.83l4.24 4.24"/>
      <path d="M19.07 4.93l-4.24 4.24"/><path d="M9.17 14.83l-4.24 4.24"/>
    </svg>
    LORACLE BRIDGE
  </div>
  <div class="top-badges">
    <div class="badge">
      <span class="conn-dot off" id="hdr-dot"></span>
      <span class="val" id="hdr-status">Disconnected</span>
    </div>
    <div class="badge"><span class="label">Up</span><span class="val" id="hdr-uptime">--</span></div>
    <div class="badge"><span class="label">Msgs</span><span class="val" id="hdr-msgs">0</span></div>
    <div class="badge"><span class="label">Nodes</span><span class="val" id="hdr-nodes">0</span></div>
    <div class="badge" id="hdr-rag-badge" style="display:none"><span class="val" style="color:var(--accent-green)">RAG</span></div>
  </div>
</header>

<!-- ═══ Tab Navigation ═══ -->
<nav id="tab-nav">
  <button class="tab-btn active" data-tab="dashboard">Inference</button>
  <button class="tab-btn" data-tab="messages">Messages</button>
  <button class="tab-btn" data-tab="coverage">Coverage</button>
  <button class="tab-btn" data-tab="controls">Controls</button>
  <button class="tab-btn" data-tab="debug">Debug</button>
  <button class="tab-btn" data-tab="guide">Guide</button>
</nav>

<!-- ═══ Tab Content ═══ -->
<main id="tab-content">

  <!-- ──── Inference Tab ──── -->
  <section id="tab-dashboard" class="tab-panel active">

    <!-- Status Cards -->
    <div class="cards">
      <div class="card">
        <div class="card-label">Connection</div>
        <div class="card-value green" id="dash-conn">--</div>
        <div class="card-sub" id="dash-conn-type">--</div>
      </div>
      <div class="card">
        <div class="card-label">Model</div>
        <div class="card-value blue" id="dash-model" style="font-size:1.1em;word-break:break-all">--</div>
      </div>
      <div class="card">
        <div class="card-label">Messages</div>
        <div class="card-value" id="dash-msgs">0</div>
        <div class="card-sub" id="dash-msgs-sub"></div>
      </div>
      <div class="card">
        <div class="card-label">Nodes</div>
        <div class="card-value" id="dash-nodes">0</div>
      </div>
      <div class="card">
        <div class="card-label">Avg Response</div>
        <div class="card-value" id="dash-avg-time">--</div>
        <div class="card-sub" id="dash-avg-chunks"></div>
      </div>
      <div class="card" id="dash-rag-card" style="display:none">
        <div class="card-label">Knowledge Base</div>
        <div class="card-value green" id="dash-rag">ON</div>
      </div>
    </div>

    <!-- Model & Prompt Controls -->
    <div class="ctrl-grid" style="margin-bottom:16px">
      <div style="background:var(--bg-secondary);border:var(--border-width) solid var(--border);padding:14px;border-top:3px solid var(--accent-blue)">
        <div style="font-size:0.82em;color:var(--text-muted);letter-spacing:1px;margin-bottom:8px">Model</div>
        <div style="display:flex;gap:8px;margin-bottom:8px">
          <select class="ctrl-select" id="inf-model-select"><option>Loading...</option></select>
          <button class="btn btn-sm" onclick="infRefreshModels()">&#x21BB;</button>
        </div>
        <button class="btn" onclick="infSwitchModel()" style="width:100%">Apply</button>
        <div class="card-sub" style="margin-top:6px">Current: <span id="inf-current-model">--</span></div>
      </div>

      <div style="background:var(--bg-secondary);border:var(--border-width) solid var(--border);padding:14px;border-top:3px solid var(--accent-blue)">
        <div style="font-size:0.82em;color:var(--text-muted);letter-spacing:1px;margin-bottom:8px">System Prompt</div>
        <textarea class="ctrl-textarea" id="inf-prompt" rows="3" placeholder="Loading..."></textarea>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px">
          <span class="card-sub" id="inf-prompt-count">0 chars</span>
          <button class="btn btn-sm" onclick="infSavePrompt()">Save</button>
        </div>
      </div>
    </div>

    <!-- Knowledge Base / RAG -->
    <div id="inf-rag-section" style="background:var(--bg-secondary);border:var(--border-width) solid var(--border);padding:14px;margin-bottom:16px;border-top:3px solid var(--accent-blue);display:none">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <span style="font-size:0.82em;color:var(--text-muted);letter-spacing:1px">Knowledge Base</span>
        <div style="display:flex;align-items:center;gap:10px">
          <span class="card-sub" id="inf-rag-stats">--</span>
          <label class="toggle">
            <input type="checkbox" id="inf-rag-toggle" onchange="infToggleRag(this.checked)">
            <span class="toggle-slider"></span>
          </label>
        </div>
      </div>
      <div style="display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap">
        <input type="file" id="inf-file-upload" accept=".pdf,.txt,.md,.zim"
          style="font-size:0.82em;color:var(--text-secondary);flex:1;min-width:150px">
        <button class="btn btn-sm" onclick="infUploadFile()">Upload</button>
      </div>
      <div style="display:flex;gap:8px;margin-bottom:8px">
        <input type="url" id="inf-url-input" placeholder="https://example.com/article"
          style="flex:1;min-width:0;background:var(--bg-input);border:1px solid var(--border);color:var(--text-primary);padding:6px 10px;font-size:0.85em">
        <button class="btn btn-sm" onclick="infIngestUrl()">Add URL</button>
      </div>
      <div id="inf-rag-docs" style="font-size:0.82em;max-height:200px;overflow-y:auto"></div>
    </div>

    <!-- Known Nodes -->
    <div id="dash-nodes-section" style="display:none;margin-bottom:16px">
      <div class="section-title" style="margin-bottom:8px">Known Nodes</div>
      <div class="node-tags" id="dash-node-tags"></div>
    </div>

    <!-- LLM Activity Log -->
    <div class="section-head">
      <div class="section-title">LLM Activity</div>
    </div>
    <div style="background:var(--bg-secondary);border:var(--border-width) solid var(--border);box-shadow:var(--shadow-inset)">
      <div id="dash-feed" class="empty">Waiting for messages...</div>
    </div>

    <!-- Direct Chat -->
    <div style="margin-top:20px">
      <div class="section-head">
        <div class="section-title">Direct Chat (bypasses mesh)</div>
        <button class="btn btn-sm btn-secondary" onclick="clearChat()">Clear</button>
      </div>
      <div style="background:var(--bg-secondary);border:var(--border-width) solid var(--border);padding:0">
        <div id="chat-history" style="max-height:300px;overflow-y:auto;padding:12px;min-height:60px">
          <div class="empty" style="padding:20px">Test the LLM directly — does not transmit over radio</div>
        </div>
        <div style="display:flex;gap:8px;padding:10px;border-top:1px solid var(--border-subtle)">
          <input class="search-input" type="text" id="chat-input" placeholder="Ask the AI something..." style="flex:1;min-width:0" onkeydown="if(event.key==='Enter')sendChat()">
          <button class="btn" id="chat-send-btn" onclick="sendChat()">Send</button>
        </div>
      </div>
    </div>
  </section>

  <!-- ──── Messages Tab ──── -->
  <section id="tab-messages" class="tab-panel">

    <!-- ── GPS Map ── -->
    <div style="margin-bottom:16px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <span style="font-size:0.82em;color:var(--text-muted);letter-spacing:1px">Node Map</span>
        <span id="map-node-count" style="font-size:0.78em;color:var(--text-dim)">0 nodes with GPS</span>
      </div>
      <div id="mesh-map" style="height:280px;border:var(--border-width) solid var(--border);background:var(--bg-secondary);box-shadow:var(--shadow-inset)"></div>
    </div>

    <!-- ── Send Message ── -->
    <div style="margin-bottom:16px;padding:12px;background:var(--bg-secondary);border:var(--border-width) solid var(--border);border-top:3px solid var(--accent-blue)">
      <div style="font-size:0.82em;color:var(--text-muted);letter-spacing:1px;margin-bottom:8px">Send Message</div>
      <div style="display:flex;gap:8px;margin-bottom:8px">
        <select class="ctrl-select" id="msg-send-to" style="width:140px;flex-shrink:0">
          <option value="">Broadcast</option>
        </select>
        <select class="ctrl-select" id="msg-send-ch" style="width:70px;flex-shrink:0">
          <option value="0">Ch 0</option>
          <option value="1">Ch 1</option>
          <option value="2">Ch 2</option>
          <option value="3">Ch 3</option>
        </select>
      </div>
      <div style="display:flex;gap:8px">
        <input type="text" id="msg-send-text" placeholder="Type a message..."
          style="flex:1;min-width:0;background:var(--bg-input);border:1px solid var(--border);color:var(--text-primary);padding:8px 10px;font-size:0.9em"
          onkeydown="if(event.key==='Enter')sendMeshMsg()">
        <button class="btn" onclick="sendMeshMsg()">Send</button>
      </div>
      <div id="msg-send-status" style="font-size:0.78em;color:var(--text-dim);margin-top:4px"></div>
    </div>

    <!-- ── Message Log ── -->
    <div class="msg-controls">
      <button class="filter-btn active" data-filter="all" onclick="setMsgFilter('all')">All</button>
      <button class="filter-btn" data-filter="in" onclick="setMsgFilter('in')">&#x2B07; In</button>
      <button class="filter-btn" data-filter="out" onclick="setMsgFilter('out')">&#x2B06; Out</button>
      <input class="search-input" type="text" placeholder="Search messages..." id="msg-search" oninput="App.messageSearch=this.value">
      <label style="font-size:0.78em;color:var(--text-muted);display:flex;align-items:center;gap:4px">
        <input type="checkbox" id="msg-autoscroll" checked onchange="App.autoScrollMessages=this.checked"> Auto-scroll
      </label>
    </div>
    <div class="msg-scroll" id="msg-scroll">
      <table class="msg-table">
        <thead>
          <tr><th>Time</th><th></th><th>Node</th><th>Message</th><th>Chunks</th><th>LLM Time</th></tr>
        </thead>
        <tbody id="msg-body">
          <tr><td colspan="6" class="empty">Waiting for messages...</td></tr>
        </tbody>
      </table>
    </div>
  </section>

  <!-- ──── Coverage Tab ──── -->
  <section id="tab-coverage" class="tab-panel">
    <div style="margin-bottom:12px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
      <span style="font-size:0.82em;color:var(--text-muted);letter-spacing:1px">Mesh Coverage Heatmap</span>
      <span id="cov-stats" style="font-size:0.78em;color:var(--text-dim)">No samples yet</span>
    </div>

    <div style="margin-bottom:10px;padding:10px;background:var(--bg-secondary);border:var(--border-width) solid var(--border);display:flex;gap:14px;flex-wrap:wrap;align-items:center;font-size:0.82em">
      <label style="display:flex;align-items:center;gap:6px;color:var(--text-muted)">
        Mode:
        <select class="ctrl-select" id="cov-mode" onchange="renderCoverage()" style="width:100px">
          <option value="grid" selected>Grid</option>
          <option value="heat">Heatmap</option>
          <option value="both">Both</option>
        </select>
      </label>
      <label style="display:flex;align-items:center;gap:6px;color:var(--text-muted)">
        Window:
        <select class="ctrl-select" id="cov-window" onchange="renderCoverage()" style="width:120px">
          <option value="3600">Last hour</option>
          <option value="21600">Last 6 hours</option>
          <option value="86400" selected>Last 24 hours</option>
          <option value="0">All time</option>
        </select>
      </label>
      <label style="display:flex;align-items:center;gap:6px;color:var(--text-muted)">
        Min RSSI:
        <input type="range" id="cov-rssi" min="-130" max="-30" value="-130" oninput="document.getElementById('cov-rssi-val').textContent=this.value+' dBm';renderCoverage()">
        <span id="cov-rssi-val" style="font-family:inherit;color:var(--text-dim)">-130 dBm</span>
      </label>
      <label style="display:flex;align-items:center;gap:6px;color:var(--text-muted)">
        <input type="checkbox" id="cov-deadzones" onchange="renderCoverage()"> Dead zones
      </label>
      <button class="btn btn-sm" onclick="loadCoverage()">Refresh</button>
    </div>

    <div id="cov-map" style="position:relative;height:520px;border:var(--border-width) solid var(--border);background:var(--bg-secondary);box-shadow:var(--shadow-inset)">
      <div id="cov-legend" style="position:absolute;bottom:10px;right:10px;z-index:500;
           background:rgba(10,15,6,0.92);border:1px solid var(--border);padding:8px 10px;
           font-family:'Share Tech Mono',monospace;font-size:0.72em;color:var(--text-muted);
           box-shadow:0 0 10px rgba(0,255,65,0.2);pointer-events:none">
        <div style="color:var(--accent-green);letter-spacing:1px;margin-bottom:5px">SIGNAL</div>
        <div style="display:flex;align-items:center;gap:6px;margin:2px 0"><span style="width:16px;height:10px;background:#00ff41;display:inline-block;border:1px solid #0a0f06"></span> Strong  &ge;-60 dBm</div>
        <div style="display:flex;align-items:center;gap:6px;margin:2px 0"><span style="width:16px;height:10px;background:#8aff00;display:inline-block;border:1px solid #0a0f06"></span> Good    -60..-80</div>
        <div style="display:flex;align-items:center;gap:6px;margin:2px 0"><span style="width:16px;height:10px;background:#ffd500;display:inline-block;border:1px solid #0a0f06"></span> OK      -80..-95</div>
        <div style="display:flex;align-items:center;gap:6px;margin:2px 0"><span style="width:16px;height:10px;background:#ff8a00;display:inline-block;border:1px solid #0a0f06"></span> Weak    -95..-110</div>
        <div style="display:flex;align-items:center;gap:6px;margin:2px 0"><span style="width:16px;height:10px;background:#ff3030;display:inline-block;border:1px solid #0a0f06"></span> Faint   &lt;-110</div>
        <div style="display:flex;align-items:center;gap:6px;margin:4px 0 0 0;border-top:1px solid var(--border);padding-top:4px"><span style="width:16px;height:10px;background:#ff0033;display:inline-block;border:2px solid #ff3030;opacity:0.7"></span> Dead zone</div>
      </div>
    </div>

    <div style="margin-top:8px;font-size:0.74em;color:var(--text-dim);line-height:1.5">
      Each sample is a (node, GPS, RSSI, SNR) tuple recorded when the bridge sees a packet from
      a node with a known position. Hot spots = strong signal. Holes inside the traveled area = dead zones.
      Samples are throttled to ~1 every 5 s / 10 m per node.
    </div>
  </section>

  <!-- ──── Controls Tab ──── -->
  <section id="tab-controls" class="tab-panel">
    <div class="ctrl-grid">

      <div class="ctrl-card" id="ctrl-connection-card">
        <h3>Connection</h3>

        <!-- Status bar: dot + status + detail + disconnect — all one row -->
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
          <span class="conn-dot off" id="conn-mgr-dot"></span>
          <span id="conn-mgr-status" style="font-size:0.95em">Disconnected</span>
          <span id="conn-mgr-detail" class="card-sub" style="margin-left:auto"></span>
          <button class="btn btn-sm" id="conn-disconnect-btn" onclick="disconnectRadio()"
            style="display:none;background:transparent;border-color:var(--accent-red);color:var(--accent-red)">Disconnect</button>
        </div>

        <!-- BLE Scanner -->
        <div style="border-top:1px solid var(--border);padding-top:12px">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
            <label style="font-size:0.82em;color:var(--text-muted);letter-spacing:1px">Bluetooth</label>
            <button class="btn btn-sm" id="ble-scan-btn" onclick="bleScan()">Scan</button>
          </div>
          <!-- Last connected quick-reconnect -->
          <div id="ble-last-device" style="display:none;margin-bottom:10px;padding:6px 10px;background:var(--bg-input);border:1px solid var(--border)">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
              <span style="font-size:0.82em;color:var(--text-muted);white-space:nowrap">Last:</span>
              <span id="ble-last-name" style="font-size:0.82em;flex:1;overflow:hidden;text-overflow:ellipsis"></span>
              <button class="btn btn-sm" onclick="bleQuickConnect()">Reconnect</button>
            </div>
          </div>
          <!-- Scan results -->
          <div id="ble-scan-status" style="font-size:0.82em;color:var(--text-muted);margin-bottom:6px"></div>
          <div id="ble-device-list"></div>
          <div id="ble-unavailable" style="display:none;font-size:0.82em;color:var(--accent-orange);margin-top:6px">
            BLE not available — requires Python 3.11+ with bleak.
          </div>
        </div>

        <!-- Manual Connection — two-row layout to prevent overflow -->
        <div style="border-top:1px solid var(--border);padding-top:12px;margin-top:12px">
          <label style="font-size:0.82em;color:var(--text-muted);letter-spacing:1px">Manual</label>
          <div style="display:flex;gap:8px;margin-top:6px">
            <select class="ctrl-select" id="conn-type-select" onchange="connTypeChanged()" style="width:90px;flex-shrink:0">
              <option value="serial">Serial</option>
              <option value="tcp">TCP</option>
              <option value="ble">BLE</option>
            </select>
            <input type="text" id="conn-address-input" placeholder="auto-detect"
              style="flex:1;min-width:0;background:var(--bg-input);border:1px solid var(--border);color:var(--text-primary);padding:6px 10px;font-size:0.85em">
          </div>
          <button class="btn" onclick="manualConnect()" style="width:100%;margin-top:8px">Connect</button>
        </div>
      </div>

      <div class="ctrl-card">
        <h3>Model</h3>
        <div class="ctrl-row">
          <select class="ctrl-select" id="ctrl-model-select"><option>Loading...</option></select>
          <button class="btn btn-sm" onclick="refreshModels()">&#x21BB;</button>
        </div>
        <button class="btn" onclick="switchModel()" style="width:100%">Apply</button>
        <div class="card-sub" style="margin-top:6px">Current: <span id="ctrl-current-model">--</span></div>
      </div>

      <div class="ctrl-card">
        <h3>System Prompt</h3>
        <textarea class="ctrl-textarea" id="ctrl-prompt" rows="4" placeholder="Loading..."></textarea>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px">
          <span class="card-sub" id="ctrl-prompt-count">0 chars</span>
          <button class="btn" onclick="savePrompt()">Save</button>
        </div>
      </div>

      <div class="ctrl-card">
        <h3>Response Settings</h3>
        <div class="ctrl-row">
          <span class="ctrl-label">Max Length</span>
          <input class="ctrl-range" type="range" id="ctrl-max-len" min="100" max="2000" step="50" value="500" oninput="document.getElementById('ctrl-max-len-val').textContent=this.value">
          <span class="ctrl-range-val" id="ctrl-max-len-val">500</span>
        </div>
        <div class="ctrl-row">
          <span class="ctrl-label">Compression</span>
          <label class="toggle">
            <input type="checkbox" id="ctrl-compression" checked>
            <span class="toggle-slider"></span>
          </label>
        </div>
        <div style="margin-top:8px;text-align:right">
          <button class="btn" onclick="applySettings()">Apply Settings</button>
        </div>
      </div>

      <div class="ctrl-card" id="ctrl-rag-card" style="display:none">
        <h3>Knowledge Base (RAG)</h3>
        <div class="ctrl-row">
          <span class="ctrl-label">Enabled</span>
          <label class="toggle">
            <input type="checkbox" id="ctrl-rag-toggle" onchange="toggleRag(this.checked)">
            <span class="toggle-slider"></span>
          </label>
        </div>
        <div id="ctrl-rag-stats" class="card-sub" style="margin-top:8px"></div>
        <div style="margin-top:12px;border-top:1px solid var(--border);padding-top:10px">
          <label style="font-size:0.82em;color:var(--text-muted)">Add web page to knowledge base</label>
          <div style="display:flex;gap:8px;margin-top:4px">
            <input type="url" id="ctrl-rag-url" placeholder="https://example.com/article"
                   style="flex:1;background:var(--bg-secondary);border:1px solid var(--border);color:var(--text-primary);padding:6px 10px;border-radius:6px;font-size:0.85em">
            <button onclick="ingestUrl()" class="btn btn-primary" style="white-space:nowrap">Add URL</button>
          </div>
          <div id="ctrl-rag-url-status" style="margin-top:6px;font-size:0.82em"></div>
        </div>
        <div style="margin-top:12px;border-top:1px solid var(--border);padding-top:10px">
          <label style="font-size:0.82em;color:var(--text-muted)">Ingested documents</label>
          <div id="ctrl-rag-docs" style="margin-top:6px;font-size:0.82em;max-height:300px;overflow-y:auto"></div>
        </div>
      </div>

      <div class="ctrl-card" style="border-color:var(--accent-red)">
        <h3 style="color:var(--accent-red)">Danger Zone</h3>
        <p style="font-size:0.82em;color:var(--text-muted);margin-bottom:10px">These actions cannot be undone.</p>
        <button class="btn btn-danger" onclick="clearHistory()">Clear All Conversation History</button>
      </div>
    </div>
  </section>

  <!-- ──── Debug Tab ──── -->
  <section id="tab-debug" class="tab-panel">
    <div class="section-head">
      <div class="section-title">Live Logs</div>
      <div class="log-controls">
        <button class="filter-btn active" data-level="all" onclick="setLogFilter('all',this)">All</button>
        <button class="filter-btn" data-level="DEBUG" onclick="setLogFilter('DEBUG',this)">Debug</button>
        <button class="filter-btn" data-level="INFO" onclick="setLogFilter('INFO',this)">Info</button>
        <button class="filter-btn" data-level="WARNING" onclick="setLogFilter('WARNING',this)">Warn</button>
        <button class="filter-btn" data-level="ERROR" onclick="setLogFilter('ERROR',this)">Error</button>
        <label style="font-size:0.78em;color:var(--text-muted);display:flex;align-items:center;gap:4px">
          <input type="checkbox" id="log-autoscroll" checked> Auto-scroll
        </label>
      </div>
    </div>
    <div class="log-viewer" id="log-viewer">Waiting for logs...</div>

    <div class="debug-grid">
      <div class="debug-card">
        <h4>Connection</h4>
        <div class="debug-row"><span class="dk">Type</span><span class="dv" id="dbg-conn-type">--</span></div>
        <div class="debug-row"><span class="dk">Ollama URL</span><span class="dv" id="dbg-ollama-url">--</span></div>
        <div class="debug-row"><span class="dk">Model</span><span class="dv" id="dbg-model">--</span></div>
        <div class="debug-row"><span class="dk">Firmware</span><span class="dv" id="dbg-firmware">--</span></div>
        <div class="debug-row"><span class="dk">Library</span><span class="dv" id="dbg-library">--</span></div>
        <div class="debug-row"><span class="dk">Hardware</span><span class="dv" id="dbg-hw-model">--</span></div>
      </div>
      <div class="debug-card">
        <h4>Performance</h4>
        <div class="debug-row"><span class="dk">Avg LLM Time</span><span class="dv" id="dbg-avg-time">--</span></div>
        <div class="debug-row"><span class="dk">Avg Chunks</span><span class="dv" id="dbg-avg-chunks">--</span></div>
        <div class="debug-row"><span class="dk">Total LLM Calls</span><span class="dv" id="dbg-total-calls">0</span></div>
      </div>
      <div class="debug-card">
        <h4>Internals</h4>
        <div class="debug-row"><span class="dk">Threads</span><span class="dv" id="dbg-threads">--</span></div>
        <div class="debug-row"><span class="dk">Queue Depth</span><span class="dv" id="dbg-queue">--</span></div>
        <div class="debug-row"><span class="dk">Dedup Cache</span><span class="dv" id="dbg-dedup">--</span></div>
      </div>
    </div>
  </section>

  <!-- ──── Guide Tab ──── -->
  <section id="tab-guide" class="tab-panel">

    <details class="guide-section" open>
      <summary>Quick Start</summary>
      <div class="guide-content">
        <div class="guide-step">
          <div class="step-num">1</div>
          <div class="step-text"><strong>Plug in your Meshtastic radio</strong> via USB-C. The bridge auto-detects it.</div>
        </div>
        <div class="guide-step">
          <div class="step-num">2</div>
          <div class="step-text"><strong>Run the bridge</strong> with a single command:
            <pre>./mesh-llm.sh</pre>
            This installs everything automatically (Python, Ollama, AI model, dependencies).
          </div>
        </div>
        <div class="guide-step">
          <div class="step-num">3</div>
          <div class="step-text"><strong>Open this dashboard</strong> at <code>http://localhost:8000</code> to monitor and control the bridge.</div>
        </div>
        <div class="guide-step">
          <div class="step-num">4</div>
          <div class="step-text"><strong>Send a message</strong> from any radio on the mesh. The bridge will respond automatically with an AI-generated answer.</div>
        </div>
        <div class="guide-step">
          <div class="step-num">5</div>
          <div class="step-text"><strong>Add documents</strong> for smarter answers: put PDFs or text files in the <code>CONTEXT FILES/</code> folder and run with <code>--rag</code>:
            <pre>./mesh-llm.sh --rag</pre>
          </div>
        </div>
      </div>
    </details>

    <details class="guide-section">
      <summary>Connection Methods</summary>
      <div class="guide-content">
        <p><strong>USB Serial (Default)</strong> &mdash; Plug in the radio via USB-C. Simplest and most reliable. The bridge auto-detects the port.</p>
        <pre>./mesh-llm.sh                              # Auto-detect
./mesh-llm.sh --serial /dev/cu.usbserial-0001  # Specific port</pre>
        <p><strong>TCP (Network)</strong> &mdash; Connect over WiFi. Great when the radio is remote or you want to use the web client simultaneously. The radio can create its own WiFi hotspot (no internet needed).</p>
        <pre>./mesh-llm.sh --tcp                        # Default: 192.168.1.1:4403
./mesh-llm.sh --tcp 192.168.1.100:4403     # Custom IP</pre>
        <p><strong>Bluetooth LE</strong> &mdash; Wireless, no network needed. Only one BLE client can connect at a time. Requires Python 3.11+ (auto-installed).</p>
        <pre>./mesh-llm.sh --ble                        # Scan for radios
./mesh-llm.sh --ble "AA:BB:CC:DD:EE:FF"   # Specific address</pre>
      </div>
    </details>

    <details class="guide-section">
      <summary>Mesh Commands</summary>
      <div class="guide-content">
        <p>Anyone on the mesh can send these commands (prefix with <code>!</code>):</p>
        <table>
          <tr><th>Command</th><th>What It Does</th></tr>
          <tr><td><code>!help</code></td><td>Show available commands</td></tr>
          <tr><td><code>!status</code></td><td>Bridge info: model, uptime, nodes, message count</td></tr>
          <tr><td><code>!model &lt;name&gt;</code></td><td>Switch the AI model</td></tr>
          <tr><td><code>!models</code></td><td>List installed models</td></tr>
          <tr><td><code>!clear</code></td><td>Reset your conversation history</td></tr>
          <tr><td><code>!ping</code></td><td>Connectivity test</td></tr>
          <tr><td><code>!rag on/off</code></td><td>Toggle knowledge base for your node</td></tr>
          <tr><td><code>!docs</code></td><td>List ingested documents</td></tr>
        </table>
      </div>
    </details>

    <details class="guide-section">
      <summary>Using the Controls</summary>
      <div class="guide-content">
        <p><strong>Model</strong> &mdash; Switch between installed Ollama models. Smaller models (e.g., <code>llama3.2:1b</code>) respond faster. Click "Refresh" to rescan.</p>
        <p><strong>System Prompt</strong> &mdash; Customize the AI's personality and behavior. For example: <em>"You are a wilderness survival expert. Be concise."</em></p>
        <p><strong>Max Response Length</strong> &mdash; Limits how many characters the AI can return. Lower = fewer chunks over LoRa = faster delivery. Default: 500.</p>
        <p><strong>Pager (!more)</strong> &mdash; Long responses are automatically truncated to fit one LoRa message. Send <code>!more</code> to get the next page.</p>
        <p><strong>Compression</strong> &mdash; Zlib compression on chunks. Reduces chunk count when possible. Leave on unless debugging.</p>
        <p><strong>Knowledge Base</strong> &mdash; Toggle RAG on/off (only available if started with <code>--rag</code>).</p>
      </div>
    </details>

    <details class="guide-section">
      <summary>Troubleshooting</summary>
      <div class="guide-content">
        <p><strong>"No Meshtastic device found"</strong> &mdash; Make sure the radio is plugged in via USB-C and powered on. Try a different cable (some are charge-only). Run <code>ls /dev/cu.usb*</code> to check.</p>
        <p><strong>"Ollama not responding"</strong> &mdash; Run <code>ollama serve</code> in a separate terminal. Check: <code>curl http://localhost:11434/api/tags</code></p>
        <p><strong>Responses are slow</strong> &mdash; Use a smaller model: <code>./mesh-llm.sh --model llama3.2:1b</code>. Or reduce max length in Controls.</p>
        <p><strong>Response was cut off</strong> &mdash; Send <code>!more</code> to get the next page of a long response.</p>
        <p><strong>Dashboard not loading</strong> &mdash; Default port is 8000. If in use: <code>./mesh-llm.sh --dashboard-port 9000</code></p>
        <p><strong>BLE issues</strong> &mdash; Requires Python 3.11+. Try: <code>rm -rf venv && ./mesh-llm.sh --ble</code> to rebuild.</p>
      </div>
    </details>

    <details class="guide-section">
      <summary>How It Works</summary>
      <div class="guide-content">
<pre>Someone on the mesh types a question
  &#x2192; Their radio sends it over LoRa
  &#x2192; Your radio receives it via USB/TCP/BLE
  &#x2192; This bridge forwards it to Ollama (local AI)
  &#x2192; (Optional) RAG searches your documents for context
  &#x2192; AI generates a response
  &#x2192; Response is chunked (8-byte header + 220-byte payload)
  &#x2192; Chunks sent back over the mesh (with configurable delay)
  &#x2192; They get the answer</pre>
        <p>Everything runs locally. No internet, no cloud, no API keys.</p>
      </div>
    </details>

  </section>

</main>

<!-- ═══ Toast container ═══ -->
<div id="toast-container"></div>

<script>
// ─── App State ──────────────────────────────────────────────────────────────

const App = {
  currentTab: 'dashboard',
  state: {},
  logs: [],
  autoScrollMessages: true,
  autoScrollLogs: true,
  messageFilter: 'all',
  messageSearch: '',
  logFilter: 'all',
  controlsLoaded: false,
  debugLoaded: false,
};

// ─── Utilities ──────────────────────────────────────────────────────────────

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function formatUptime(s) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h > 0 ? h+'h '+m+'m' : m > 0 ? m+'m '+sec+'s' : sec+'s';
}

function formatTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString();
}

function relativeTime(ts) {
  const diff = Math.floor(Date.now()/1000 - ts);
  if (diff < 10) return 'just now';
  if (diff < 60) return diff + 's ago';
  if (diff < 3600) return Math.floor(diff/60) + 'm ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  return Math.floor(diff/86400) + 'd ago';
}

function showToast(message, type) {
  type = type || 'info';
  const c = document.getElementById('toast-container');
  const t = document.createElement('div');
  t.className = 'toast toast-' + type;
  t.textContent = message;
  c.appendChild(t);
  setTimeout(function() { t.classList.add('fade-out'); }, 2700);
  setTimeout(function() { if (t.parentNode) c.removeChild(t); }, 3100);
}

async function callApi(method, url, body) {
  try {
    const opts = { method: method, headers: {'Content-Type': 'application/json'} };
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(url, opts);
    const data = await r.json();
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

// ─── Tab Switching ──────────────────────────────────────────────────────────

function switchTab(name) {
  document.querySelectorAll('.tab-panel').forEach(function(p) { p.classList.remove('active'); });
  document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); });
  document.getElementById('tab-' + name).classList.add('active');
  document.querySelector('[data-tab="' + name + '"]').classList.add('active');
  App.currentTab = name;
  if (name === 'controls' && !App.controlsLoaded) loadControlsData();
  if (name === 'messages') { setTimeout(function() { initMap(); if (_meshMap) _meshMap.invalidateSize(); }, 100); }
  if (name === 'coverage') { setTimeout(function() { initCovMap(); if (_covMap) _covMap.invalidateSize(); loadCoverage(); }, 100); }
  if (name === 'debug' && !App.debugLoaded) { App.debugLoaded = true; loadDebugData(); }
}

document.querySelectorAll('.tab-btn').forEach(function(btn) {
  btn.addEventListener('click', function() { switchTab(btn.dataset.tab); });
});

// ─── Poll Loop ──────────────────────────────────────────────────────────────

async function poll() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    App.state = d;

    // Top bar
    const dot = document.getElementById('hdr-dot');
    const st = document.getElementById('hdr-status');
    if (d.connected) { dot.className = 'conn-dot on'; st.textContent = 'Connected'; }
    else { dot.className = 'conn-dot off'; st.textContent = 'Disconnected'; }
    document.getElementById('hdr-uptime').textContent = formatUptime(d.uptime);
    document.getElementById('hdr-msgs').textContent = d.message_count;
    document.getElementById('hdr-nodes').textContent = d.node_count;
    if (d.rag_enabled) document.getElementById('hdr-rag-badge').style.display = '';

    // Connection card (always update)
    updateConnectionCard(d);

    // Inference/Dashboard tab
    if (App.currentTab === 'dashboard') {
      if (!_infLoaded) loadInferenceData();
      updateDashboard(d);
    }

    // Messages tab
    if (App.currentTab === 'messages') {
      updateMessages(d);
      updateMap(d.node_positions);
      updateSendDropdown(d.known_nodes);
    }

    // Debug: fetch logs
    if (App.currentTab === 'debug') {
      try {
        const lr = await fetch('/api/logs?limit=300');
        const ld = await lr.json();
        App.logs = ld.logs;
        updateDebugLogs();
        updateDebugInfo(d);
      } catch(e) {}
    }
  } catch(e) { /* silent retry */ }
}

// ─── Inference Tab ──────────────────────────────────────────────────────────

var _infLoaded = false;

async function loadInferenceData() {
  if (_infLoaded) return;
  _infLoaded = true;
  // Models
  await infRefreshModels();
  // System prompt
  var pd = await callApi('GET', '/api/system-prompt');
  if (pd) {
    document.getElementById('inf-prompt').value = pd.prompt;
    document.getElementById('inf-prompt-count').textContent = pd.prompt.length + ' chars';
  }
  document.getElementById('inf-prompt').addEventListener('input', function() {
    document.getElementById('inf-prompt-count').textContent = this.value.length + ' chars';
  });
  // RAG docs
  infLoadRagDocs();
}

async function infRefreshModels() {
  var d = await callApi('GET', '/api/models');
  if (!d) return;
  var sel = document.getElementById('inf-model-select');
  sel.innerHTML = d.models.map(function(m) {
    return '<option' + (m === d.current ? ' selected' : '') + '>' + escapeHtml(m) + '</option>';
  }).join('');
  document.getElementById('inf-current-model').textContent = d.current;
}

async function infSwitchModel() {
  var model = document.getElementById('inf-model-select').value;
  var d = await callApi('POST', '/api/model', {model: model});
  if (d && d.ok) { showToast('Model: ' + d.model); infRefreshModels(); }
}

async function infSavePrompt() {
  var prompt = document.getElementById('inf-prompt').value;
  var d = await callApi('POST', '/api/system-prompt', {prompt: prompt});
  if (d && d.ok) showToast('Prompt saved');
}

async function infToggleRag(enabled) {
  await callApi('POST', '/api/rag/toggle', {enabled: enabled});
}

async function infUploadFile() {
  var input = document.getElementById('inf-file-upload');
  if (!input.files.length) return;
  var formData = new FormData();
  formData.append('file', input.files[0]);
  try {
    var res = await fetch('/api/rag/ingest-file', {method: 'POST', body: formData});
    var data = await res.json();
    if (data.ok) {
      showToast('Uploaded: ' + data.filename + ' (' + data.chunks + ' chunks)');
      input.value = '';
      infLoadRagDocs();
    } else {
      showToast(data.error || 'Upload failed', 'error');
    }
  } catch(e) { showToast('Upload error', 'error'); }
}

async function infIngestUrl() {
  var url = document.getElementById('inf-url-input').value.trim();
  if (!url) return;
  var d = await callApi('POST', '/api/rag/ingest-url', {url: url});
  if (d && d.ok) {
    showToast('Ingested: ' + (d.filename || url));
    document.getElementById('inf-url-input').value = '';
    infLoadRagDocs();
  }
}

async function infLoadRagDocs() {
  var d = await callApi('GET', '/api/rag/stats');
  if (!d || !d.available) return;
  document.getElementById('inf-rag-section').style.display = '';
  document.getElementById('inf-rag-toggle').checked = App.state ? App.state.rag_enabled : true;
  var stats = d.stats || {};
  document.getElementById('inf-rag-stats').textContent =
    (stats.total_docs || 0) + ' docs, ' + (stats.total_chunks || 0) + ' chunks';
  var docs = d.documents || [];
  var el = document.getElementById('inf-rag-docs');
  if (docs.length === 0) {
    el.innerHTML = '<div style="color:var(--text-dim);padding:4px 0">No documents loaded</div>';
  } else {
    el.innerHTML = docs.map(function(doc) {
      return '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid var(--border-subtle)">' +
        '<span style="color:var(--text-secondary)">' + escapeHtml(doc.filename || doc.doc_id) + '</span>' +
        '<span style="display:flex;align-items:center;gap:8px">' +
          '<span style="color:var(--text-dim)">' + (doc.chunk_count || doc.chunks || 0) + ' chunks</span>' +
          '<button class="btn btn-sm" style="background:transparent;color:var(--accent-red);border-color:var(--accent-red);padding:2px 6px;font-size:0.72em" ' +
            'onclick="infDeleteDoc(\'' + escapeHtml(doc.doc_id) + '\')">x</button>' +
        '</span></div>';
    }).join('');
  }
}

async function infDeleteDoc(docId) {
  var d = await callApi('POST', '/api/rag/delete', {doc_id: docId});
  if (d && d.ok) { showToast('Deleted'); infLoadRagDocs(); }
}

function updateDashboard(d) {
  // Cards
  const connEl = document.getElementById('dash-conn');
  if (d.connected) { connEl.textContent = 'Connected'; connEl.className = 'card-value green'; }
  else { connEl.textContent = 'Offline'; connEl.className = 'card-value red'; }
  document.getElementById('dash-conn-type').textContent = d.connection_type.toUpperCase() + ' | ' + formatUptime(d.uptime);
  document.getElementById('dash-model').textContent = d.model;
  document.getElementById('dash-msgs').textContent = d.message_count;
  document.getElementById('dash-nodes').textContent = d.node_count;

  const avgTime = d.avg_llm_time;
  document.getElementById('dash-avg-time').textContent = avgTime > 0 ? avgTime + 's' : '--';
  document.getElementById('dash-avg-chunks').textContent = d.avg_chunks > 0 ? d.avg_chunks + ' chunks avg' : '';

  // RAG card
  if (d.rag_enabled) {
    document.getElementById('dash-rag-card').style.display = '';
    document.getElementById('dash-rag').textContent = 'ON';
  }

  // Nodes
  if (d.known_nodes && d.known_nodes.length > 0) {
    document.getElementById('dash-nodes-section').style.display = '';
    document.getElementById('dash-node-tags').innerHTML =
      d.known_nodes.map(function(n) { return '<span class="node-tag">' + escapeHtml(n) + '</span>'; }).join('');
  }

  // Recent messages feed
  var msgs = d.messages || [];
  var feed = document.getElementById('dash-feed');
  if (msgs.length === 0) {
    feed.className = 'empty';
    feed.innerHTML = 'Waiting for messages...';
  } else {
    feed.className = '';
    var recent = msgs.slice(-10).reverse();
    feed.innerHTML = recent.map(function(m) {
      var dirClass = m.dir === 'in' ? 'dir-in' : 'dir-out';
      var dirIcon = m.dir === 'in' ? '&#x2B07;' : '&#x2B06;';
      var text = m.text.length > 80 ? m.text.substring(0,80) + '...' : m.text;
      return '<div class="feed-item">' +
        '<span class="feed-dir ' + dirClass + '">' + dirIcon + '</span>' +
        '<span class="node-id" style="min-width:70px">' + escapeHtml(m.node) + '</span>' +
        '<span class="feed-text">' + escapeHtml(text) + '</span>' +
        '<span class="feed-meta">' + relativeTime(m.ts) + '</span>' +
        '</div>';
    }).join('');
  }
}

// ─── Messages Tab ───────────────────────────────────────────────────────────

// Leaflet map
var _meshMap = null;
var _mapMarkers = {};

function initMap() {
  if (_meshMap) return;
  var el = document.getElementById('mesh-map');
  if (!el || typeof L === 'undefined') return;
  _meshMap = L.map('mesh-map', {attributionControl: false}).setView([39.8, -98.5], 4);
  L.tileLayer('/tiles/{z}/{x}/{y}.png', {
    maxZoom: 15,
    attribution: 'OSM'
  }).addTo(_meshMap);
}

function updateMap(positions) {
  if (!_meshMap) initMap();
  if (!_meshMap || !positions) return;
  var bounds = [];
  var count = 0;
  var nodeMeta = (App.state && App.state.node_meta) || {};
  Object.keys(positions).forEach(function(nodeId) {
    var p = positions[nodeId];
    if (!p.lat || !p.lon) return;
    count++;
    var latlng = [p.lat, p.lon];
    bounds.push(latlng);
    // Short label: keep the last 4 chars of a !hex id, otherwise the whole thing truncated
    var shortId = nodeId;
    if (shortId.length > 10) shortId = shortId.slice(-6);
    var meta = nodeMeta[nodeId] || {};
    var hops = (typeof meta.hops === 'number') ? meta.hops : null;
    var hopSuffix = '';
    var hopText = 'Unknown hops';
    if (hops !== null) {
      if (hops === 0) { hopSuffix = ' \u00b7 direct'; hopText = 'Direct (0 hops)'; }
      else if (hops === 1) { hopSuffix = ' \u00b7 1h'; hopText = '1 hop'; }
      else { hopSuffix = ' \u00b7 ' + hops + 'h'; hopText = hops + ' hops'; }
    }
    var label = shortId + hopSuffix;
    // Stale after 10 minutes
    var ageSec = p.last_update ? (Date.now() / 1000 - p.last_update) : 0;
    var staleCls = ageSec > 600 ? ' stale' : '';
    var iconHtml =
      '<div class="node-marker' + staleCls + '">' +
      '  <div class="ring"></div>' +
      '  <div class="core"></div>' +
      '  <div class="label">' + escapeHtml(label) + '</div>' +
      '</div>';
    if (_mapMarkers[nodeId]) {
      _mapMarkers[nodeId].setLatLng(latlng);
      // Refresh the icon so stale + hop label updates as time passes
      var el = _mapMarkers[nodeId].getElement();
      if (el) {
        var inner = el.querySelector('.node-marker');
        if (inner) inner.className = 'node-marker' + staleCls;
        var lbl = el.querySelector('.node-marker .label');
        if (lbl) lbl.textContent = label;
      }
    } else {
      var icon = L.divIcon({
        className: 'node-marker-wrap',
        html: iconHtml,
        iconSize: [28, 28], iconAnchor: [14, 14]
      });
      _mapMarkers[nodeId] = L.marker(latlng, {icon: icon, zIndexOffset: 1000}).addTo(_meshMap);
    }
    var age = p.last_update ? relativeTime(p.last_update) : '';
    var alt = p.alt ? ' | Alt: ' + Math.round(p.alt) + 'm' : '';
    // Escape nodeId for use inside an HTML onclick="..." attribute (allow only safe chars)
    var safeNodeId = nodeId.replace(/[^a-zA-Z0-9!_\-]/g, '');
    var popupHtml =
      '<div style="font-family:monospace;font-size:12px;text-transform:none;min-width:180px">' +
        '<div style="font-weight:bold;color:#00ff41;margin-bottom:4px">' + escapeHtml(nodeId) + '</div>' +
        '<div>' + p.lat.toFixed(5) + ', ' + p.lon.toFixed(5) + alt + '</div>' +
        '<div style="color:#00ff41;margin-top:3px">Hops: ' + escapeHtml(hopText) + '</div>' +
        '<div style="color:#888;margin-top:2px">' + escapeHtml(age) + '</div>' +
        '<button class="btn btn-sm" style="margin-top:8px;width:100%" ' +
          'onclick="dmNode(\'' + safeNodeId + '\')">DM this node</button>' +
      '</div>';
    _mapMarkers[nodeId].bindPopup(popupHtml);
  });
  // Remove stale markers
  Object.keys(_mapMarkers).forEach(function(id) {
    if (!positions[id]) { _meshMap.removeLayer(_mapMarkers[id]); delete _mapMarkers[id]; }
  });
  document.getElementById('map-node-count').textContent = count + ' node(s) with GPS';
  if (bounds.length > 0 && !App._mapFitted) {
    _meshMap.fitBounds(bounds, {padding: [30, 30], maxZoom: 14});
    App._mapFitted = true;
  }
}

// ─── Coverage Tab ───────────────────────────────────────────────────────────

var _covMap = null;
var _covHeatLayer = null;
var _covDeadLayer = null;
var _covSamples = [];

function initCovMap() {
  if (_covMap) return;
  var el = document.getElementById('cov-map');
  if (!el || typeof L === 'undefined') return;
  _covMap = L.map('cov-map', {attributionControl: false}).setView([39.8, -98.5], 4);
  L.tileLayer('/tiles/{z}/{x}/{y}.png', {maxZoom: 15, attribution: 'OSM'}).addTo(_covMap);
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
      statsEl.textContent = td.count + ' samples / ' + td.nodes + ' node(s)';
    } else {
      statsEl.textContent = 'No samples yet — bridge needs to receive packets from nodes with GPS';
    }
    renderCoverage();
  } catch(e) {
    document.getElementById('cov-stats').textContent = 'Error loading coverage: ' + e;
  }
}

// Shared color ramp for grid + heatmap + legend. Input: RSSI in dBm.
// Higher (less negative) = stronger. Missing RSSI is treated as "Faint".
function covColorForRssi(rssi) {
  if (rssi == null) return '#ff3030';         // unknown -> faint
  if (rssi >= -60)  return '#00ff41';         // strong
  if (rssi >= -80)  return '#8aff00';         // good
  if (rssi >= -95)  return '#ffd500';         // ok
  if (rssi >= -110) return '#ff8a00';         // weak
  return '#ff3030';                            // faint
}

var _covGridLayer = null;
var _covGridCell = null; // last seen cellDeg so we can drop on refresh

function renderCoverage() {
  if (!_covMap) return;
  var mode = document.getElementById('cov-mode').value;  // 'grid' | 'heat' | 'both'
  var windowSec = parseInt(document.getElementById('cov-window').value);
  var minRssi = parseInt(document.getElementById('cov-rssi').value);
  var showDead = document.getElementById('cov-deadzones').checked;
  var now = Date.now() / 1000;

  // Filter samples by time window + min-RSSI threshold
  var filtered = _covSamples.filter(function(s) {
    if (windowSec > 0 && (now - s.ts) > windowSec) return false;
    if (s.rssi != null && s.rssi < minRssi) return false;
    return true;
  });

  // Bin into ~40m x 40m cells: key "latBin,lonBin" -> {lat, lon, bestRssi, count}
  var cellDeg = 0.00036; // ~40 m at mid-latitudes
  var cells = {};
  filtered.forEach(function(s) {
    var latBin = Math.round(s.lat / cellDeg);
    var lonBin = Math.round(s.lon / cellDeg);
    var key = latBin + ',' + lonBin;
    var c = cells[key];
    if (!c) {
      cells[key] = { latBin: latBin, lonBin: lonBin, bestRssi: s.rssi, count: 1 };
    } else {
      c.count += 1;
      // Track the strongest signal seen in this cell
      if (s.rssi != null && (c.bestRssi == null || s.rssi > c.bestRssi)) c.bestRssi = s.rssi;
    }
  });

  // ── Clear any previous layers ─────────────────────────────────────────
  if (_covGridLayer) { _covMap.removeLayer(_covGridLayer); _covGridLayer = null; }
  if (_covHeatLayer) { _covMap.removeLayer(_covHeatLayer); _covHeatLayer = null; }
  if (_covDeadLayer) { _covMap.removeLayer(_covDeadLayer); _covDeadLayer = null; }

  // ── Grid render path ──────────────────────────────────────────────────
  if (mode === 'grid' || mode === 'both') {
    _covGridLayer = L.layerGroup();
    Object.keys(cells).forEach(function(k) {
      var c = cells[k];
      var latMin = c.latBin * cellDeg - cellDeg / 2;
      var latMax = c.latBin * cellDeg + cellDeg / 2;
      var lonMin = c.lonBin * cellDeg - cellDeg / 2;
      var lonMax = c.lonBin * cellDeg + cellDeg / 2;
      var color = covColorForRssi(c.bestRssi);
      // Single-sample cells rendered lighter; multi-sample cells solid
      var fillOpacity = (c.count >= 3) ? 0.72 : (c.count === 2 ? 0.58 : 0.42);
      L.rectangle([[latMin, lonMin], [latMax, lonMax]], {
        color: color,
        weight: 1,
        opacity: 0.85,
        fillColor: color,
        fillOpacity: fillOpacity
      }).addTo(_covGridLayer);
    });
    _covGridLayer.addTo(_covMap);
  }

  // ── Heatmap render path ───────────────────────────────────────────────
  if ((mode === 'heat' || mode === 'both') && filtered.length > 0 && typeof L.heatLayer === 'function') {
    var heatPoints = filtered.map(function(s) {
      var intensity = 0.5;
      if (s.rssi != null) {
        // Clamp RSSI [-130..-30] -> [0..1]
        intensity = Math.max(0, Math.min(1, (s.rssi - (-130)) / 100));
      }
      return [s.lat, s.lon, intensity];
    });
    _covHeatLayer = L.heatLayer(heatPoints, {
      radius: 55, blur: 35, maxZoom: 15, minOpacity: 0.55,
      gradient: {
        0.0: '#7a0000', 0.2: '#ff3030', 0.4: '#ff8a00',
        0.6: '#ffd500', 0.8: '#8aff00', 1.0: '#00ff41'
      }
    }).addTo(_covMap);
  }

  // ── Dead zones (same cell hash, much bigger visuals) ──────────────────
  if (showDead) {
    var deadCells = [];
    Object.keys(cells).forEach(function(k) {
      var c = cells[k];
      var isDead = (c.bestRssi == null || c.bestRssi < -110);
      if (isDead) {
        var lat = c.latBin * cellDeg;
        var lon = c.lonBin * cellDeg;
        deadCells.push([lat, lon]);
      }
    });
    if (deadCells.length > 0) {
      _covDeadLayer = L.layerGroup();
      deadCells.forEach(function(latlng) {
        L.circle(latlng, {
          radius: 80, color: '#ff3030', weight: 2,
          fillColor: '#ff0033', fillOpacity: 0.45
        }).addTo(_covDeadLayer);
      });
      _covDeadLayer.addTo(_covMap);
    }
  }

  // ── Always re-fit on render when there is data ────────────────────────
  if (filtered.length > 0) {
    var bounds = filtered.map(function(s) { return [s.lat, s.lon]; });
    try { _covMap.fitBounds(bounds, {padding: [40, 40], maxZoom: 16}); } catch(e) {}
  }
}

// Populate send-to dropdown with known nodes
function updateSendDropdown(knownNodes) {
  var sel = document.getElementById('msg-send-to');
  if (!sel) return;
  var cur = sel.value;
  var opts = '<option value="">Broadcast</option>';
  (knownNodes || []).forEach(function(n) {
    opts += '<option value="' + escapeHtml(n) + '">' + escapeHtml(n) + '</option>';
  });
  sel.innerHTML = opts;
  if (cur) sel.value = cur;
}

async function sendMeshMsg() {
  var text = document.getElementById('msg-send-text').value.trim();
  if (!text) return;
  var nodeId = document.getElementById('msg-send-to').value;
  var channel = parseInt(document.getElementById('msg-send-ch').value);
  var statusEl = document.getElementById('msg-send-status');
  statusEl.textContent = 'Sending...';
  var d = await callApi('POST', '/api/send-mesh', {text: text, node_id: nodeId, channel: channel});
  if (d && d.ok) {
    statusEl.textContent = 'Sent (' + d.direction + ')';
    document.getElementById('msg-send-text').value = '';
    setTimeout(function() { statusEl.textContent = ''; }, 3000);
  } else {
    statusEl.textContent = 'Failed: ' + (d ? d.error : 'network error');
  }
}

// Pre-fill the Send Message form targeted at a specific node and scroll it
// into view. Called from the map popup's "DM this node" button.
function dmNode(nodeId) {
  if (!nodeId) return;
  if (App.currentTab !== 'messages') switchTab('messages');
  var sel = document.getElementById('msg-send-to');
  if (sel) {
    // Make sure the node is an option (it may be a position-only node that
    // never sent a text message and therefore isn't in known_nodes yet)
    var found = false;
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === nodeId) { found = true; break; }
    }
    if (!found) {
      var opt = document.createElement('option');
      opt.value = nodeId;
      opt.textContent = nodeId;
      sel.appendChild(opt);
    }
    sel.value = nodeId;
  }
  // Close any open popup so the form is visible
  if (_meshMap && _meshMap.closePopup) _meshMap.closePopup();
  var form = document.getElementById('msg-send-text');
  if (form) {
    // Find the enclosing Send Message card (2 parents up from the input)
    var card = form.closest('div[style*="accent-blue"]') || form.parentElement.parentElement;
    if (card && card.scrollIntoView) {
      card.scrollIntoView({behavior: 'smooth', block: 'center'});
      // Brief highlight flash so it's obvious where we jumped to
      var origShadow = card.style.boxShadow;
      card.style.boxShadow = '0 0 0 2px var(--accent-blue), 0 0 18px rgba(77,166,255,0.6)';
      setTimeout(function() { card.style.boxShadow = origShadow; }, 1100);
    }
    setTimeout(function() { form.focus(); }, 350);
  }
  var statusEl = document.getElementById('msg-send-status');
  if (statusEl) {
    statusEl.textContent = 'DM target: ' + nodeId;
    setTimeout(function() {
      if (statusEl.textContent.indexOf('DM target:') === 0) statusEl.textContent = '';
    }, 4000);
  }
}

function setMsgFilter(f) {
  App.messageFilter = f;
  document.querySelectorAll('.msg-controls .filter-btn').forEach(function(b) {
    b.classList.toggle('active', b.dataset.filter === f);
  });
}

function updateMessages(d) {
  var msgs = d.messages || [];
  // Filter
  if (App.messageFilter !== 'all') msgs = msgs.filter(function(m) { return m.dir === App.messageFilter; });
  if (App.messageSearch) {
    var q = App.messageSearch.toLowerCase();
    msgs = msgs.filter(function(m) { return m.text.toLowerCase().indexOf(q) !== -1 || m.node.toLowerCase().indexOf(q) !== -1; });
  }
  msgs = msgs.slice().reverse();

  var tbody = document.getElementById('msg-body');
  if (msgs.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">No messages' + (App.messageFilter !== 'all' || App.messageSearch ? ' matching filter' : '') + '</td></tr>';
    return;
  }
  tbody.innerHTML = msgs.map(function(m) {
    var dirClass = m.dir === 'in' ? 'dir-in' : 'dir-out';
    var dirIcon = m.dir === 'in' ? '&#x2B07;' : '&#x2B06;';
    return '<tr>' +
      '<td class="meta">' + formatTime(m.ts) + '</td>' +
      '<td class="' + dirClass + '">' + dirIcon + '</td>' +
      '<td class="node-id">' + escapeHtml(m.node) + '</td>' +
      '<td class="msg-text">' + escapeHtml(m.text) + '</td>' +
      '<td class="meta">' + (m.chunks > 0 ? m.chunks : '') + '</td>' +
      '<td class="meta">' + (m.llm_time > 0 ? m.llm_time + 's' : '') + '</td>' +
      '</tr>';
  }).join('');

  if (App.autoScrollMessages) {
    var sc = document.getElementById('msg-scroll');
    sc.scrollTop = 0; // newest at top
  }
}

// ─── Controls Tab ───────────────────────────────────────────────────────────

async function loadControlsData() {
  App.controlsLoaded = true;
  // Load models
  await refreshModels();
  // Load system prompt
  var pd = await callApi('GET', '/api/system-prompt');
  if (pd) {
    document.getElementById('ctrl-prompt').value = pd.prompt;
    document.getElementById('ctrl-prompt-count').textContent = pd.prompt.length + ' chars';
  }
  // Load config
  var cd = await callApi('GET', '/api/config');
  if (cd) {
    document.getElementById('ctrl-max-len').value = cd.max_response_length;
    document.getElementById('ctrl-max-len-val').textContent = cd.max_response_length;
    document.getElementById('ctrl-compression').checked = cd.compression_enabled;
  }
  // RAG
  if (App.state.rag_enabled !== undefined) {
    var ragCard = document.getElementById('ctrl-rag-card');
    // Show RAG card if bridge has rag capability
    var rs = await callApi('GET', '/api/rag/stats');
    if (rs && rs.available) {
      ragCard.style.display = '';
      document.getElementById('ctrl-rag-toggle').checked = App.state.rag_enabled;
      var stats = rs.stats || {};
      document.getElementById('ctrl-rag-stats').textContent =
        (stats.documents || 0) + ' docs, ' + (stats.chunks || 0) + ' chunks';
      loadRagDocs();
    }
  }
}

async function refreshModels() {
  var d = await callApi('GET', '/api/models');
  if (!d) return;
  var sel = document.getElementById('ctrl-model-select');
  sel.innerHTML = d.models.map(function(m) {
    return '<option value="' + escapeHtml(m) + '"' + (m === d.current ? ' selected' : '') + '>' + escapeHtml(m) + '</option>';
  }).join('');
  document.getElementById('ctrl-current-model').textContent = d.current;
}

async function switchModel() {
  var model = document.getElementById('ctrl-model-select').value;
  var d = await callApi('POST', '/api/model', {model: model});
  if (d && d.ok) {
    showToast('Model switched to ' + d.model, 'success');
    document.getElementById('ctrl-current-model').textContent = d.model;
  }
}

async function savePrompt() {
  var prompt = document.getElementById('ctrl-prompt').value;
  var d = await callApi('POST', '/api/system-prompt', {prompt: prompt});
  if (d && d.ok) showToast('System prompt saved', 'success');
}

document.getElementById('ctrl-prompt').addEventListener('input', function() {
  document.getElementById('ctrl-prompt-count').textContent = this.value.length + ' chars';
});

async function applySettings() {
  var d = await callApi('POST', '/api/config', {
    max_response_length: parseInt(document.getElementById('ctrl-max-len').value),
    compression_enabled: document.getElementById('ctrl-compression').checked
  });
  if (d && d.ok) showToast('Settings applied', 'success');
}

async function toggleRag(enabled) {
  var d = await callApi('POST', '/api/rag/toggle', {enabled: enabled});
  if (d && d.ok) showToast('RAG ' + (enabled ? 'enabled' : 'disabled'), 'success');
}

async function ingestUrl() {
  var urlInput = document.getElementById('ctrl-rag-url');
  var url = urlInput.value.trim();
  if (!url) return;
  var status = document.getElementById('ctrl-rag-url-status');
  status.innerHTML = '<span style="color:var(--text-muted)">Fetching & ingesting...</span>';
  var d = await callApi('POST', '/api/rag/ingest-url', {url: url});
  if (d && d.ok) {
    status.innerHTML = '<span style="color:var(--accent-green)">Ingested: ' +
      escapeHtml(d.filename) + ' (' + d.chunks + ' chunks)</span>';
    urlInput.value = '';
    loadRagDocs();
  } else {
    status.innerHTML = '<span style="color:var(--accent-red)">Error: ' +
      escapeHtml((d && d.error) || 'Unknown error') + '</span>';
  }
}

async function loadRagDocs() {
  var d = await callApi('GET', '/api/rag/stats');
  var container = document.getElementById('ctrl-rag-docs');
  if (!container) return;
  if (!d || !d.documents || d.documents.length === 0) {
    container.innerHTML = '<span style="color:var(--text-muted)">No documents ingested yet.</span>';
    return;
  }
  var html = '<table style="width:100%;border-collapse:collapse">';
  html += '<tr style="color:var(--text-muted);font-size:0.9em"><td>Document</td><td style="width:60px;text-align:right">Chunks</td><td style="width:50px"></td></tr>';
  d.documents.forEach(function(doc) {
    html += '<tr style="border-top:1px solid var(--border)">';
    html += '<td style="padding:4px 0;word-break:break-all">' + escapeHtml(doc.filename) + '</td>';
    html += '<td style="text-align:right;padding:4px 0">' + doc.chunk_count + '</td>';
    html += '<td style="text-align:right;padding:4px 0"><button onclick="deleteDoc(\'' +
      escapeHtml(doc.doc_id) + '\')" style="background:none;border:none;color:var(--accent-red);cursor:pointer;font-size:0.85em" title="Delete">&#x2715;</button></td>';
    html += '</tr>';
  });
  html += '</table>';
  container.innerHTML = html;
}

async function deleteDoc(docId) {
  if (!confirm('Delete this document from the knowledge base?')) return;
  var d = await callApi('POST', '/api/rag/delete', {doc_id: docId});
  if (d && d.ok) {
    showToast('Document deleted', 'success');
    loadRagDocs();
  } else {
    showToast('Delete failed: ' + ((d && d.error) || 'Unknown error'), 'error');
  }
}

async function clearHistory() {
  if (!confirm('Clear all conversation history for all nodes? This cannot be undone.')) return;
  var d = await callApi('POST', '/api/clear-history', {});
  if (d && d.ok) showToast('Cleared history for ' + d.cleared + ' node(s)', 'success');
}

// ─── Debug Tab ──────────────────────────────────────────────────────────────

function setLogFilter(level, btn) {
  App.logFilter = level;
  document.querySelectorAll('.log-controls .filter-btn').forEach(function(b) { b.classList.remove('active'); });
  if (btn) btn.classList.add('active');
}

function updateDebugLogs() {
  var logs = App.logs;
  if (App.logFilter !== 'all') {
    logs = logs.filter(function(l) { return l.level === App.logFilter; });
  }
  var viewer = document.getElementById('log-viewer');
  if (logs.length === 0) {
    viewer.innerHTML = '<span style="color:var(--text-dim)">No logs' + (App.logFilter !== 'all' ? ' at ' + App.logFilter + ' level' : '') + '</span>';
    return;
  }
  viewer.innerHTML = logs.map(function(l) {
    return '<div class="log-line log-' + l.level + '">' + escapeHtml(l.message) + '</div>';
  }).join('');
  if (document.getElementById('log-autoscroll').checked) {
    viewer.scrollTop = viewer.scrollHeight;
  }
}

async function loadDebugData() {
  var d = await callApi('GET', '/api/debug');
  if (d) {
    document.getElementById('dbg-threads').textContent = d.thread_count;
    document.getElementById('dbg-queue').textContent = d.queue_size;
    document.getElementById('dbg-dedup').textContent = d.dedup_cache_size;
  }
}

function updateDebugInfo(d) {
  document.getElementById('dbg-conn-type').textContent = d.connection_type.toUpperCase();
  document.getElementById('dbg-ollama-url').textContent = d.ollama_url;
  document.getElementById('dbg-model').textContent = d.model;
  document.getElementById('dbg-firmware').textContent = d.firmware_version || '--';
  document.getElementById('dbg-library').textContent = d.library_version || '--';
  document.getElementById('dbg-hw-model').textContent = d.hw_model || '--';
  document.getElementById('dbg-avg-time').textContent = d.avg_llm_time > 0 ? d.avg_llm_time + 's' : '--';
  document.getElementById('dbg-avg-chunks').textContent = d.avg_chunks > 0 ? d.avg_chunks : '--';
  document.getElementById('dbg-total-calls').textContent = d.total_llm_calls;
  // Refresh internals every few polls
  loadDebugData();
}

// ─── Chat Panel ─────────────────────────────────────────────────────────────

var chatMessages = [];

async function sendChat() {
  var input = document.getElementById('chat-input');
  var msg = input.value.trim();
  if (!msg) return;

  input.value = '';
  input.disabled = true;
  document.getElementById('chat-send-btn').disabled = true;

  // Add user message
  chatMessages.push({role: 'you', text: msg, ts: Date.now()/1000});
  renderChat();

  // Show loading
  var history = document.getElementById('chat-history');
  var loading = document.createElement('div');
  loading.className = 'chat-loading';
  loading.textContent = 'AI is thinking...';
  history.appendChild(loading);
  history.scrollTop = history.scrollHeight;

  var d = await callApi('POST', '/api/chat', {message: msg});

  // Remove loading
  if (loading.parentNode) loading.parentNode.removeChild(loading);

  if (d && d.ok) {
    chatMessages.push({role: 'ai', text: d.response, ts: Date.now()/1000});
  } else {
    chatMessages.push({role: 'ai', text: 'Error: ' + (d ? d.error : 'No response'), ts: Date.now()/1000});
  }

  renderChat();
  input.disabled = false;
  document.getElementById('chat-send-btn').disabled = false;
  input.focus();
}

function clearChat() {
  chatMessages = [];
  renderChat();
  // Also clear LLM history for dashboard node
  callApi('POST', '/api/clear-history', {node_id: 'dashboard'});
}

function renderChat() {
  var history = document.getElementById('chat-history');
  if (chatMessages.length === 0) {
    history.innerHTML = '<div class="empty" style="padding:20px">Send a message to test the LLM directly (does not transmit over radio)</div>';
    return;
  }
  history.innerHTML = chatMessages.map(function(m) {
    return '<div class="chat-msg">' +
      '<div class="chat-role ' + m.role + '">' + (m.role === 'you' ? 'You' : 'AI') + '</div>' +
      '<div class="chat-text">' + escapeHtml(m.text) + '</div>' +
      '<div class="chat-meta">' + relativeTime(m.ts) + '</div>' +
      '</div>';
  }).join('');
  history.scrollTop = history.scrollHeight;
}

// ─── Connection Management ────────────────────────────────────────────────

var _bleLastDevice = null;

function updateConnectionCard(d) {
  var dot = document.getElementById('conn-mgr-dot');
  var status = document.getElementById('conn-mgr-status');
  var detail = document.getElementById('conn-mgr-detail');
  var disconnBtn = document.getElementById('conn-disconnect-btn');
  if (d.connected) {
    dot.className = 'conn-dot on';
    status.textContent = 'Connected';
    detail.textContent = (d.connection_type || '').toUpperCase() +
      (d.connection_address ? ' — ' + d.connection_address : '');
    disconnBtn.style.display = '';
  } else {
    dot.className = 'conn-dot off';
    status.textContent = 'Disconnected';
    detail.textContent = '';
    disconnBtn.style.display = 'none';
  }
  // BLE availability
  if (d.ble_available === false) {
    document.getElementById('ble-unavailable').style.display = '';
    document.getElementById('ble-scan-btn').disabled = true;
    document.getElementById('ble-scan-btn').title = 'BLE not available';
  }
}

async function bleScan() {
  var btn = document.getElementById('ble-scan-btn');
  var statusEl = document.getElementById('ble-scan-status');
  var listEl = document.getElementById('ble-device-list');
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  statusEl.textContent = 'Scanning for Meshtastic devices (~10s)...';
  listEl.innerHTML = '';
  try {
    var d = await callApi('GET', '/api/ble/scan?timeout=10');
    if (!d || !d.devices) {
      statusEl.textContent = 'Scan failed.';
      return;
    }
    if (d.devices.length === 0) {
      statusEl.textContent = 'No Meshtastic devices found. Make sure Bluetooth is enabled on your radio.';
      return;
    }
    // Check for error responses (e.g. permission issues)
    if (d.devices.length === 1 && d.devices[0].error) {
      var err = d.devices[0];
      if (err.error === 'bluetooth_permission') {
        statusEl.textContent = '';
        listEl.innerHTML = '<div style="padding:12px;background:var(--bg-secondary);border:1px solid var(--accent-orange);border-radius:6px;font-size:0.85em">' +
          '<div style="color:var(--accent-orange);font-weight:600;margin-bottom:6px">Bluetooth Permission Required</div>' +
          '<div style="color:var(--text-secondary);margin-bottom:8px">' + escapeHtml(err.message) + '</div>' +
          '<div style="color:var(--text-muted);font-size:0.82em">Restart with <code style="background:var(--bg-tertiary);padding:2px 6px;border-radius:3px">./mesh-llm.sh</code> to auto-fix, or check the Debug tab for manual instructions.</div>' +
          '</div>';
      } else {
        statusEl.textContent = 'Scan error: ' + (err.message || err.error);
      }
      return;
    }
    // Filter out any error entries mixed with real devices
    var realDevices = d.devices.filter(function(dev) { return !dev.error; });
    if (realDevices.length === 0) {
      statusEl.textContent = 'No Meshtastic devices found. Make sure Bluetooth is enabled on your radio.';
      return;
    }
    statusEl.textContent = realDevices.length + ' device(s) found:';
    var html = '<div style="display:flex;flex-direction:column;gap:6px">';
    realDevices.forEach(function(dev) {
      var rssiPct = Math.min(100, Math.max(0, (dev.rssi + 100)));
      var rssiColor = rssiPct > 60 ? 'var(--accent-green)' : rssiPct > 30 ? 'var(--accent-orange)' : 'var(--accent-red)';
      html += '<div style="display:flex;align-items:center;gap:10px;padding:8px 12px;background:var(--bg-secondary);border-radius:6px;border:1px solid var(--border)">' +
        '<div style="flex:1">' +
          '<div style="font-size:0.9em">' + escapeHtml(dev.name || 'Unknown') + '</div>' +
          '<div style="font-size:0.75em;color:var(--text-muted)">' + escapeHtml(dev.address) + '</div>' +
        '</div>' +
        '<div style="width:60px;text-align:center">' +
          '<div style="height:4px;background:var(--bg-tertiary);border-radius:2px;overflow:hidden">' +
            '<div style="height:100%;width:' + rssiPct + '%;background:' + rssiColor + '"></div>' +
          '</div>' +
          '<div style="font-size:0.7em;color:var(--text-muted);margin-top:2px">' + dev.rssi + ' dBm</div>' +
        '</div>' +
        '<button class="btn btn-primary btn-sm" onclick="bleConnect(\'' + escapeHtml(dev.address) + '\',\'' + escapeHtml(dev.name || '') + '\')">Connect</button>' +
      '</div>';
    });
    html += '</div>';
    listEl.innerHTML = html;
  } catch(e) {
    statusEl.textContent = 'Scan error: ' + e;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Scan';
  }
}

async function bleConnect(address, name) {
  var statusEl = document.getElementById('ble-scan-status');
  statusEl.textContent = 'Connecting to ' + (name || address) + '...';
  var d = await callApi('POST', '/api/connection/switch', {type: 'ble', address: address});
  if (d && d.ok) {
    statusEl.textContent = 'Connected to ' + (name || address);
    loadLastBleDevice();
  } else {
    statusEl.textContent = 'Connection attempt started — retrying in background...';
  }
}

async function bleQuickConnect() {
  if (!_bleLastDevice) return;
  await bleConnect(_bleLastDevice.address, _bleLastDevice.name);
}

async function disconnectRadio() {
  await callApi('POST', '/api/connection/disconnect');
}

function connTypeChanged() {
  var sel = document.getElementById('conn-type-select');
  var inp = document.getElementById('conn-address-input');
  if (sel.value === 'serial') inp.placeholder = 'auto-detect (or /dev/...)';
  else if (sel.value === 'tcp') inp.placeholder = '192.168.1.1:4403';
  else inp.placeholder = 'BLE address (or leave empty to scan)';
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
  if (d && d.ok) {
    document.getElementById('ble-scan-status').textContent = 'Connected!';
  } else {
    document.getElementById('ble-scan-status').textContent = 'Connecting in background...';
  }
}

async function loadLastBleDevice() {
  try {
    var d = await callApi('GET', '/api/ble/last-device');
    if (d && d.device && d.device.address) {
      _bleLastDevice = d.device;
      var el = document.getElementById('ble-last-device');
      el.style.display = '';
      document.getElementById('ble-last-name').textContent =
        (d.device.name || d.device.address);
    }
  } catch(e) {}
}

// Load last device on init
loadLastBleDevice();

// ─── Init ───────────────────────────────────────────────────────────────────

poll();
setInterval(poll, 2000);
</script>
</body>
</html>"""
