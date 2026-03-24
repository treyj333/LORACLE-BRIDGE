"""Simple health check HTTP server."""

import threading
import logging
from flask import Flask, jsonify

logger = logging.getLogger(__name__)

app = Flask(__name__)

# Shared state for health reporting
_status = {
    "radio_connected": False,
    "nomad_reachable": False,
    "active_nodes": 0,
}


def update_status(radio_connected=None, nomad_reachable=None, active_nodes=None):
    """Update health status from the bridge main loop."""
    if radio_connected is not None:
        _status["radio_connected"] = radio_connected
    if nomad_reachable is not None:
        _status["nomad_reachable"] = nomad_reachable
    if active_nodes is not None:
        _status["active_nodes"] = active_nodes


@app.route("/health")
def health():
    """Health check endpoint for Docker."""
    healthy = _status["radio_connected"] and _status["nomad_reachable"]
    status_code = 200 if healthy else 503
    return jsonify({"status": "ok" if healthy else "degraded", **_status}), status_code


@app.route("/status")
def status():
    """Detailed status endpoint."""
    return jsonify(_status)


def start_health_server(port=5275):
    """Start the health check server in a background thread."""
    def run():
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    logger.info(f"Health server started on port {port}")
