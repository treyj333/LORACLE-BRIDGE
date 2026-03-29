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


def record_message(direction, node_id, text, chunks=0, llm_time=0):
    """Called by the bridge to record a message event."""
    _messages.append({
        "ts": time.time(),
        "dir": direction,
        "node": node_id,
        "text": text[:2000],
        "chunks": chunks,
        "llm_time": round(llm_time, 1),
    })
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
    return jsonify(state)


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


def start_dashboard(port=8000):
    """Start the dashboard in a background thread."""
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
<style>
:root {
  --bg-primary: #0d1117;
  --bg-secondary: #161b22;
  --bg-tertiary: #1c2128;
  --bg-input: #0d1117;
  --border: #30363d;
  --border-subtle: #21262d;
  --text-primary: #e6edf3;
  --text-secondary: #c9d1d9;
  --text-muted: #8b949e;
  --text-dim: #484f58;
  --accent-blue: #58a6ff;
  --accent-green: #3fb950;
  --accent-red: #f85149;
  --accent-yellow: #d29922;
  --accent-purple: #bc8cff;
  --radius: 8px;
  --radius-sm: 4px;
  --font-mono: 'SF Mono', 'Fira Code', 'Consolas', 'Courier New', monospace;
  --font-sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: var(--font-sans); background: var(--bg-primary); color: var(--text-secondary); }
button { cursor: pointer; font-family: var(--font-sans); }
input, select, textarea { font-family: var(--font-sans); }

/* ─── Top Bar ─── */
#top-bar {
  position: sticky; top: 0; z-index: 100;
  background: var(--bg-secondary); border-bottom: 1px solid var(--border);
  padding: 10px 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
}
.logo { display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 1.05em; color: var(--text-primary); white-space: nowrap; }
.logo svg { flex-shrink: 0; }
.top-badges { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-left: auto; }
.badge {
  display: flex; align-items: center; gap: 5px;
  background: var(--bg-tertiary); border: 1px solid var(--border-subtle);
  border-radius: 20px; padding: 3px 10px; font-size: 0.78em; white-space: nowrap;
}
.badge .label { color: var(--text-muted); }
.badge .val { color: var(--text-primary); font-weight: 600; }
.conn-dot {
  width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex-shrink: 0;
}
.conn-dot.on { background: var(--accent-green); box-shadow: 0 0 6px var(--accent-green); animation: pulse 2s ease-in-out infinite; }
.conn-dot.off { background: var(--accent-red); }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }

/* ─── Tabs ─── */
#tab-nav {
  display: flex; background: var(--bg-secondary); border-bottom: 1px solid var(--border);
  overflow-x: auto; -webkit-overflow-scrolling: touch;
}
.tab-btn {
  background: none; border: none; border-bottom: 2px solid transparent;
  color: var(--text-muted); padding: 10px 18px; font-size: 0.85em; font-weight: 500;
  transition: color 0.2s, border-color 0.2s; white-space: nowrap;
}
.tab-btn:hover { color: var(--text-secondary); }
.tab-btn.active { color: var(--accent-blue); border-bottom-color: var(--accent-blue); }

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
  background: var(--bg-secondary); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 14px 16px;
}
.card-label { font-size: 0.72em; text-transform: uppercase; color: var(--text-muted); margin-bottom: 6px; letter-spacing: 0.5px; }
.card-value { font-size: 1.5em; font-weight: 700; color: var(--text-primary); }
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
.msg-scroll { max-height: 500px; overflow-y: auto; border: 1px solid var(--border); border-radius: var(--radius); }
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
.node-id { font-family: var(--font-mono); color: #79c0ff; font-size: 0.92em; }
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
  background: var(--bg-secondary); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 16px;
}
.ctrl-card h3 { font-size: 0.88em; color: var(--text-primary); margin-bottom: 12px; }
.ctrl-row { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.ctrl-row:last-child { margin-bottom: 0; }
.ctrl-label { font-size: 0.8em; color: var(--text-muted); min-width: 100px; }
.ctrl-select, .ctrl-input {
  background: var(--bg-input); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 6px 10px; color: var(--text-primary);
  font-size: 0.85em; flex: 1;
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
  background: var(--accent-blue); color: #fff; border: none;
  border-radius: var(--radius-sm); padding: 6px 14px; font-size: 0.82em; font-weight: 500;
  transition: opacity 0.2s;
}
.btn:hover { opacity: 0.85; }
.btn-sm { padding: 4px 10px; font-size: 0.78em; }
.btn-danger { background: var(--accent-red); }
.btn-secondary { background: var(--bg-tertiary); border: 1px solid var(--border); color: var(--text-secondary); }

/* Toggle switch */
.toggle { position: relative; display: inline-block; width: 40px; height: 22px; }
.toggle input { opacity: 0; width: 0; height: 0; }
.toggle-slider {
  position: absolute; inset: 0; background: var(--bg-tertiary); border: 1px solid var(--border);
  border-radius: 22px; transition: 0.2s; cursor: pointer;
}
.toggle-slider::before {
  content: ''; position: absolute; width: 16px; height: 16px; left: 2px; bottom: 2px;
  background: var(--text-muted); border-radius: 50%; transition: 0.2s;
}
.toggle input:checked + .toggle-slider { background: var(--accent-blue); border-color: var(--accent-blue); }
.toggle input:checked + .toggle-slider::before { transform: translateX(18px); background: #fff; }

/* ─── Debug ─── */
.log-controls { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; flex-wrap: wrap; }
.log-viewer {
  background: var(--bg-secondary); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 10px;
  font-family: var(--font-mono); font-size: 0.75em; line-height: 1.6;
  max-height: 400px; overflow-y: auto; white-space: pre-wrap; word-break: break-all;
}
.log-line { padding: 1px 0; }
.log-DEBUG { color: var(--accent-purple); }
.log-INFO { color: var(--text-secondary); }
.log-WARNING { color: var(--accent-yellow); }
.log-ERROR { color: var(--accent-red); }
.debug-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-top: 16px; }
.debug-card {
  background: var(--bg-secondary); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 14px;
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
  background: var(--accent-blue); color: #fff; width: 24px; height: 24px;
  border-radius: 50%; display: flex; align-items: center; justify-content: center;
  font-size: 0.78em; font-weight: 700; flex-shrink: 0; margin-top: 1px;
}
.step-text { flex: 1; }
.step-text strong { color: var(--text-primary); }

/* ─── Nodes ─── */
.node-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.node-tag {
  background: var(--bg-tertiary); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm);
  padding: 2px 8px; font-size: 0.78em; font-family: var(--font-mono); color: #79c0ff;
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
  background: var(--bg-secondary); border: 1px solid var(--border); border-left: 3px solid var(--accent-blue);
  border-radius: var(--radius-sm); padding: 10px 16px; font-size: 0.82em; color: var(--text-primary);
  animation: slideIn 0.3s ease; min-width: 200px; max-width: 360px;
}
.toast-success { border-left-color: var(--accent-green); }
.toast-error { border-left-color: var(--accent-red); }
.toast.fade-out { opacity: 0; transition: opacity 0.3s; }
@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

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
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#58a6ff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
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
  <button class="tab-btn active" data-tab="dashboard">Dashboard</button>
  <button class="tab-btn" data-tab="messages">Messages</button>
  <button class="tab-btn" data-tab="controls">Controls</button>
  <button class="tab-btn" data-tab="debug">Debug</button>
  <button class="tab-btn" data-tab="guide">Guide</button>
</nav>

<!-- ═══ Tab Content ═══ -->
<main id="tab-content">

  <!-- ──── Dashboard Tab ──── -->
  <section id="tab-dashboard" class="tab-panel active">
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

    <div id="dash-nodes-section" style="display:none;margin-bottom:16px">
      <div class="section-title" style="margin-bottom:8px">Known Nodes</div>
      <div class="node-tags" id="dash-node-tags"></div>
    </div>

    <div class="section-head">
      <div class="section-title">Recent Messages</div>
    </div>
    <div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius)">
      <div id="dash-feed" class="empty">Waiting for messages...</div>
    </div>

    <!-- Chat Panel -->
    <div style="margin-top:20px">
      <div class="section-head">
        <div class="section-title">Chat with LLM</div>
        <button class="btn btn-sm btn-secondary" onclick="clearChat()">Clear</button>
      </div>
      <div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius);padding:0">
        <div id="chat-history" style="max-height:300px;overflow-y:auto;padding:12px;min-height:60px">
          <div class="empty" style="padding:20px">Send a message to test the LLM directly (does not transmit over radio)</div>
        </div>
        <div style="display:flex;gap:8px;padding:10px;border-top:1px solid var(--border-subtle)">
          <input class="search-input" type="text" id="chat-input" placeholder="Ask the AI something..." style="flex:1" onkeydown="if(event.key==='Enter')sendChat()">
          <button class="btn" id="chat-send-btn" onclick="sendChat()">Send</button>
        </div>
      </div>
    </div>
  </section>

  <!-- ──── Messages Tab ──── -->
  <section id="tab-messages" class="tab-panel">
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

  <!-- ──── Controls Tab ──── -->
  <section id="tab-controls" class="tab-panel">
    <div class="ctrl-grid">

      <div class="ctrl-card">
        <h3>Model</h3>
        <div class="ctrl-row">
          <select class="ctrl-select" id="ctrl-model-select"><option>Loading...</option></select>
          <button class="btn btn-sm" onclick="refreshModels()">&#x21BB;</button>
          <button class="btn" onclick="switchModel()">Apply</button>
        </div>
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

    // Dashboard tab
    if (App.currentTab === 'dashboard') updateDashboard(d);

    // Messages tab
    if (App.currentTab === 'messages') updateMessages(d);

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

// ─── Dashboard Tab ──────────────────────────────────────────────────────────

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

// ─── Init ───────────────────────────────────────────────────────────────────

poll();
setInterval(poll, 2000);
</script>
</body>
</html>"""
