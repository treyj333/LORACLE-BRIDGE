"""Triage dashboard tab — minimal high-contrast medical reference UI."""

import os

from flask import jsonify, request
from werkzeug.utils import secure_filename


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

    def api_ingest_file():
        if not addon.rag_engine:
            return jsonify({"error": "Triage RAG not available"}), 503
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
            result = addon.rag_engine.ingest_file(tmp_path)
            return jsonify({"ok": True, "filename": result.get("filename", safe_name),
                            "chunks": result.get("chunks", 0)})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    return [
        ("GET", "/api/triage/status", api_status),
        ("GET", "/api/triage/documents", api_documents),
        ("POST", "/api/triage/query", api_query),
        ("POST", "/api/triage/ingest-url", api_ingest_url),
        ("POST", "/api/triage/ingest-file", api_ingest_file),
        ("POST", "/api/triage/delete", api_delete_doc),
    ]


_TAB_HTML = """
<div style="max-width: 800px; margin: 0 auto;">
  <h2 style="color: var(--accent-red); margin-bottom: 4px;">LORACLE TRIAGE</h2>
  <div style="padding:6px 12px;background:rgba(255,51,51,0.1);border-left:3px solid var(--accent-red);margin-bottom:16px;font-size:0.8em;color:var(--accent-red)">
    Offline Medical Reference — NOT a substitute for professional medical care
  </div>

  <!-- Empty State: No docs loaded -->
  <div id="triage-empty-state" style="display:none;padding:20px;background:var(--bg-secondary);border:var(--border-width) solid var(--border);border-top:3px solid var(--accent-yellow);margin-bottom:20px">
    <div style="font-size:0.95em;color:var(--text-primary);margin-bottom:12px">Load medical references to begin</div>
    <div style="font-size:0.82em;color:var(--text-muted);margin-bottom:16px">
      Upload TCCC handbooks, field medicine PDFs, or add URLs to medical references.
    </div>
    <div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap">
      <input type="file" id="triage-file-empty" accept=".pdf,.txt,.md,.zim"
        style="font-size:0.82em;color:var(--text-secondary);flex:1;min-width:150px">
      <button class="btn" onclick="triageUploadFile('triage-file-empty')">Upload PDF</button>
    </div>
    <div style="display:flex;gap:8px">
      <input type="url" id="triage-url-empty" placeholder="https://example.com/medical-reference"
        style="flex:1;min-width:0;background:var(--bg-input);border:1px solid var(--border);color:var(--text-primary);padding:8px 10px;font-size:0.85em">
      <button class="btn" onclick="triageIngestUrl('triage-url-empty')">Add URL</button>
    </div>
  </div>

  <!-- Loaded State: Search + Results -->
  <div id="triage-loaded-state" style="display:none">
    <!-- Search -->
    <div style="display:flex;gap:8px;margin-bottom:16px">
      <input type="text" id="triage-query" placeholder="e.g. how to apply a tourniquet"
        style="flex:1;min-width:0;padding:10px 14px;font-size:1em;background:var(--bg-secondary);
        border:2px solid var(--border);color:var(--text-primary)"
        onkeydown="if(event.key==='Enter')triageSearch()">
      <button onclick="triageSearch()"
        style="background:var(--accent-red);color:#0a0f06;border:2px solid var(--border);padding:10px 20px;font-size:0.95em;cursor:pointer;box-shadow:var(--shadow-raised)">
        Search
      </button>
    </div>

    <!-- Result -->
    <div id="triage-result" style="display:none;padding:14px;background:var(--bg-secondary);
      border-left:4px solid var(--accent-red);margin-bottom:16px;
      font-size:0.95em;line-height:1.6;white-space:pre-wrap;text-transform:none">
    </div>
    <div id="triage-loading" style="display:none;padding:16px;text-align:center;color:var(--text-muted)">
      Searching medical references...
    </div>

    <!-- Stats -->
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px">
      <div class="stat-card">
        <div class="stat-label">References</div>
        <div class="stat-value" id="triage-docs">0</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Sections</div>
        <div class="stat-value" id="triage-chunks">0</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Status</div>
        <div class="stat-value" id="triage-status-val" style="font-size:0.8em">—</div>
      </div>
    </div>

    <!-- Document Management (always visible when loaded) -->
    <div style="background:var(--bg-secondary);border:var(--border-width) solid var(--border);padding:14px;border-top:3px solid var(--accent-red)">
      <div style="font-size:0.82em;color:var(--text-muted);letter-spacing:1px;margin-bottom:10px">Manage References</div>
      <div style="display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap">
        <input type="file" id="triage-file-loaded" accept=".pdf,.txt,.md,.zim"
          style="font-size:0.82em;color:var(--text-secondary);flex:1;min-width:150px">
        <button class="btn btn-sm" onclick="triageUploadFile('triage-file-loaded')">Upload</button>
      </div>
      <div style="display:flex;gap:8px;margin-bottom:10px">
        <input type="url" id="triage-url-loaded" placeholder="https://..."
          style="flex:1;min-width:0;background:var(--bg-input);border:1px solid var(--border);color:var(--text-primary);padding:6px 10px;font-size:0.85em">
        <button class="btn btn-sm" onclick="triageIngestUrl('triage-url-loaded')">Add URL</button>
      </div>
      <div id="triage-doc-list" style="font-size:0.82em;max-height:250px;overflow-y:auto"></div>
    </div>
  </div>
</div>
"""

_TAB_JS = """
async function triagePoll() {
  try {
    var res = await fetch('/api/triage/status');
    var data = await res.json();
    var docCount = data.docs || 0;
    document.getElementById('triage-docs').textContent = docCount;
    document.getElementById('triage-chunks').textContent = data.chunks || 0;
    document.getElementById('triage-status-val').textContent = data.available ? 'Ready' : 'No RAG';

    // State-aware: show empty or loaded state
    document.getElementById('triage-empty-state').style.display = (docCount === 0) ? 'block' : 'none';
    document.getElementById('triage-loaded-state').style.display = (docCount > 0) ? 'block' : 'none';

    var docsRes = await fetch('/api/triage/documents');
    var docsData = await docsRes.json();
    var list = document.getElementById('triage-doc-list');
    if (docsData.documents && docsData.documents.length > 0) {
      list.innerHTML = docsData.documents.map(function(d) {
        return '<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border-subtle)">' +
          '<span style="color:var(--text-secondary)">' + escapeHtml(d.filename) + '</span>' +
          '<span style="display:flex;align-items:center;gap:8px">' +
            '<span style="color:var(--text-dim)">' + d.chunk_count + ' sections</span>' +
            '<button onclick="triageDeleteDoc(\\'' + d.doc_id + '\\')" ' +
              'style="background:none;border:1px solid var(--accent-red);color:var(--accent-red);padding:2px 8px;cursor:pointer;font-size:0.8em">x</button>' +
          '</span></div>';
      }).join('');
    } else {
      list.innerHTML = '<div style="color:var(--text-dim);padding:6px 0">No documents loaded</div>';
    }
  } catch(e) {}
}

async function triageSearch() {
  var query = document.getElementById('triage-query').value.trim();
  if (!query) return;
  document.getElementById('triage-loading').style.display = 'block';
  document.getElementById('triage-result').style.display = 'none';
  try {
    var res = await fetch('/api/triage/query', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
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

async function triageUploadFile(inputId) {
  var input = document.getElementById(inputId);
  if (!input || !input.files.length) return;
  var formData = new FormData();
  formData.append('file', input.files[0]);
  try {
    var res = await fetch('/api/triage/ingest-file', {method: 'POST', body: formData});
    var data = await res.json();
    if (data.ok) {
      showToast('Uploaded: ' + data.filename + ' (' + data.chunks + ' sections)');
      input.value = '';
      triagePoll();
    } else {
      showToast(data.error || 'Upload failed', 'error');
    }
  } catch(e) { showToast('Upload error', 'error'); }
}

async function triageIngestUrl(inputId) {
  var input = document.getElementById(inputId);
  var url = input ? input.value.trim() : '';
  if (!url) return;
  try {
    var res = await fetch('/api/triage/ingest-url', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: url})
    });
    var data = await res.json();
    if (data.ok) {
      showToast('Ingested: ' + data.filename + ' (' + data.chunks + ' sections)');
      input.value = '';
      triagePoll();
    } else {
      showToast(data.error || 'Ingest failed', 'error');
    }
  } catch(e) { showToast('Ingest error', 'error'); }
}

async function triageDeleteDoc(docId) {
  try {
    await fetch('/api/triage/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({doc_id: docId})
    });
    showToast('Deleted');
    triagePoll();
  } catch(e) { showToast('Delete failed', 'error'); }
}

triagePoll();
setInterval(triagePoll, 10000);
"""
