"""Direct Ollama API client for standalone Meshtastic LLM bridge.

Replaces nomad_client.py when running without the full N.O.M.A.D. stack.
Calls Ollama's REST API directly and maintains per-node conversation history.
"""

import logging
from collections import defaultdict, deque
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful AI assistant. "
    "Keep responses brief, around 2-4 sentences. "
    "Write in short plain sentences. Never use bullet points, lists, asterisks, "
    "markdown, or code blocks. "
    "Be direct and give practical, actionable advice. "
    "Only state facts you are confident about. If unsure, say so."
)


class OllamaClient:
    """Lightweight Ollama client with per-node conversation history."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "gemma3:4b",
        max_response_length: int = 200,
        history_length: int = 10,
        system_prompt: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_response_length = max_response_length
        self._history = defaultdict(  # type: Dict[str, deque]
            lambda: deque(maxlen=history_length)
        )
        self._history_length = history_length
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT.format(
            max_chars=max_response_length
        )
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

        # Auto-resolve model name: "llama3.2" → "llama3.2:1b" if exact name not found
        self._resolve_model()

    def _resolve_model(self):
        """If the exact model name isn't installed, try fuzzy matching."""
        try:
            available = self.list_models()
            if not available:
                return
            if self.model in available:
                return  # Exact match found
            # Try prefix match: "llama3.2" matches "llama3.2:1b", "llama3.2:latest"
            matches = [m for m in available if m.startswith(f"{self.model}:")]
            if matches:
                old = self.model
                self.model = matches[0]
                logger.info(f"Model resolved: {old} -> {self.model}")
        except Exception:
            pass  # Ollama not running yet — will fail later with clear error

    def chat(
        self,
        node_id: str,
        message: str,
        context_messages: Optional[List[Dict]] = None,
    ) -> str:  # noqa: C901
        """Send a message to Ollama and return the response.

        Maintains conversation history per node_id.

        Args:
            node_id: Identifier for the sending node.
            message: The user's message.
            context_messages: Optional RAG context messages to inject
                between system prompt and history.
        """
        # Build messages array: [system] + [context] + [history] + [user]
        messages = [{"role": "system", "content": self.system_prompt}]
        if context_messages:
            messages.extend(context_messages)
        messages.extend(list(self._history[node_id]))
        messages.append({"role": "user", "content": message})

        try:
            resp = self._session.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                },
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("message", {}).get("content", "").strip()

            # Update history
            self._history[node_id].append({"role": "user", "content": message})
            self._history[node_id].append({"role": "assistant", "content": content})

            return content

        except requests.Timeout:
            logger.error("Ollama request timed out (120s)")
            return "Request timed out. Try a shorter question."
        except requests.ConnectionError:
            logger.error(f"Cannot connect to Ollama at {self.base_url}")
            return "AI service unavailable."
        except Exception as e:
            logger.error(f"Ollama chat error: {e}")
            return f"Error: {e}"

    def is_available(self) -> bool:
        """Check if Ollama is reachable."""
        try:
            resp = self._session.get(f"{self.base_url}/api/tags", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    def list_models(self) -> List[str]:
        """List installed Ollama models."""
        try:
            resp = self._session.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            models = resp.json().get("models", [])
            return [m["name"] for m in models]
        except Exception as e:
            logger.error(f"Failed to list models: {e}")
            return []

    def set_model(self, model: str) -> bool:
        """Switch to a different model. Returns True if model exists."""
        available = self.list_models()
        # Match with or without tag suffix
        matches = [m for m in available if m == model or m.startswith(f"{model}:")]
        if matches:
            self.model = matches[0]
            logger.info(f"Model switched to: {self.model}")
            return True
        logger.warning(f"Model '{model}' not found. Available: {available}")
        return False

    def clear_history(self, node_id: str):
        """Clear conversation history for a node."""
        if node_id in self._history:
            self._history[node_id].clear()
            logger.info(f"Cleared history for {node_id}")

    def embed(self, texts: List[str], model: str = "nomic-embed-text") -> List[List[float]]:
        """Generate embeddings via Ollama /api/embed endpoint.

        Args:
            texts: List of strings to embed.
            model: Embedding model name.

        Returns:
            List of embedding vectors.
        """
        resp = self._session.post(
            f"{self.base_url}/api/embed",
            json={"model": model, "input": texts},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]

    def pull_model(self, model: str) -> bool:
        """Pull (download) a model from Ollama.

        Args:
            model: Model name to pull.

        Returns:
            True if successful.
        """
        try:
            logger.info(f"Pulling model: {model}")
            resp = self._session.post(
                f"{self.base_url}/api/pull",
                json={"name": model, "stream": False},
                timeout=600,
            )
            resp.raise_for_status()
            logger.info(f"Model {model} pulled successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to pull model {model}: {e}")
            return False
