# Project Context

This file tracks the history of changes, decisions, and current state of LORACLE BRIDGE.

---

## [2026-03-25] — Independence Cleanup

- What changed:
  - Removed all references to the original project that LORACLE BRIDGE was forked from
  - Deleted unused files and directories that were not part of the standalone bridge
  - Updated LICENSE, README, CONTRIBUTING.md to reflect LORACLE BRIDGE as an independent project
  - Cleaned up comments and docstrings across all source files

- Why:
  - LORACLE BRIDGE's standalone bridge has zero code dependencies on the original project
  - Making a clean break ensures LORACLE BRIDGE is a fully independent open-source project

---

## [2026-03-24] — LORACLE BRIDGE Rebrand

- What changed:
  - Rebranded the project to LORACLE BRIDGE (LoRa + Oracle)
  - Updated all user-facing strings, banners, and documentation

---

## [2026-03-24] — Standalone Meshtastic LLM Bridge

- What changed:
  - Built standalone bridge (`standalone_bridge.py`) that talks directly to Ollama
  - One-command launcher (`mesh-llm.sh`) with auto-detection and dependency installation
  - RAG knowledge base with PDF/ZIM/text ingestion and semantic search
  - 5-tab web dashboard with live status, message log, controls, debug, and guide
  - Message chunking protocol for LoRa's 233-byte limit
  - BLE, TCP, and USB serial connection support
  - Auto-model selection based on system RAM
  - Per-node conversation history with auto-cleanup

- Files:
  - `mesh-llm.sh` — one-command launcher
  - `meshtastic-bridge/standalone_bridge.py` — main bridge
  - `meshtastic-bridge/ollama_client.py` — Ollama API client
  - `meshtastic-bridge/protocol.py` — LoRa chunking protocol
  - `meshtastic-bridge/dashboard.py` — Flask web dashboard
  - `meshtastic-bridge/manage_docs.py` — document management CLI
  - `meshtastic-bridge/rag/` — RAG engine, chunker, extractors
  - `meshtastic-bridge/tests/` — unit tests
