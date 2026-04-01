"""Dead Drop dashboard tab — HTML/JS and API routes."""

import time

from flask import jsonify


def get_tab_config() -> dict:
    """Return the dashboard tab configuration for Dead Drop."""
    return {
        "id": "dead_drop",
        "label": "Dead Drop",
        "html": _TAB_HTML,
        "js": _TAB_JS,
    }


def get_api_routes(addon):
    """Return Flask API routes for Dead Drop."""

    def api_stats():
        stats = addon.store.get_stats()
        return jsonify(stats)

    def api_messages():
        messages = addon.store.get_recent_messages(limit=50)
        # Convert timestamps to relative strings
        now = time.time()
        for msg in messages:
            age = now - msg["created_at"]
            if age < 3600:
                msg["age"] = f"{int(age / 60)}m ago"
            elif age < 86400:
                msg["age"] = f"{int(age / 3600)}h ago"
            else:
                msg["age"] = f"{int(age / 86400)}d ago"
        return jsonify({"messages": messages})

    def api_purge():
        count = addon.store.purge_expired()
        return jsonify({"purged": count})

    return [
        ("GET", "/api/dead_drop/stats", api_stats),
        ("GET", "/api/dead_drop/messages", api_messages),
        ("POST", "/api/dead_drop/purge", api_purge),
    ]


_TAB_HTML = """
<h2>Dead Drop — Encrypted Async Messages</h2>
<p style="color: var(--text-muted); margin-bottom: 16px;">
  Encrypted store-and-forward messaging. Nodes leave messages that get picked up on reconnect.
</p>

<div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 20px;">
  <div class="stat-card">
    <div class="stat-label">Pending</div>
    <div class="stat-value" id="dd-pending">—</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Delivered</div>
    <div class="stat-value" id="dd-delivered">—</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Expired</div>
    <div class="stat-value" id="dd-expired">—</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Registered Nodes</div>
    <div class="stat-value" id="dd-nodes">—</div>
  </div>
</div>

<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
  <h3 style="margin: 0;">Recent Messages</h3>
  <button onclick="ddPurge()" style="background: var(--accent-red); color: white; border: none; padding: 6px 14px; border-radius: 0; font-size: 0.8em; cursor: pointer;">
    Purge Expired
  </button>
</div>

<div style="overflow-x: auto;">
  <table style="width: 100%; border-collapse: collapse; font-size: 0.85em;">
    <thead>
      <tr style="border-bottom: 1px solid var(--border); text-align: left;">
        <th style="padding: 8px;">Status</th>
        <th style="padding: 8px;">From</th>
        <th style="padding: 8px;">To</th>
        <th style="padding: 8px;">Age</th>
        <th style="padding: 8px;">ID</th>
      </tr>
    </thead>
    <tbody id="dd-messages-body">
      <tr><td colspan="5" style="padding: 20px; text-align: center; color: var(--text-muted);">Loading...</td></tr>
    </tbody>
  </table>
</div>

<div style="margin-top: 20px; padding: 14px; background: var(--bg-secondary); border-radius: 0;">
  <h3 style="margin: 0 0 8px 0; font-size: 0.9em;">Mesh Commands</h3>
  <div style="font-size: 0.82em; color: var(--text-secondary); line-height: 1.6;">
    <code>!drop-key &lt;passphrase&gt;</code> — Register your encryption key<br>
    <code>!drop &lt;node_id&gt; &lt;message&gt;</code> — Leave encrypted message<br>
    <code>!pickup</code> — Retrieve your pending messages<br>
    <code>!pending</code> — Check waiting message count
  </div>
</div>
"""

_TAB_JS = """
// Dead Drop polling
async function ddPoll() {
  try {
    var statsRes = await fetch('/api/dead_drop/stats');
    var stats = await statsRes.json();
    document.getElementById('dd-pending').textContent = stats.pending || 0;
    document.getElementById('dd-delivered').textContent = stats.delivered || 0;
    document.getElementById('dd-expired').textContent = stats.expired || 0;
    document.getElementById('dd-nodes').textContent = stats.registered_nodes || 0;

    var msgsRes = await fetch('/api/dead_drop/messages');
    var data = await msgsRes.json();
    var tbody = document.getElementById('dd-messages-body');
    if (data.messages && data.messages.length > 0) {
      tbody.innerHTML = data.messages.map(function(m) {
        var statusColor = m.status === 'pending' ? 'var(--accent-yellow)' :
                          m.status === 'delivered' ? 'var(--accent-green)' : 'var(--text-muted)';
        return '<tr style="border-bottom: 1px solid var(--border);">' +
          '<td style="padding: 8px;"><span style="color:' + statusColor + ';">' + m.status + '</span></td>' +
          '<td style="padding: 8px; font-family: monospace; font-size: 0.85em;">' + m.sender_node + '</td>' +
          '<td style="padding: 8px; font-family: monospace; font-size: 0.85em;">' + m.recipient_node + '</td>' +
          '<td style="padding: 8px;">' + m.age + '</td>' +
          '<td style="padding: 8px; font-family: monospace; font-size: 0.8em; color: var(--text-muted);">' + m.msg_id + '</td>' +
          '</tr>';
      }).join('');
    } else {
      tbody.innerHTML = '<tr><td colspan="5" style="padding: 20px; text-align: center; color: var(--text-muted);">No messages yet. Use !drop from a mesh node.</td></tr>';
    }
  } catch(e) {
    console.log('Dead Drop poll error:', e);
  }
}

async function ddPurge() {
  try {
    await fetch('/api/dead_drop/purge', {method: 'POST'});
    ddPoll();
    showToast('Expired messages purged');
  } catch(e) {
    showToast('Purge failed: ' + e, true);
  }
}

ddPoll();
setInterval(ddPoll, 5000);
"""
