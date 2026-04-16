"""Tests for dashboard API endpoints (Phase 4)."""

import json
import unittest
import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard


def _make_mock_bridge():
    """Create a mock bridge with minimum dashboard-expected attributes."""
    bridge = MagicMock()
    bridge.interface = MagicMock()
    bridge._is_interface_alive = MagicMock(return_value=True)
    bridge.ollama = MagicMock()
    bridge.ollama.model = "test-model"
    bridge.ollama.chat = MagicMock(return_value="AI response here")
    bridge.ollama.clear_all_history = MagicMock(return_value=3)
    bridge.rag_engine = MagicMock()
    bridge.rag_engine.ingest_file = MagicMock(return_value={"filename": "test.txt", "chunks": 5})
    bridge.rag_enabled = True
    bridge.coverage = MagicMock()
    bridge.coverage.read_all = MagicMock(return_value=[])
    bridge._request_queue = MagicMock()
    bridge._request_queue.qsize = MagicMock(return_value=0)
    bridge._start_time = 0
    bridge._message_count = 42
    bridge._node_count = 5
    bridge._known_nodes = {"!abc", "!def"}
    bridge._node_positions = {}
    bridge._node_meta = {}
    bridge.interface.nodes = {}
    bridge.greeter = MagicMock()
    bridge.greeter.stats = MagicMock(return_value={"enabled": True, "greeted_count": 0})
    bridge._radio_manager = MagicMock()
    bridge._radio_manager.get_backends_info = MagicMock(return_value=[
        {"id": "mt-serial-0", "protocol": "mt", "transport": "serial", "connected": True}
    ])
    bridge._ai_replies_enabled = True
    return bridge


class TestStatusEndpoint(unittest.TestCase):
    def setUp(self):
        dashboard.set_bridge(_make_mock_bridge())
        self.client = dashboard.app.test_client()

    def test_status_returns_json(self):
        resp = self.client.get("/api/state")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("connected", data)
        self.assertIn("model", data)

    def test_status_no_bridge(self):
        dashboard._bridge = None
        resp = self.client.get("/api/state")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        # Should still return state dict even without bridge
        self.assertIn("connected", data)


class TestSendMeshEndpoint(unittest.TestCase):
    def setUp(self):
        dashboard.set_bridge(_make_mock_bridge())
        self.client = dashboard.app.test_client()

    def test_send_empty_text_rejected(self):
        resp = self.client.post(
            "/api/send-mesh",
            data=json.dumps({"text": "", "node_id": "broadcast"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_send_broadcast(self):
        resp = self.client.post(
            "/api/send-mesh",
            data=json.dumps({"text": "hello mesh", "node_id": "broadcast"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data.get("ok"))
        self.assertIn("broadcast", data.get("direction", ""))

    def test_send_no_bridge(self):
        dashboard._bridge = None
        resp = self.client.post(
            "/api/send-mesh",
            data=json.dumps({"text": "test"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 503)


class TestAskEndpoint(unittest.TestCase):
    def setUp(self):
        dashboard.set_bridge(_make_mock_bridge())
        self.client = dashboard.app.test_client()

    def test_ask_empty_text(self):
        resp = self.client.post(
            "/api/ask",
            data=json.dumps({"text": ""}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_ask_local_query(self):
        resp = self.client.post(
            "/api/ask",
            data=json.dumps({"text": "What is the meaning of life?", "dest": "local"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data.get("ok"))
        self.assertIn("answer", data)


class TestClearHistoryEndpoint(unittest.TestCase):
    def setUp(self):
        dashboard.set_bridge(_make_mock_bridge())
        self.client = dashboard.app.test_client()

    def test_clear_all_history(self):
        resp = self.client.post(
            "/api/clear-history",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data.get("ok"))
        dashboard._bridge.ollama.clear_all_history.assert_called_once()

    def test_clear_single_node(self):
        resp = self.client.post(
            "/api/clear-history",
            data=json.dumps({"node_id": "!abc123"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        dashboard._bridge.ollama.clear_history.assert_called_with("!abc123")


class TestLogsEndpoint(unittest.TestCase):
    def setUp(self):
        dashboard.set_bridge(_make_mock_bridge())
        self.client = dashboard.app.test_client()

    def test_logs_returns_json(self):
        resp = self.client.get("/api/logs")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("logs", data)


if __name__ == "__main__":
    unittest.main()
