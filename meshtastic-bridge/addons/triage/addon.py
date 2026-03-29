"""LORACLE TRIAGE addon — offline medical reference assistant.

Commands:
    !triage <question>  — Query medical knowledge base
    !triage topics      — List available medical topics
    !triage status      — Medical KB statistics
"""

import logging
import os
from typing import Dict, Optional, Tuple

from addons.base import Addon
from addons.triage.prompts import TRIAGE_SYSTEM_PROMPT, TRIAGE_RAG_ADDENDUM, NO_DOCS_PROMPT

logger = logging.getLogger(__name__)

ADDON_CLASS = None  # Set at bottom of file


class TriageAddon(Addon):
    name = "triage"
    display_name = "Triage"

    def __init__(self, bridge, config: dict = None):
        super().__init__(bridge, config)
        config = config or {}

        self.data_dir = config.get("data_dir") or os.path.join(
            os.path.expanduser("~"), ".mesh-llm", "triage"
        )
        self.ollama_url = config.get("ollama_url", "http://localhost:11434")

        # Create a separate RAG engine instance for medical documents
        self.rag_engine = None
        try:
            from rag import RAGEngine
            self.rag_engine = RAGEngine(
                ollama_base_url=self.ollama_url,
                data_dir=self.data_dir,
                top_k=3,
                score_threshold=0.25,
            )
            stats = self.rag_engine.get_stats()
            logger.info(
                f"Triage RAG: {stats['total_docs']} docs, "
                f"{stats['total_chunks']} chunks in {self.data_dir}"
            )
        except ImportError as e:
            logger.warning(f"Triage RAG dependencies not available: {e}")
        except Exception as e:
            logger.warning(f"Triage RAG init failed: {e}")

    def get_commands(self) -> Dict[str, Tuple[callable, str]]:
        return {
            "!triage": (self._cmd_triage, "<question> - query medical reference"),
        }

    def get_dashboard_tab(self) -> Optional[dict]:
        from addons.triage.dashboard import get_tab_config
        return get_tab_config()

    def get_api_routes(self):
        from addons.triage.dashboard import get_api_routes
        return get_api_routes(self)

    def _cmd_triage(self, node_id: str, arg: str) -> str:
        """Handle !triage queries — stateless medical reference lookups."""
        if not arg:
            return (
                "LORACLE TRIAGE — Offline Medical Reference. "
                "Usage: !triage <question>. "
                "Example: !triage how to apply a tourniquet"
            )

        # Sub-commands
        if arg.lower() == "topics":
            return self._get_topics()
        if arg.lower() == "status":
            return self._get_status()

        # Check if RAG is available
        if not self.rag_engine:
            return NO_DOCS_PROMPT

        stats = self.rag_engine.get_stats()
        if stats["total_docs"] == 0:
            return NO_DOCS_PROMPT

        # Search medical knowledge base
        try:
            context_messages = self.rag_engine.build_context_messages(arg)
        except Exception as e:
            logger.warning(f"Triage RAG search failed: {e}")
            context_messages = None

        if not context_messages:
            return (
                "No relevant medical reference found for that query. "
                "Try rephrasing or check !triage topics. "
                "[Not medical advice — seek professional care]"
            )

        # Build context string from RAG results
        context_text = "\n\n".join(
            msg["content"] for msg in context_messages if msg.get("content")
        )

        # Query LLM with medical system prompt + RAG context (stateless — no history)
        full_prompt = TRIAGE_SYSTEM_PROMPT + "\n\n" + TRIAGE_RAG_ADDENDUM.format(
            context=context_text
        )

        try:
            response = self.bridge.ollama.chat(
                f"triage_{node_id}",  # Isolated history namespace
                arg,
                context_messages=None,  # We inject context via system prompt
                system_prompt_override=full_prompt,
            )
            # Always clear triage history (stateless queries)
            self.bridge.ollama.clear_history(f"triage_{node_id}")
            return response
        except Exception as e:
            logger.error(f"Triage LLM query failed: {e}")
            return f"Triage query error: {e}. [Not medical advice — seek professional care]"

    def _get_topics(self) -> str:
        """List documents in the medical knowledge base."""
        if not self.rag_engine:
            return NO_DOCS_PROMPT
        docs = self.rag_engine.list_documents()
        if not docs:
            return "No medical documents loaded. Add TCCC PDFs via dashboard."
        lines = [f"{d['filename']} ({d['chunk_count']} chunks)" for d in docs[:10]]
        result = "Medical KB: " + ", ".join(lines)
        if len(docs) > 10:
            result += f" ...and {len(docs) - 10} more"
        return result

    def _get_status(self) -> str:
        """Get medical knowledge base statistics."""
        if not self.rag_engine:
            return "Triage RAG not available."
        stats = self.rag_engine.get_stats()
        return (
            f"Triage Medical KB: {stats['total_docs']} docs, "
            f"{stats['total_chunks']} chunks. "
            f"Data dir: {self.data_dir}"
        )

    def on_start(self):
        logger.info("Triage addon started")

    def on_stop(self):
        logger.info("Triage addon stopped")


ADDON_CLASS = TriageAddon
