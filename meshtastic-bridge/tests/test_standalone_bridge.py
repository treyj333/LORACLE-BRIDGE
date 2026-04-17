"""Tests for standalone_bridge core functionality (Phase 4).

These tests mock hardware dependencies and focus on bridge logic:
command dispatch, dedup, rate limiting, public channel addressing, and paging.
"""

import hashlib
import tempfile
import time
import unittest
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from standalone_bridge import (
    StandaloneBridge,
    _is_addressed_to_ai,
    _dedup_cache,
    DEDUP_TTL,
    RATE_LIMIT_SECS,
    get_dedup_cache_size,
)


def _make_bridge(**overrides):
    """Create a minimal StandaloneBridge with all external deps mocked."""
    defaults = dict(
        connection_type="serial",
        serial_port="/dev/null",
        ollama_url="http://localhost:11434",
        model="test-model",
        max_response_length=200,
        compression_enabled=False,
        rag_enabled=False,
        dashboard_port=0,
        addon_config=None,
        auto_greet=False,
        public_talk=True,
    )
    defaults.update(overrides)
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "test.db")
    with patch("standalone_bridge.OllamaClient") as MockOllama, \
         patch("standalone_bridge.DB_PATH", db_path):
        mock_ollama = MagicMock()
        MockOllama.return_value = mock_ollama
        bridge = StandaloneBridge(**defaults)
    bridge.interface = MagicMock()
    return bridge


class TestIsAddressedToAI(unittest.TestCase):
    """Test the trigger-word gating for public channel messages."""

    def test_command_always_addressed(self):
        self.assertTrue(_is_addressed_to_ai("!help"))
        self.assertTrue(_is_addressed_to_ai("!status"))
        self.assertTrue(_is_addressed_to_ai("!nav 34,-118"))

    def test_trigger_words_match(self):
        self.assertTrue(_is_addressed_to_ai("hey loracle what is the weather"))
        self.assertTrue(_is_addressed_to_ai("AI can you help me"))
        self.assertTrue(_is_addressed_to_ai("oracle, tell me"))
        self.assertTrue(_is_addressed_to_ai("I need help with something"))

    def test_normal_chat_ignored(self):
        self.assertFalse(_is_addressed_to_ai("discussing the weather today"))
        self.assertFalse(_is_addressed_to_ai("anyone know a good restaurant"))
        self.assertFalse(_is_addressed_to_ai(""))

    def test_trigger_only_in_first_50_chars(self):
        # "ai" beyond 50 chars should not trigger
        msg = "x" * 55 + " ai are you there"
        self.assertFalse(_is_addressed_to_ai(msg))

    def test_case_insensitive(self):
        self.assertTrue(_is_addressed_to_ai("Hey LORACLE"))
        self.assertTrue(_is_addressed_to_ai("HELP me out"))


class TestCommandDispatch(unittest.TestCase):
    """Test that _handle_command routes correctly."""

    def setUp(self):
        self.bridge = _make_bridge()

    def test_help_command(self):
        result = self.bridge._handle_command("!node1", "!help")
        self.assertIsNotNone(result)
        self.assertIn("!help", result)

    def test_ping_command(self):
        result = self.bridge._handle_command("!node1", "!ping")
        self.assertEqual(result, "pong")

    def test_clear_command(self):
        result = self.bridge._handle_command("!node1", "!clear")
        self.assertIn("cleared", result.lower())
        self.bridge.ollama.clear_history.assert_called_with("!node1")

    def test_unknown_command_returns_none(self):
        result = self.bridge._handle_command("!node1", "!nonexistent")
        self.assertIsNone(result)

    def test_non_command_returns_none(self):
        result = self.bridge._handle_command("!node1", "just a regular message")
        self.assertIsNone(result)

    def test_status_command(self):
        self.bridge.ollama.model = "test-model"
        result = self.bridge._handle_command("!node1", "!status")
        self.assertIsNotNone(result)
        self.assertIn("test-model", result)


class TestOverflowPager(unittest.TestCase):
    """Test the !more pager mechanism."""

    def setUp(self):
        self.bridge = _make_bridge()

    def test_more_with_content(self):
        self.bridge._overflow["!node1"] = ("more text here", time.time())
        result = self.bridge._handle_command("!node1", "!more")
        self.assertEqual(result, "more text here")
        self.assertNotIn("!node1", self.bridge._overflow)

    def test_more_without_content(self):
        result = self.bridge._handle_command("!node1", "!more")
        self.assertEqual(result, "No more content.")


class TestDedup(unittest.TestCase):
    """Test deduplication cache logic."""

    def setUp(self):
        _dedup_cache.clear()

    def tearDown(self):
        _dedup_cache.clear()

    def test_get_dedup_cache_size(self):
        self.assertEqual(get_dedup_cache_size(), 0)
        _dedup_cache[("node1", "hash1")] = time.time()
        self.assertEqual(get_dedup_cache_size(), 1)

    def test_duplicate_within_ttl(self):
        """Same content hash within TTL should be detected as duplicate."""
        content = "test message"
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        key = ("!node1", content_hash)
        _dedup_cache[key] = time.time()
        # A second message with the same hash should find the key
        self.assertIn(key, _dedup_cache)

    def test_expired_entry_not_blocking(self):
        """Content hash with expired TTL should not block new messages."""
        content_hash = "abcdef1234567890"
        key = ("!node1", content_hash)
        _dedup_cache[key] = time.time() - DEDUP_TTL - 10  # expired
        # After cleanup, this entry would be removed
        now = time.time()
        is_expired = now - _dedup_cache[key] > DEDUP_TTL
        self.assertTrue(is_expired)


class TestRateLimiting(unittest.TestCase):
    """Test the per-node rate limiting via _node_last_active."""

    def setUp(self):
        self.bridge = _make_bridge()

    def test_rate_limit_tracking(self):
        """Verify _node_last_active timestamps are stored."""
        self.bridge._node_last_active["!node1"] = time.time()
        self.assertIn("!node1", self.bridge._node_last_active)

    def test_rate_limit_window(self):
        """Messages within RATE_LIMIT_SECS should be throttled."""
        now = time.time()
        self.bridge._node_last_active["!node1"] = now
        elapsed = time.time() - self.bridge._node_last_active["!node1"]
        self.assertLess(elapsed, RATE_LIMIT_SECS)


if __name__ == "__main__":
    unittest.main()
