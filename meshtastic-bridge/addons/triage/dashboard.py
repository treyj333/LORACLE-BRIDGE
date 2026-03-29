"""Triage dashboard tab — minimal high-contrast medical reference UI."""

import os

from flask import jsonify, request


def get_tab_config() -> dict:
    return {
        "id": "triage",
        "label": "Triage",
        "html": _TAB_HTML,
        "js": _TAB_JS,
    }


def get_api_routes(addon):
    """Return Flask API routes for Triage."""

    def api_status():
        if not addon.rag_engine:
            return jsonify({"available": False, "docs": 0, "chunks": 0})
        stats = addon.rag_engine.get_stats()
        return jsonify({
            "available": True,
            "docs": stats["total_docs"],
            "chunks": stats["total_chunks"],
            "data_dir": addon.data_dir,
        })

    def api_documents():
        if not addon.rag_engine:
            return jsonify({"documents": []})
        docs = addon.rag_engine.list_documents()
        return jsonify({"documents": docs})

    def api_query():
        data = request.get_json(silent=True) or {}
        question = data.get("question", "").strip()
        if not question:
            return jsonify({"error": "No question provided"}), 400
        response = addon._cmd_triage("dashboard", question)
        return jsonify({"response": response})

    def api_ingest_url():
        data = request.get_json(silent=True) or {}
        url = data.get("url", "").strip()
        if not url:
            return jsonify({"error": "No URL provided"}), 400
        if not addon.rag_engine:
            return jsonify({"error": "Triage RAG not available"}), 503
        try:
            from rag.extractors import extract_url
            text = extract_url(url)
            if not text or len(text) < 50:
                return jsonify({"error": "Could not extract useful content"}), 400

            from rag.chunker import chunk_text
            import hashlib
            doc_id = hashlib.md5(url.encode()).hexdigest()[:12]
            filename = url.split("/")[-1][:80] or "web_page"

            chunks = chunk_text(text)
            addon.rag_engine.ingest_chunks(doc_id, filename, url, "url", chunks)
            stats = addon.rag_engine.get_stats()
            return jsonify({
                "ok": True,
                "doc_id": doc_id,
                "filename": filename,
                "chunks": len(chunks),
                "total_docs": stats["total_docs"],
            })
        except ImportError:
            return jsonify({"error": "URL extraction not available"}), 503
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    def api_delete_doc():
        data = request.get_json(silent=True) or {}
        doc_id = data.get("doc_id", "").strip()
        if not doc_id:
            return jsonify({"error": "No doc_id provided"}), 400
        if not addon.rag_engine:
            return jsonify({"error": "Triage RAG not available"}), 503
        addon.rag_engine.delete_document(doc_id)
        return jsonify({"ok": True})

    return [
        ("GET", "/api/triage/status", api_status),
        ("GET", "/api/triage/documents", api_documents),
        ("POST", "/api/triage/query", api_query),
        ("POST", "/api/triage/ingest-url", api_ingest_url),
        ("POST", "/api/triage/delete", api_delete_doc),
    ]


_TAB_HTML = """
<div style="max-width: 800px; margin: 0 auto;">
  <h2 style="color: #ff4444; margin-bottom: 4px;">LORACLE TRIAGE</h2>
  <p style="color: var(--text-muted); margin-bottom: 20px; font-size: 0.85em;">
    Offline Medical Reference — TCCC &amp; Field Medicine
  </p>

  <!-- Query Section -->
  <div style="margin-bottom: 24px;">
    <div style="display: flex; gap: 8px;">
      <input type="text" id="triage-query" placeholder="e.g. how to apply a tourniquet"
        style="flex: 1; padding: 12px 16px; font-size: 1.1em; background: var(--bg-secondary);
        border: 2px solid var(--border); border-radius: 8px; color: var(--text-primary);"
        onkeydown="if(event.key==='Enter')triageSearch()">
      <button onclick="triageSearch()"
        style="background: #ff4444; color: white; border: none; padding: 12px 24px;
        border-radius: 8px; font-size: 1em; font-weight: 600; cursor: pointer;">
        Search
      </button>
    </div>
  </div>

  <!-- Result Section -->
  <div id="triage-result" style="display: none; padding: 16px; background: var(--bg-secondary);
    border-radius: 8px; border-left: 4px solid #ff4444; margin-bottom: 24px;
    font-size: 1.05em; line-height: 1.6; white-space: pre-wrap;">
  </div>
  <div id="triage-loading" style="display: none; padding: 20px; text-align: center;
    color: var(--text-muted);">
    Searching medical references...
  </div>

  <!-- Stats -->
  <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 20px;">
    <div class="stat-card">
      <div class="stat-label">Medical Docs</div>
      <div class="stat-value" id="triage-docs">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Knowledge Chunks</div>
      <div class="stat-value" id="triage-chunks">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Status</div>
      <div class="stat-value" id="triage-status-val" style="font-size: 0.8em;">—</div>
    </div>
  </div>

  <!-- Document Management -->
  <details style="margin-top: 16px;">
    <summary style="cursor: pointer; font-weight: 600; padding: 8px 0;">
      Medical Knowledge Base Management
    </summary>
    <div style="padding: 12px 0;">
      <div style="display: flex; gap: 8px; margin-bottom: 12px;">
        <input type="text" id="triage-ingest-url" placeholder="URL to medical reference..."
          style="flex: 1; padding: 8px 12px; background: var(--bg-secondary);
          border: 1px solid var(--border); border-radius: 6px; color: var(--text-primary);">
        <button onclick="triageIngestUrl()"
          style="background: var(--accent-blue); color: white; border: none;
          padding: 8px 16px; border-radius: 6px; cursor: pointer;">
          Ingest URL
        </button>
      </div>
      <div id="triage-doc-list" style="font-size: 0.85em;"></div>
    </div>
  </details>

  <div style="margin-top: 20px; padding: 10px 14px; background: rgba(255,68,68,0.1);
    border-radius: 6px; font-size: 0.8em; color: #ff6666; text-align: center;">
    NOT a substitute for professional medical care — seek qualified help
  </div>
</div>
"""

_TAB_JS = """
// Triage polling
async function triagePoll() {
  try {
    var res = await fetch('/api/triage/status');
    var data = await res.json();
    document.getElementById('triage-docs').textContent = data.docs || 0;
    document.getElementById('triage-chunks').textContent = data.chunks || 0;
    document.getElementById('triage-status-val').textContent = data.available ? 'Ready' : 'No RAG';

    var docsRes = await fetch('/api/triage/documents');
    var docsData = await docsRes.json();
    var list = document.getElementById('triage-doc-list');
    if (docsData.documents && docsData.documents.length > 0) {
      list.innerHTML = docsData.documents.map(function(d) {
        return '<div style="display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid var(--border);">' +
          '<span>' + d.filename + ' (' + d.chunk_count + ' chunks)</span>' +
          '<button onclick="triageDeleteDoc(\\'' + d.doc_id + '\\')" style="background: none; border: none; color: var(--accent-red); cursor: pointer; font-size: 0.85em;">Delete</button>' +
          '</div>';
      }).join('');
    } else {
      list.innerHTML = '<div style="color: var(--text-muted); padding: 8px 0;">No medical documents loaded. Add TCCC PDFs to get started.</div>';
    }
  } catch(e) {
    console.log('Triage poll error:', e);
  }
}

async function triageSearch() {
  var query = document.getElementById('triage-query').value.trim();
  if (!query) return;
  document.getElementById('triage-loading').style.display = 'block';
  document.getElementById('triage-result').style.display = 'none';
  try {
    var res = await fetch('/api/triage/query', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question: query})
    });
    var data = await res.json();
    document.getElementById('triage-result').textContent = data.response || data.error || 'No response';
    document.getElementById('triage-result').style.display = 'block';
  } catch(e) {
    document.getElementById('triage-result').textContent = 'Error: ' + e;
    document.getElementById('triage-result').style.display = 'block';
  }
  document.getElementById('triage-loading').style.display = 'none';
}

async function triageIngestUrl() {
  var url = document.getElementById('triage-ingest-url').value.trim();
  if (!url) return;
  try {
    var res = await fetch('/api/triage/ingest-url', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: url})
    });
    var data = await res.json();
    if (data.ok) {
      showToast('Ingested: ' + data.filename + ' (' + data.chunks + ' chunks)');
      document.getElementById('triage-ingest-url').value = '';
      triagePoll();
    } else {
      showToast('Ingest failed: ' + (data.error || 'Unknown error'), true);
    }
  } catch(e) {
    showToast('Ingest error: ' + e, true);
  }
}

async function triageDeleteDoc(docId) {
  try {
    await fetch('/api/triage/delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({doc_id: docId})
    });
    showToast('Document deleted');
    triagePoll();
  } catch(e) {
    showToast('Delete failed: ' + e, true);
  }
}

triagePoll();
setInterval(triagePoll, 10000);
"""
