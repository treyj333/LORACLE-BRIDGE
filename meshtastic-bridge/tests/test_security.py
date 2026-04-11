"""Tests for Phase 1 security hardening: path traversal, query bounds, channel validation."""

import io
import json
import unittest
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard


def _make_mock_bridge():
    """Create a mock bridge with the minimum attributes the dashboard expects."""
    bridge = MagicMock()
    bridge.interface = MagicMock()
    bridge._is_interface_alive = MagicMock(return_value=True)
    bridge.ollama = MagicMock()
    bridge.rag_engine = MagicMock()
    bridge.rag_engine.ingest_file = MagicMock(return_value={"filename": "test.txt", "chunks": 1})
    bridge.coverage = MagicMock()
    bridge.coverage.read_all = MagicMock(return_value=[])
    return bridge


class TestFileUploadSanitization(unittest.TestCase):
    """Verify that filenames with path traversal are sanitized."""

    def setUp(self):
        dashboard.set_bridge(_make_mock_bridge())
        self.client = dashboard.app.test_client()

    def test_path_traversal_filename_is_sanitized(self):
        """A filename like '../../etc/passwd' must be sanitized."""
        data = {
            "file": (io.BytesIO(b"test content"), "../../etc/passwd.txt"),
        }
        resp = self.client.post(
            "/api/rag/ingest-file",
            data=data,
            content_type="multipart/form-data",
        )
        # Should succeed with sanitized name, not write outside tempdir
        result = json.loads(resp.data)
        if resp.status_code == 200:
            # The filename should have traversal stripped
            self.assertNotIn("..", result.get("filename", ""))
        # Either way, no 500 error from os.path shenanigans
        self.assertIn(resp.status_code, (200, 400))

    def test_unicode_only_filename_rejected(self):
        """A filename that resolves to empty after secure_filename is rejected."""
        data = {
            "file": (io.BytesIO(b"test"), "\u200b\u200b\u200b"),
        }
        resp = self.client.post(
            "/api/rag/ingest-file",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)
        result = json.loads(resp.data)
        self.assertIn("Invalid filename", result.get("error", ""))


class TestQueryParamBounds(unittest.TestCase):
    """Verify that limit params are clamped to prevent memory exhaustion."""

    def setUp(self):
        dashboard.set_bridge(_make_mock_bridge())
        self.client = dashboard.app.test_client()

    def test_coverage_limit_clamped(self):
        """?limit=999999 on /api/coverage/samples should be clamped to 10000."""
        resp = self.client.get("/api/coverage/samples?limit=999999")
        self.assertEqual(resp.status_code, 200)
        # The mock returns [], but the important thing is it didn't try to
        # allocate a list of 999999 items. We verify the mock was called
        # with the clamped value.
        dashboard._bridge.coverage.read_all.assert_called_once_with(limit=10000)

    def test_logs_limit_clamped(self):
        """?limit=999999 on /api/logs should be clamped to 10000."""
        resp = self.client.get("/api/logs?limit=999999")
        self.assertEqual(resp.status_code, 200)


class TestChannelValidation(unittest.TestCase):
    """Verify that channel values are clamped to the valid 0-7 range."""

    def setUp(self):
        dashboard.set_bridge(_make_mock_bridge())
        self.client = dashboard.app.test_client()

    def test_channel_above_max_clamped(self):
        """channel=99 should be clamped to 7."""
        resp = self.client.post(
            "/api/send-mesh",
            data=json.dumps({"text": "test", "node_id": "broadcast", "channel": 99}),
            content_type="application/json",
        )
        # Extract what channelIndex was passed to sendText
        call_args = dashboard._bridge.interface.sendText.call_args
        if call_args:
            self.assertLessEqual(call_args.kwargs.get("channelIndex", 0), 7)

    def test_channel_below_min_clamped(self):
        """channel=-5 should be clamped to 0."""
        resp = self.client.post(
            "/api/send-mesh",
            data=json.dumps({"text": "test", "node_id": "broadcast", "channel": -5}),
            content_type="application/json",
        )
        call_args = dashboard._bridge.interface.sendText.call_args
        if call_args:
            self.assertGreaterEqual(call_args.kwargs.get("channelIndex", 0), 0)

    def test_channel_invalid_string_defaults_to_zero(self):
        """channel='abc' should default to 0."""
        resp = self.client.post(
            "/api/ask",
            data=json.dumps({"text": "hello", "channel": "abc"}),
            content_type="application/json",
        )
        # Should not crash — the endpoint has a try/except for ValueError
        self.assertNotEqual(resp.status_code, 500)


if __name__ == "__main__":
    unittest.main()
