# Project Context

This file tracks the history of changes, decisions, and current state of LORACLE BRIDGE.

---

## [2026-04-07] — Public-Channel Talk + Manual Broadcast Welcome

- What changed:
  - **Real DM vs broadcast detection** in `_on_receive` (`standalone_bridge.py`). Replaced the hardcoded `is_dm = True` with a proper comparison: `is_dm = (to_id.lower() == self_id.lower())` where `self_id` comes from `_get_self_node_id()` (added with the greeter work). Falls back to legacy "everything is a DM" only if `self_id` is None (interface still warming up), so we never silently drop a message.
  - **Trigger-gated public-channel replies** — wired up the existing-but-dormant `_is_addressed_to_ai()` function. On a public channel, the bridge stays silent unless the message starts with `!` or contains a trigger word from `_AI_TRIGGERS = {agent, ai, oracle, loracle, bridge, help, hey}`. Casual chat is ignored. When triggered, the response goes back as a broadcast on the same channel index.
  - **Per-channel cooldown** (`PUBLIC_CHANNEL_COOLDOWN_SECS = 8`, tracked in new `_channel_last_send` dict) prevents a chain of trigger-word messages from saturating a public channel with bot replies. Per-sender rate limit still applies on top.
  - **Hard kill switch**: `--no-public-talk` CLI flag (default on). Routes through `__init__` as `public_talk: bool`.
  - **Greeter message exposed**: `GreeterService.stats()` now includes `"message"` so the dashboard can read the welcome text.
  - **New dashboard button "Welcome → Public"** next to the existing Send button on the Messages tab. Clicking it pre-fills `#msg-send-text` with the greeter message, sets recipient to Broadcast and channel to 0, and focuses the input. **Does not auto-send** — user reviews and clicks Send. Reuses the existing `/api/send-mesh` endpoint, no new routes.
  - **README updated** with a "Public Channel Mode" section under the existing Auto-Greeter section.

- Why:
  - The bridge previously ignored public-channel traffic entirely. New users on the mesh had to discover LORACLE via DM, which required out-of-band knowledge. The trigger-gated public reply gives anyone on the mesh a way to interact (`hey loracle, ...`) while keeping the bot quiet during normal chat. The manual broadcast button lets the operator post the welcome message on demand without typing it out.

- Key decisions:
  - Trigger-gated, not auto-reply. A bot that replies to every public message is spam.
  - Reuse the existing infrastructure (`_send_response` + `_request_queue` + `_is_addressed_to_ai`) rather than building parallel paths. The whole pipeline already accepted `is_dm` and `channel`; only `_on_receive` needed real values.
  - Pre-fill, don't auto-send. The user explicitly chose "no auto-send" in planning so accidental clicks don't spam the channel.
  - 8-second per-channel cooldown is conservative. If the user wants chattier replies they can lower it; if they want it strict it can go higher.

- Files modified:
  - `meshtastic-bridge/standalone_bridge.py` — `_on_receive` real DM detection + trigger gating, `PUBLIC_CHANNEL_COOLDOWN_SECS` constant, `_channel_last_send` dict, `public_talk` init arg, `--no-public-talk` / `--public-talk` CLI flags
  - `meshtastic-bridge/greeter.py` — `stats()` returns `message`
  - `meshtastic-bridge/dashboard.py` — Welcome → Public button + `prefillWelcome()` JS
  - `README.md` — Public Channel Mode subsection

---

## [2026-04-07] — Auto-Greeter for New Mesh Nodes

- What changed:
  - **New module `meshtastic-bridge/greeter.py`** — `GreeterService` class that proactively DMs each newly-discovered mesh node a one-time welcome message identifying the bridge as an offline AI assistant.
  - **Persisted one-shot per node forever** — state stored in `~/.mesh-llm/greeted_nodes.json` (atomic write via `.tmp` rename), so restarts never re-greet anyone.
  - **First-deployment safety net** — on the very first launch with an empty greeted-list, every node already in `interface.nodes` is silently marked as known so a fresh deploy doesn't blast the entire mesh.
  - **Startup grace period** — `pump()` is a no-op for the first 90 s after process start, preventing the initial nodeDB flood from triggering sends.
  - **Global rate limit** — at most 1 greeting per 10 s, queued FIFO. Failed sends re-queue once.
  - **Self-DM and broadcast filters** — `_is_concrete_node` rejects empty/non-hex ids, `^all`, `!ffffffff`, and the local node id (set via `set_self_id` after connect derives it from `interface.myInfo.my_node_num`).
  - **Wired into 3 first-sighting paths** in `standalone_bridge.py`: `_on_receive`, `_on_position` (only on `is_new`), and `_load_nodedb_positions` (which also calls `seed_from_nodedb` once for the first-deployment safety net).
  - **Pump is hooked into the existing reconnect-loop tick** alongside the periodic nodeDB refresh, so no new threads.
  - **CLI flags**: `--auto-greet` (default on), `--no-auto-greet`, `--greet-message "..."` to override the built-in text.
  - **Dashboard exposure**: `/api/state.greeter` returns `{enabled, greeted_count, queued, sent_this_session, self_id, grace_remaining_s, ...}` for visibility without enabling DEBUG logging.
  - **README updated** with an Auto-Greeter section under the bridge features.

- Why:
  - New users joining the mesh had no idea LORACLE was on the channel — they'd have to be told out-of-band or stumble onto `!help`. Proactive greeting closes that discovery gap.

- Files added:
  - `meshtastic-bridge/greeter.py`

- Files modified:
  - `meshtastic-bridge/standalone_bridge.py` — instantiate greeter, CLI flags, hooks into 3 packet paths + connect-loop pump, `_get_self_node_id` helper
  - `meshtastic-bridge/dashboard.py` — `/api/state.greeter` field
  - `README.md` — Auto-Greeter section

---

## [2026-04-06] — Spatial Features: Navigation, Map Fixes, Coverage, DM Popup, Hops

This is a catch-up entry for several commits that landed across early April but never got logged here. Recent commits: `692cb39`, `9292908`, `3aafd1f`.

- What changed (combined):
  - **Navigation addon** at `meshtastic-bridge/addons/navigation/` — registers `!nav` / `!navigate <lat>,<lon>`. Pure-Python Haversine + initial-bearing math, returns a single-packet template (`Hdg / Dist / From / To / GPS age`). No LLM call, no internet, no geocoder. CLI flag `--enable-navigation` (default on). Wired through the existing addon system the same way Triage / Brief / Dead Drop are.
  - **Map bug fix — other nodes' positions now appear on the dashboard map.** Two real bugs fixed:
    1. `_load_nodedb_positions` only handled `latitude`/`longitude` floats and silently dropped `latitudeI`/`longitudeI` int1e7 form (which is what some meshtastic-python paths emit). Pulled the logic into a shared `_extract_position` static helper used by both `_on_position` and `_load_nodedb_positions` so the two code paths can never diverge again.
    2. `_load_nodedb_positions` only ran once at connect, before the meshtastic library had finished streaming the nodeDB. Now re-runs every 30 s from the existing reconnect loop via `_last_nodedb_refresh` + `NODEDB_REFRESH_INTERVAL_S`.
    First-seen positions also log at INFO so the user can see the bridge picking up nodes without enabling DEBUG.
  - **`/api/state` visibility**: added `node_positions_count` and `nodedb_size` so you can hit the endpoint in a browser and immediately see whether positions are arriving.
  - **Coverage logger + Coverage tab**: new `meshtastic-bridge/coverage_logger.py` (`CoverageLogger`) appends `(ts, node, lat, lon, rssi, snr)` JSONL records to `~/.mesh-llm/coverage.jsonl` whenever a packet has both signal info and a known position. Throttle: 1 sample per node per 5 s / 10 m. Hooked into both `_on_receive` and `_on_position`. New dashboard tab renders the data via Leaflet.heat (heatmap mode, default) plus a Grid mode (40 m × 40 m solid colored rectangles by best RSSI), Both mode, dead-zone overlay, time-window filter, min-RSSI slider, and a persistent legend. New endpoints: `/api/coverage/samples`, `/api/coverage/stats`.
  - **Hop tracking**: new `_node_meta` dict tracks `hopStart - hopLimit` per node from any incoming packet. Exposed via `/api/state.node_meta`.
  - **Bigger / brighter node markers** on the Messages map: 28 px pulsing rings, glowing core, bright label below each marker (last 6 chars of node id + hop suffix like `· 2h`), amber stale state after 10 min. CSS keyframe `nodePing` added.
  - **Actionable popups**: clicking a node now opens a popup with lat/lon/alt, hop count, age, and a **DM this node** button. New `dmNode(nodeId)` JS helper switches to the Messages tab if needed, injects the node into the Send Message dropdown if missing, sets it as the recipient, scrolls the form into view with a brief blue highlight flash, and focuses the text input.
  - **Send dropdown bug fix**: `updateSendDropdown` was wiping any DM target that wasn't in `_known_nodes` on the next poll tick — position-only nodes silently reset to Broadcast. Now unions `known_nodes` + `node_positions` keys + the currently-selected value before regenerating options.
  - **Kwarg-collision fix**: `/api/send-mesh` was calling `record_message("out", "dashboard", text, direction=direction)` — but `record_message`'s first positional parameter is also literally named `direction`, triggering "got multiple values for argument 'direction'" on every manual send. Renamed the human-label kwarg to `dest_label`.

- Files added:
  - `meshtastic-bridge/addons/navigation/__init__.py`
  - `meshtastic-bridge/addons/navigation/addon.py`
  - `meshtastic-bridge/coverage_logger.py`

- Files modified:
  - `meshtastic-bridge/addons/__init__.py` — registered `navigation` addon
  - `meshtastic-bridge/standalone_bridge.py` — `_extract_position`, periodic nodeDB refresh, hop tracking, coverage logger, navigation CLI flag
  - `meshtastic-bridge/dashboard.py` — Coverage tab, popup rewrite, dmNode helper, dropdown union fix, kwarg fix, /api/state additions

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
