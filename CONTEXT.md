# Project Context

This file tracks the history of changes, decisions, and current state of LORACLE BRIDGE.

---

## [2026-03-28] — Addon System + Dead Drop, Triage, Brief

- What changed:
  - **Plugin architecture**: Added addon system with base class, command registry, dashboard tab injection, and message observer hooks. Addons register commands, dashboard tabs, and API routes without modifying core bridge code.
  - **LORACLE DEAD DROP**: Encrypted async store-and-forward messaging over mesh. Fernet encryption (AES-128-CBC + HMAC), SQLite message queue, 72-hour TTL, commands: `!drop-key`, `!drop`, `!pickup`, `!pending`. Dashboard tab with pending/delivered status and purge controls.
  - **LORACLE TRIAGE**: Offline medical reference assistant. Separate RAG instance for medical docs (TCCC, field medicine, trauma protocols). Stateless queries with medical disclaimer. High-contrast dashboard UI. Commands: `!triage <question>`, `!triage topics`, `!triage status`.
  - **LORACLE BRIEF**: AI-generated situation reports from mesh traffic. Traffic aggregator observes all messages, scheduled SITREP generation via LLM (military format: SITUATION, KEY ACTIVITY, NODE STATUS, ASSESSMENT), text/PDF export, template fallback. Commands: `!brief`, `!brief now`, `!brief history`.
  - **Command registry refactor**: Replaced if/elif chain in `_handle_command` with dict-based registry. Addon commands auto-appear in `!help`.
  - **Dashboard tab injection**: Addons inject tabs via string replacement at serve time — no template engine needed.
  - **OllamaClient**: Added `system_prompt_override` parameter to `chat()` for addon-specific system prompts.
  - **New CLI flags**: `--enable-dead-drop`, `--enable-triage`, `--enable-brief`, `--enable-all-addons`, `--triage-dir`, `--brief-interval`
  - **New dependency**: `cryptography>=41.0.0` for Dead Drop encryption

- Key decisions:
  - Addons are opt-in (disabled by default) to keep the core bridge lightweight
  - Triage uses a separate RAGEngine instance (isolated medical KB, no cross-contamination with general docs)
  - Triage queries are stateless (no conversation history) for grounded, fresh answers
  - Dead Drop uses Fernet symmetric encryption (passphrase → PBKDF2 key derivation) — simpler than PKI, appropriate for the threat model
  - Brief falls back to template-based SITREPs when LLM is unavailable
  - Plugin loading is explicit (config/CLI controlled), not auto-discovery

- Files added:
  - `meshtastic-bridge/addons/` — full addon system (base.py + 3 addon packages)

---

## [2026-03-26] — DM-Only Responses, End Indicator

- What changed:
  - All AI responses are now sent as DMs (direct messages) back to the sender
  - Reverted broken public channel detection (Meshtastic toId format mismatch)
  - End-of-response indicator changed from `[Ready]` to `[End]`
  - Trigger word system (`_AI_TRIGGERS`) kept in codebase but not active

- Why:
  - The Meshtastic library's `toId` (hex string) doesn't match `myInfo.my_node_num` (integer), making reliable DM detection impossible without format conversion
  - DM-only is the simplest reliable behavior — users get private responses

---

## [2026-03-26] — URL-to-RAG, Clickable Launcher, Multi-Part Fix

- What changed:
  - **URL ingestion in dashboard**: Paste a URL in Controls > Knowledge Base > Add URL — fetches page, extracts text, saves as .txt, ingests into RAG
  - **Document management in dashboard**: View all ingested docs, delete individual documents
  - **macOS launcher**: `LORACLE BRIDGE.command` — double-click in Finder to launch
  - **Browser auto-open**: Dashboard opens in browser automatically on startup
  - **Multi-part send fix**: Use `wantAck=False` for multi-chunk messages to prevent radio ACK conflicts
  - **New API endpoints**: `/api/rag/ingest-url`, `/api/rag/delete`

---

## [2026-03-26] — Rebrand, Send Reliability, Model Profiles, RAG Fixes

- What changed:
  - Renamed project from LORACLE to LORACLE BRIDGE across all files
  - **Send reliability**: Added interface health check, retry with reconnect, `wantAck` on single messages
  - **Interactive pager**: Long responses truncated with `... (!more)` — send `!more` for next page
  - **"Thinking..." indicator**: Bridge sends immediate acknowledgment before running inference
  - **Model profiles**: Added Qwen3 (14b/8b), promoted Phi4:14b as preferred for 16GB+ RAM
  - **Smart auto-selection**: Best model auto-selected based on system RAM, auto-pulled if not installed
  - **RAG embedding fix**: UTF-8 sanitization in extractors and pre-embed pipeline — fixes 400 errors on scanned PDFs
  - **RAG context awareness**: LLM system prompt now includes RAG addendum so models know about the knowledge base
  - **Auto-clear history**: Conversation context auto-clears after 1 hour of node inactivity
  - **Rate limiting**: 5-second cooldown per node to prevent message flooding
  - **Relaxed log suppression**: Meshtastic library logs at WARNING (was CRITICAL) for better diagnostics

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
