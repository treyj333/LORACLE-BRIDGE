"""Brief dashboard tab — SITREP display, history, and generation controls."""

import time

from flask import jsonify, request


def get_tab_config() -> dict:
    return {
        "id": "brief",
        "label": "Brief",
        "html": _TAB_HTML,
        "js": _TAB_JS,
    }


def get_api_routes(addon):
    """Return Flask API routes for Brief."""

    def api_latest():
        latest = addon.aggregator.get_latest_sitrep()
        if latest:
            latest["age_minutes"] = int((time.time() - latest["generated_at"]) / 60)
        return jsonify({"sitrep": latest})

    def api_history():
        history = addon.aggregator.get_sitrep_history(limit=20)
        now = time.time()
        for s in history:
            s["age"] = _relative_time(now - s["generated_at"])
        return jsonify({"history": history})

    def api_generate():
        content = addon._generate_sitrep()
        return jsonify({"content": content})

    def api_stats():
        stats = addon.aggregator.get_stats()
        stats["interval_minutes"] = addon.interval_minutes
        stats["last_sitrep_age"] = int((time.time() - addon._last_sitrep_time) / 60)
        return jsonify(stats)

    def api_export():
        data = request.get_json(silent=True) or {}
        sitrep_id = data.get("sitrep_id")
        fmt = data.get("format", "text")

        if sitrep_id:
            sitrep = addon.aggregator.get_sitrep_by_id(sitrep_id)
        else:
            sitrep = addon.aggregator.get_latest_sitrep()

        if not sitrep:
            return jsonify({"error": "No SITREP found"}), 404

        if fmt == "pdf":
            from addons.brief.exporter import export_pdf
            path = export_pdf(sitrep["content"], sitrep["sitrep_id"])
        else:
            from addons.brief.exporter import export_text
            path = export_text(sitrep["content"], sitrep["sitrep_id"])

        return jsonify({"path": path, "format": fmt})

    def api_view():
        sitrep_id = request.args.get("id", "")
        if not sitrep_id:
            return jsonify({"error": "No sitrep_id"}), 400
        sitrep = addon.aggregator.get_sitrep_by_id(sitrep_id)
        if not sitrep:
            return jsonify({"error": "Not found"}), 404
        return jsonify({"sitrep": sitrep})

    return [
        ("GET", "/api/brief/latest", api_latest),
        ("GET", "/api/brief/history", api_history),
        ("POST", "/api/brief/generate", api_generate),
        ("GET", "/api/brief/stats", api_stats),
        ("POST", "/api/brief/export", api_export),
        ("GET", "/api/brief/view", api_view),
    ]


def _relative_time(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    elif seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


_TAB_HTML = """
<h2>LORACLE BRIEF — Situation Reports</h2>
<p style="color: var(--text-muted); margin-bottom: 16px;">
  AI-generated SITREPs from mesh network traffic.
</p>

<div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 20px;">
  <div class="stat-card">
    <div class="stat-label">Total Events</div>
    <div class="stat-value" id="brief-events">—</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Total SITREPs</div>
    <div class="stat-value" id="brief-sitreps">—</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Interval</div>
    <div class="stat-value" id="brief-interval">—</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Last SITREP</div>
    <div class="stat-value" id="brief-last-age">—</div>
  </div>
</div>

<div style="display: flex; gap: 8px; margin-bottom: 16px;">
  <button onclick="briefGenerate()"
    style="background: var(--accent-blue); color: white; border: none;
    padding: 8px 20px; border-radius: 0; font-weight: 600; cursor: pointer;">
    Generate SITREP Now
  </button>
  <button onclick="briefExport('text')"
    style="background: var(--bg-secondary); color: var(--text-primary); border: 1px solid var(--border);
    padding: 8px 16px; border-radius: 0; cursor: pointer;">
    Export Text
  </button>
  <button onclick="briefExport('pdf')"
    style="background: var(--bg-secondary); color: var(--text-primary); border: 1px solid var(--border);
    padding: 8px 16px; border-radius: 0; cursor: pointer;">
    Export PDF
  </button>
</div>

<!-- Latest SITREP -->
<div id="brief-latest" style="padding: 16px; background: var(--bg-secondary);
  border-radius: 0; border-left: 4px solid var(--accent-blue); margin-bottom: 20px;
  font-family: monospace; font-size: 0.9em; line-height: 1.6; white-space: pre-wrap;
  color: var(--text-secondary);">
  No SITREPs generated yet. Click "Generate SITREP Now" or wait for automatic generation.
</div>

<!-- SITREP History -->
<h3 style="margin-bottom: 12px;">SITREP History</h3>
<div id="brief-history" style="font-size: 0.85em;">
  <div style="color: var(--text-muted); padding: 12px 0;">Loading...</div>
</div>
"""

_TAB_JS = """
// Brief polling
async function briefPoll() {
  try {
    var statsRes = await fetch('/api/brief/stats');
    var stats = await statsRes.json();
    document.getElementById('brief-events').textContent = stats.total_events || 0;
    document.getElementById('brief-sitreps').textContent = stats.total_sitreps || 0;
    document.getElementById('brief-interval').textContent = (stats.interval_minutes || 60) + 'm';
    document.getElementById('brief-last-age').textContent = (stats.last_sitrep_age || 0) + 'm';

    var latestRes = await fetch('/api/brief/latest');
    var latestData = await latestRes.json();
    var el = document.getElementById('brief-latest');
    if (latestData.sitrep) {
      el.textContent = latestData.sitrep.content;
    }

    var histRes = await fetch('/api/brief/history');
    var histData = await histRes.json();
    var histEl = document.getElementById('brief-history');
    if (histData.history && histData.history.length > 0) {
      histEl.innerHTML = histData.history.map(function(s) {
        var ts = new Date(s.generated_at * 1000).toLocaleString();
        return '<div style="display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid var(--border); cursor: pointer;" onclick="briefView(\\'' + s.sitrep_id + '\\')">' +
          '<span>' + ts + '</span>' +
          '<span style="color: var(--text-muted);">' + s.node_count + ' nodes, ' + s.message_count + ' events — ' + s.age + '</span>' +
          '</div>';
      }).join('');
    } else {
      histEl.innerHTML = '<div style="color: var(--text-muted); padding: 8px 0;">No SITREPs generated yet.</div>';
    }
  } catch(e) {
    console.log('Brief poll error:', e);
  }
}

async function briefGenerate() {
  document.getElementById('brief-latest').textContent = 'Generating SITREP...';
  try {
    var res = await fetch('/api/brief/generate', {method: 'POST'});
    var data = await res.json();
    document.getElementById('brief-latest').textContent = data.content || 'Generation failed';
    showToast('SITREP generated');
    briefPoll();
  } catch(e) {
    showToast('Generation failed: ' + e, true);
  }
}

async function briefExport(fmt) {
  try {
    var res = await fetch('/api/brief/export', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({format: fmt})
    });
    var data = await res.json();
    if (data.path) {
      showToast('Exported to: ' + data.path);
    } else {
      showToast('Export failed: ' + (data.error || 'Unknown'), true);
    }
  } catch(e) {
    showToast('Export failed: ' + e, true);
  }
}

async function briefView(sitrep_id) {
  try {
    var res = await fetch('/api/brief/view?id=' + sitrep_id);
    var data = await res.json();
    if (data.sitrep) {
      document.getElementById('brief-latest').textContent = data.sitrep.content;
    }
  } catch(e) {
    console.log('Brief view error:', e);
  }
}

briefPoll();
setInterval(briefPoll, 10000);
"""
