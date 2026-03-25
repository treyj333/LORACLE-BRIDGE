"""Tests for the standalone Ollama client."""

import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ollama_client import OllamaClient


class TestOllamaClient(unittest.TestCase):
    """Tests for OllamaClient."""

    def setUp(self):
        self.client = OllamaClient(
            base_url="http://localhost:11434",
            model="test-model",
            max_response_length=100,
            history_length=4,
        )

    @patch.object(OllamaClient, "_session", create=True)
    def _mock_chat_response(self, content, mock_session=None):
        """Helper to mock a chat response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "message": {"role": "assistant", "content": content}
        }
        mock_resp.raise_for_status = MagicMock()
        self.client._session.post = MagicMock(return_value=mock_resp)
        return mock_resp

    def test_chat_builds_correct_payload(self):
        """Chat should send correct JSON to Ollama API."""
        self._mock_chat_response("Hello back!")

        self.client.chat("node1", "Hello")

        call_args = self.client._session.post.call_args
        self.assertIn("/api/chat", call_args[0][0])
        body = call_args[1]["json"]
        self.assertEqual(body["model"], "test-model")
        self.assertFalse(body["stream"])
        # Should have system prompt + user message
        messages = body["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-1]["content"], "Hello")

    def test_chat_returns_response(self):
        """Chat should return the LLM response content."""
        self._mock_chat_response("The answer is 42.")
        result = self.client.chat("node1", "What is the answer?")
        self.assertEqual(result, "The answer is 42.")

    def test_chat_maintains_history(self):
        """Second call should include first exchange in messages."""
        self._mock_chat_response("First reply")
        self.client.chat("node1", "First question")

        self._mock_chat_response("Second reply")
        self.client.chat("node1", "Second question")

        call_args = self.client._session.post.call_args
        messages = call_args[1]["json"]["messages"]
        # system + history(user, assistant) + new user = 4 messages
        roles = [m["role"] for m in messages]
        self.assertEqual(roles[0], "system")
        self.assertEqual(roles[1], "user")
        self.assertEqual(roles[2], "assistant")
        self.assertEqual(roles[3], "user")
        self.assertEqual(messages[1]["content"], "First question")
        self.assertEqual(messages[2]["content"], "First reply")
        self.assertEqual(messages[3]["content"], "Second question")

    def test_chat_separate_node_history(self):
        """Different nodes should have independent history."""
        self._mock_chat_response("Reply to A")
        self.client.chat("nodeA", "Question from A")

        self._mock_chat_response("Reply to B")
        self.client.chat("nodeB", "Question from B")

        call_args = self.client._session.post.call_args
        messages = call_args[1]["json"]["messages"]
        # nodeB should only have system + its own message (no nodeA history)
        self.assertEqual(len(messages), 2)  # system + user
        self.assertEqual(messages[-1]["content"], "Question from B")

    def test_chat_truncates_long_response(self):
        """Responses exceeding max_response_length should be truncated."""
        long_text = "A" * 200  # max is 100
        self._mock_chat_response(long_text)

        result = self.client.chat("node1", "Tell me a lot")
        self.assertLessEqual(len(result), 100)
        self.assertTrue(result.endswith("...[truncated]"))

    def test_history_bounded(self):
        """History should not exceed maxlen."""
        for i in range(10):
            self._mock_chat_response(f"Reply {i}")
            self.client.chat("node1", f"Question {i}")

        # history_length=4, each exchange adds 2 entries, so 4 entries max
        self.assertEqual(len(self.client._history["node1"]), 4)

    def test_is_available_success(self):
        """is_available returns True when Ollama responds."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        self.client._session.get = MagicMock(return_value=mock_resp)

        self.assertTrue(self.client.is_available())

    def test_is_available_failure(self):
        """is_available returns False on connection error."""
        from requests.exceptions import ConnectionError
        self.client._session.get = MagicMock(side_effect=ConnectionError())

        self.assertFalse(self.client.is_available())

    def test_list_models(self):
        """list_models should return model names."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [
                {"name": "llama3.2:latest"},
                {"name": "mistral:latest"},
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        self.client._session.get = MagicMock(return_value=mock_resp)

        models = self.client.list_models()
        self.assertEqual(models, ["llama3.2:latest", "mistral:latest"])

    def test_set_model_success(self):
        """set_model should succeed for installed models."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [{"name": "mistral:latest"}]
        }
        mock_resp.raise_for_status = MagicMock()
        self.client._session.get = MagicMock(return_value=mock_resp)

        self.assertTrue(self.client.set_model("mistral"))
        self.assertEqual(self.client.model, "mistral:latest")

    def test_set_model_failure(self):
        """set_model should return False for unknown models."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"models": [{"name": "llama3.2:latest"}]}
        mock_resp.raise_for_status = MagicMock()
        self.client._session.get = MagicMock(return_value=mock_resp)

        self.assertFalse(self.client.set_model("nonexistent"))
        self.assertEqual(self.client.model, "test-model")  # unchanged

    def test_clear_history(self):
        """clear_history should empty the deque for that node."""
        self._mock_chat_response("Reply")
        self.client.chat("node1", "Question")
        self.assertGreater(len(self.client._history["node1"]), 0)

        self.client.clear_history("node1")
        self.assertEqual(len(self.client._history["node1"]), 0)

    def test_clear_history_nonexistent(self):
        """clear_history on unknown node should not error."""
        self.client.clear_history("unknown_node")  # should not raise

    def test_chat_timeout(self):
        """Chat should return timeout message on request timeout."""
        from requests.exceptions import Timeout
        self.client._session.post = MagicMock(side_effect=Timeout())

        result = self.client.chat("node1", "Hello")
        self.assertIn("timed out", result.lower())

    def test_chat_connection_error(self):
        """Chat should return unavailable message on connection error."""
        from requests.exceptions import ConnectionError
        self.client._session.post = MagicMock(side_effect=ConnectionError())

        result = self.client.chat("node1", "Hello")
        self.assertIn("unavailable", result.lower())


if __name__ == "__main__":
    unittest.main()
