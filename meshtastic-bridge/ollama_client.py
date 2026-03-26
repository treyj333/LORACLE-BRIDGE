"""Ollama API client for LORACLE BRIDGE.

Calls Ollama's REST API directly and maintains per-node conversation history.
"""

import logging
import os
import platform
import subprocess
import time
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful AI assistant communicating over a low-bandwidth LoRa mesh radio. "
    "Keep responses brief, 2-4 sentences. Write in short plain sentences. "
    "Never use bullet points, lists, asterisks, markdown, or code blocks. "
    "Be direct and give practical, actionable advice. "
    "Only state facts you are confident about. If unsure, say so."
)

# ─── Model profiles ──────────────────────────────────────────────────────────
# Ordered by preference (best first). Auto-select picks the first one
# that fits in available RAM and is installed.

MODEL_PROFILES = [
    {
        "name": "phi4:14b",
        "min_ram_gb": 16,
        "tier": "large",
        "description": "Microsoft Phi-4 — strong reasoning, connects dots across RAG chunks",
        "system_prompt": (
            "You are a helpful AI assistant communicating over a low-bandwidth LoRa mesh radio. "
            "Keep responses under 220 characters. Respond in 1-2 short sentences maximum. "
            "Never use bullet points, lists, asterisks, markdown, or code blocks. "
            "When context documents are provided, reason carefully across them to synthesize an answer. "
            "Cite which context you drew from when relevant. "
            "Be direct and give practical, actionable advice. Only state facts you are confident about."
        ),
    },
    {
        "name": "qwen3:14b",
        "min_ram_gb": 16,
        "tier": "large",
        "description": "Qwen 3 14B — latest generation, strong reasoning and instruction-following",
        "system_prompt": (
            "You are a helpful AI assistant communicating over a low-bandwidth LoRa mesh radio. "
            "Keep responses under 220 characters. Respond in 1-2 short sentences maximum. "
            "Never use bullet points, lists, asterisks, markdown, or code blocks. "
            "When context documents are provided, reason carefully across them to synthesize an answer. "
            "Cite which context you drew from when relevant. "
            "Be direct and give practical, actionable advice. Only state facts you are confident about."
        ),
    },
    {
        "name": "qwen2.5:14b",
        "min_ram_gb": 16,
        "tier": "large",
        "description": "Qwen 2.5 14B — excellent instruction-following, table/structured data reading",
        "system_prompt": (
            "You are a helpful AI assistant communicating over a low-bandwidth LoRa mesh radio. "
            "Keep responses under 220 characters. Respond in 1-2 short sentences maximum. "
            "Never use bullet points, lists, asterisks, markdown, or code blocks. "
            "You excel at reading structured data and tables from context documents. "
            "When provided with context, extract precise facts and figures. "
            "Be direct and give practical, actionable advice. Only state facts you are confident about."
        ),
    },
    {
        "name": "qwen3:8b",
        "min_ram_gb": 8,
        "tier": "medium",
        "description": "Qwen 3 8B — latest generation, excellent instruction-following at lower resource usage",
        "system_prompt": (
            "You are a helpful AI assistant communicating over a low-bandwidth LoRa mesh radio. "
            "Keep responses under 220 characters. Respond in 1-2 short sentences maximum. "
            "Never use bullet points, lists, asterisks, markdown, or code blocks. "
            "When context documents are provided, use them to give accurate answers. "
            "Be direct and give practical, actionable advice. Only state facts you are confident about."
        ),
    },
    {
        "name": "qwen2.5:7b",
        "min_ram_gb": 8,
        "tier": "medium",
        "description": "Qwen 2.5 7B — strong instruction-following, good balance of speed and quality",
        "system_prompt": (
            "You are a helpful AI assistant communicating over a low-bandwidth LoRa mesh radio. "
            "Keep responses under 220 characters. Respond in 1-2 short sentences maximum. "
            "Never use bullet points, lists, asterisks, markdown, or code blocks. "
            "When context documents are provided, use them to give accurate answers. "
            "Be direct and give practical, actionable advice. Only state facts you are confident about."
        ),
    },
    {
        "name": "gemma3:4b",
        "min_ram_gb": 4,
        "tier": "small",
        "description": "Google Gemma 3 4B — lightweight fallback, runs on low-RAM devices",
        "system_prompt": None,  # Uses DEFAULT_SYSTEM_PROMPT
    },
]


def get_system_ram_gb() -> int:
    """Detect total system RAM in GB. Returns 8 if detection fails."""
    try:
        if platform.system() == "Darwin":
            raw = subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip()
            return int(raw) // (1024 ** 3)
        else:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return (pages * page_size) // (1024 ** 3)
    except Exception:
        return 8


def get_profile_for_model(model_name: str) -> Optional[Dict]:
    """Find the profile matching a model name.

    Checks exact match first, then base name match (e.g. 'qwen2.5:7b' matches
    profile 'qwen2.5:7b' but NOT 'qwen2.5:14b').
    """
    # Exact match first
    for profile in MODEL_PROFILES:
        if model_name == profile["name"]:
            return profile
    # Base name match: strip tag variants like ':latest'
    base = model_name.split(":")[0] if ":" in model_name else model_name
    for profile in MODEL_PROFILES:
        profile_base = profile["name"].split(":")[0]
        profile_tag = profile["name"].split(":")[1] if ":" in profile["name"] else ""
        model_tag = model_name.split(":")[1] if ":" in model_name else ""
        if base == profile_base and (model_tag == profile_tag or model_tag == "latest"):
            return profile
    return None


def auto_select_model(
    ollama_url: str = "http://localhost:11434",
    user_model: Optional[str] = None,
) -> Tuple[str, Optional[str], bool]:
    """Auto-select the best model based on available RAM and installed models.

    Args:
        ollama_url: Ollama API base URL.
        user_model: Explicit model from --model flag (None = auto-select).

    Returns:
        (model_name, system_prompt_or_None, needs_pull)
    """
    ram_gb = get_system_ram_gb()
    logger.info(f"System RAM: {ram_gb}GB")

    # If user explicitly chose a model, respect it but still try to find a matching prompt
    if user_model:
        profile = get_profile_for_model(user_model)
        prompt = profile["system_prompt"] if profile else None
        logger.info(f"Using user-specified model: {user_model}")
        return (user_model, prompt, False)

    # Query installed models
    installed = []
    try:
        session = requests.Session()
        resp = session.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=5)
        resp.raise_for_status()
        installed = [m["name"] for m in resp.json().get("models", [])]
    except Exception as e:
        logger.warning(f"Could not query Ollama models: {e}")

    # Find the best profile that fits this RAM (whether installed or not)
    best_for_ram = None
    for profile in MODEL_PROFILES:
        if profile["min_ram_gb"] <= ram_gb:
            best_for_ram = profile
            break

    # Walk profiles top-to-bottom: pick best that fits RAM and is installed
    best_installed = None
    best_installed_profile = None
    for profile in MODEL_PROFILES:
        if profile["min_ram_gb"] > ram_gb:
            continue
        name = profile["name"]
        # Check if installed (exact match or 'name:latest' variant)
        matched = [m for m in installed if m == name or m == f"{name}:latest"]
        if matched:
            best_installed = matched[0]
            best_installed_profile = profile
            break

    # If we found an installed model but a better one could fit, recommend pulling it
    if best_installed and best_for_ram and best_for_ram != best_installed_profile:
        logger.info(
            f"Installed: {best_installed} ({best_installed_profile['tier']}), "
            f"but {best_for_ram['name']} ({best_for_ram['tier']}) would be better "
            f"for {ram_gb}GB RAM. Pulling upgrade..."
        )
        return (best_for_ram["name"], best_for_ram["system_prompt"], True)

    if best_installed:
        logger.info(
            f"Auto-selected {best_installed} ({best_installed_profile['tier']}, "
            f"{ram_gb}GB RAM available)"
        )
        return (best_installed, best_installed_profile["system_prompt"], False)

    # Nothing installed that matches — pick the best that fits and flag for pull
    if best_for_ram:
        logger.info(
            f"No suitable model installed. Recommending {best_for_ram['name']} "
            f"({best_for_ram['tier']}, needs {best_for_ram['min_ram_gb']}GB+ RAM)"
        )
        return (best_for_ram["name"], best_for_ram["system_prompt"], True)

    # Absolute fallback
    fallback = MODEL_PROFILES[-1]
    return (fallback["name"], fallback["system_prompt"], True)


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
        self._last_activity = {}  # node_id -> timestamp for auto-clear
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
        # Auto-clear stale conversation context (>1 hour idle)
        now = time.time()
        if node_id in self._last_activity:
            if now - self._last_activity[node_id] > 3600:
                logger.info(f"Auto-clearing stale context for {node_id} (>1h idle)")
                self.clear_history(node_id)
        self._last_activity[node_id] = now

        # Build messages array: [system] + [context] + [history] + [user]
        system_content = self.system_prompt
        if context_messages:
            from rag.engine import RAG_SYSTEM_ADDENDUM
            system_content = f"{self.system_prompt}\n\n{RAG_SYSTEM_ADDENDUM}"
        messages = [{"role": "system", "content": system_content}]
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
        """Switch to a different model. Returns True if model exists.

        Also updates the system prompt if the model has a matching profile.
        """
        available = self.list_models()
        # Match with or without tag suffix
        matches = [m for m in available if m == model or m.startswith(f"{model}:")]
        if matches:
            self.model = matches[0]
            # Update system prompt if there's a profile for this model
            profile = get_profile_for_model(self.model)
            if profile and profile["system_prompt"]:
                self.system_prompt = profile["system_prompt"]
                logger.info(f"Model switched to: {self.model} (with tuned prompt)")
            else:
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
