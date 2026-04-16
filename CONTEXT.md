# Project Context

This file tracks the history of changes, decisions, and current state of LORACLE BRIDGE.

---

## [2026-04-16] — Session 2a: MeshCore Backend + RadioBackend Abstraction

- What changed:
  - **New `radio/` module** with 7 files providing a protocol-agnostic abstraction layer:
    - `events.py` — `Protocol` enum (MT/MC), `Transport` enum, `UnifiedNode` and `UnifiedMessage` dataclasses with globally-unique IDs (`"mt:!a3f2b8c1"`, `"mc:abcdef012345"`)
    - `backend.py` — abstract `RadioBackend` interface (threading-based): `connect()`, `disconnect()`, `send_direct_message()`, `send_broadcast()`, `start_listening(callback)`, `get_nodes()`, `get_self_info()`
    - `meshtastic_backend.py` — `MeshtasticBackend`: extracts Meshtastic-specific code (serial/TCP/BLE connection, pubsub, packet→UnifiedMessage conversion, nodeDB refresh, BLE scanning) from `standalone_bridge.py`
    - `meshcore_backend.py` — `MeshCoreBackend`: wraps async `meshcore` library in background thread, subscribes to `CONTACT_MSG_RECV`/`CHANNEL_MSG_RECV`, converts events to UnifiedMessage. RSSI/SNR not available (set to None).
    - `manager.py` — `RadioManager`: holds multiple backends, shared `queue.Queue` for incoming messages, routes outgoing sends by protocol prefix in node ID, provides `get_all_nodes()`/`get_backends_info()`
    - `detector.py` — `detect_protocol()`: probes serial/TCP/BLE connections to determine Meshtastic vs MeshCore
  - **Refactored `standalone_bridge.py`**: added `_radio_manager` (RadioManager) and `_primary_backend` (MeshtasticBackend) fields, new `_node_sync_loop` thread, new `_ai_replies_enabled` flag, new CLI flags (`--protocol`, `--second-radio`, `--ai-replies`), settings persistence (`~/.mesh-llm/settings.json`)
  - **Updated `greeter.py`**: `pump()` now accepts `send_fn` callback as alternative to raw `interface`
  - **Updated `dashboard.py`**: new `GET/POST /api/radios` and `GET/POST /api/ai-replies` endpoints, `/api/state` includes `backends` list and `ai_replies_enabled`, message feed shows MT/MC protocol badge, status banner shows backend protocol info, stat strip has "Radios" cell, CONFIG tab has new RADIOS and AI REPLIES sections, onboarding step 2 mentions MeshCore
  - **Updated `requirements.txt`**: added `meshcore>=2.2.1`
  - **19 new tests** in `test_radio_backends.py` covering UnifiedNode, UnifiedMessage, RadioManager routing/queue/lifecycle, Protocol/Transport enums
  - **Architecture**: threading-based (not async) to match existing patterns. MeshCore's asyncio runs in isolated background thread with `asyncio.run_coroutine_threadsafe()` for cross-thread calls.
- Architecture decision: **Threading-based RadioBackend interface** (not async as originally spec'd) because the entire existing bridge uses pubsub callbacks + queue.Queue + daemon threads. Converting to async would be a major rewrite beyond Session 2a scope.
- Files changed:
  - `meshtastic-bridge/radio/__init__.py`, `events.py`, `backend.py`, `meshtastic_backend.py`, `meshcore_backend.py`, `detector.py`, `manager.py` — new
  - `meshtastic-bridge/standalone_bridge.py` — refactored (RadioManager integration)
  - `meshtastic-bridge/dashboard.py` — new endpoints, badges, CONFIG sections
  - `meshtastic-bridge/greeter.py` — pump() signature
  - `meshtastic-bridge/requirements.txt` — meshcore
  - `meshtastic-bridge/tests/test_radio_backends.py` — new (19 tests)
  - `meshtastic-bridge/tests/test_dashboard_api.py` — mock bridge updated

---

## [2026-04-16] — Session 1: Retro UI Revamp — 2-Tab Shell, Mesh Header, Onboarding Modal

- What changed:
  - **Complete frontend rewrite** of `dashboard.py`'s `DASHBOARD_HTML` string. Replaced 6-tab cyberpunk theme (Inference/Messages/Coverage/Controls/Debug/Guide) with a 2-tab retro-modern design (LIVE/CONFIG).
  - **New design system**: IBM Plex Mono font, warm paper palette (light theme #ebe6dc / dark theme #121110), hairline dividers, no cards/gradients/shadows. CSS custom properties with `--lo-` prefix. WCAG AA contrast enforced.
  - **LIVE tab** merges Dashboard + Messages + Debug + Coverage: animated SVG mesh header (oracle sonar pulse + teal peer nodes + dashed packet animations), 4-cell stat strip, schematic SVG mesh map (nodes positioned by ID hash with RSSI/hop labels), unified message feed (grid rows with direction arrows, filter chips, search), composer bar (direct LLM chat via `/api/chat`), collapsible system log viewer, collapsible coverage heatmap.
  - **CONFIG tab** replaces Controls: collapsible `<details>` sections for Connection, Model, Response, Knowledge Base, History, Geographic Map (Leaflet), Appearance (light/dark toggle), About. Addon tabs (Dead Drop, Brief, Triage) now inject as collapsible sections via modified `_inject_addon_tabs()`.
  - **Onboarding modal**: 5-step tour with SVG animations (reuses mesh header primitives), arrow key navigation, localStorage persistence. Auto-shows on first visit, re-launchable from CONFIG > Appearance.
  - **Help popover**: `?` button in title bar with quick troubleshooting reference. Replaces deleted Guide tab.
  - **Theme system**: light/dark themes with CSS custom properties and localStorage persistence. System preference detection via `prefers-color-scheme`.
  - **Backward compatibility**: CSS variable aliases (`--text-primary`, `--border`, `--accent-red`, etc.) preserve addon styling. `showToast()` and `escapeHtml()` function signatures unchanged.
  - **prefers-reduced-motion**: all SVG animations disabled when reduce motion is preferred.
  - **IBM Plex Mono** font files added to `static/fonts/` (Regular 400, Medium 500).
  - **No backend changes**: all 30 API endpoints unchanged. Only `_inject_addon_tabs()` modified (injects collapsible CONFIG sections instead of separate tabs).
- Files changed:
  - `meshtastic-bridge/dashboard.py` — `_inject_addon_tabs()` + full `DASHBOARD_HTML` rewrite
  - `meshtastic-bridge/static/fonts/IBMPlexMono-Regular.ttf` — new
  - `meshtastic-bridge/static/fonts/IBMPlexMono-Medium.ttf` — new
  - `README.md` — tab name references updated

---

## [2026-04-07] — Ask LORACLE from the Dashboard (Local Chat + Optional Rebroadcast)

- What changed:
  - **New `POST /api/ask` endpoint** in `dashboard.py`. Takes `{text, dest, channel}`, runs the text through `_bridge._handle_command("!dashboard", text)` first (so `!nav`, `!help`, etc. work), falls back to `_bridge.ollama.chat("!dashboard", text, context_messages=rag)` for regular questions, and returns the answer + a `transmitted` flag. Optionally rebroadcasts the answer via `_bridge._send_response()` (broadcast on a channel OR DM to a specific node).
  - **Uses a sentinel node id `"!dashboard"`** so Ollama history for dashboard chats is isolated from real-node conversation histories.
  - **New "Mode" selector on the Send Message card** — `Raw send` (existing behavior) vs `Ask LORACLE`. Default is Raw send, preserving all existing flows including the Welcome → Public pre-fill button.
  - **In Ask mode**: card title flips to "Ask LORACLE", placeholder changes to "Ask LORACLE anything…", Send button relabels to "Ask", a hint explains the mode, and the recipient dropdown gets a new sentinel `Local only (don't transmit)` option at the top.
  - **Recipient semantics in Ask mode**: `Local only` → answer shown in the dashboard message log, nothing goes out on the radio. `Broadcast` → answer is transmitted on the selected channel via `_send_response(is_dm=False)`. A specific `!hex` node → answer is DM'd to that node via `_send_response(is_dm=True)`, with `!more` pager continuation keyed to the target's id.
  - **Unified Enter / click dispatch**: new `handleSendKey` / `handleSendClick` JS wrappers check `currentSendMode()` and dispatch to either `sendMeshMsg` (Raw) or `askLoracle` (Ask).
  - **`updateSendDropdown` hardened** to preserve the `Local only` sentinel through poll-driven rebuilds while Ask mode is active, and to never list the sentinel as a concrete node.
  - **Send button is briefly locked** during an Ask request so repeated Enter presses don't fire multiple LLM calls in parallel.
  - **Messages tab log shows both the question and the answer** via the existing `record_message("in"/"out", "dashboard", ...)` path, with `dest_label` reflecting the transmission choice (`local`, `broadcast ch0`, `DM to !abc...`).
  - **Fixes the user's "typed in dashboard, got no response" confusion**: the Send Message form was always a raw passthrough to `interface.sendText()`; since the local radio doesn't hear its own transmissions, `_on_receive` never fired and the LLM never saw the question. Ask mode gives the dashboard a proper loopback.

- Why:
  - The operator couldn't query their own LORACLE from the dashboard without using a second Meshtastic node to DM from. Raw send looked like it should work ("I typed 'Loracle, do I need a ham license?' and got nothing") but it just broadcast plain text onto the mesh with no LLM dispatch. Ask mode is the proper entry point — talk to LORACLE directly, optionally share the answer with specific nodes or the public channel so the whole mesh benefits from one Q&A.

- Reused code (no new plumbing):
  - `_bridge._handle_command(node_id, text)` for `!` commands — same path `_processing_loop` uses.
  - `_bridge.rag_engine.build_context_messages(text)` for RAG context, when enabled.
  - `_bridge.ollama.chat(node_id, text, context_messages=...)` for regular questions.
  - `_bridge._send_response(node_id, content, channel=, is_dm=)` for mesh transmission — handles chunking, `!more` pager state, retries, interface health checks.
  - `record_message("in"/"out", "dashboard", text, dest_label=)` for the Messages tab log (the `dest_label` collision with the function's first positional arg was fixed previously in commit `3aafd1f`).
  - Existing Send Message form HTML, recipient dropdown (unioned from `known_nodes` + `node_positions` + current selection), channel selector, and message log rendering.

- Files modified:
  - `meshtastic-bridge/dashboard.py` — new `/api/ask` endpoint, Mode selector + hint UI in the Send Message card, `askLoracle` + `handleSendKey` + `handleSendClick` + `updateSendMode` + `currentSendMode` JS, sentinel-aware rewrite of `updateSendDropdown`.

- Verified (REPL + Flask test client with a stub bridge):
  - Empty text → 400
  - `!help` → command dispatch path (not Ollama), local only, no TX
  - Regular question + `dest=local` → Ollama path, no TX
  - Regular question + `dest=broadcast` + `channel=0` → `_send_response` sends to `^all` on ch 0
  - Regular question + `dest=!abc12345` → `_send_response` DMs the target
  - Index HTML contains the new Mode selector, Ask LORACLE option, `askLoracle` / `handleSendClick` / `updateSendMode` functions

---

## [2026-04-07] — Coverage Tab: Auto-Refresh, Disconnected Banner, Clear Log

- What changed:
  - **Auto-refresh** the Coverage tab while it's open. The poll loop now calls `loadCoverage()` every ~10 s when `App.currentTab === 'coverage'`, tracked via a new `App.lastCovRefresh` timestamp. Previously the coverage data only refreshed on tab open or manual Refresh click — stale samples accumulated in `_covSamples` while users watched.
  - **Disconnected banner** on the Coverage tab. New `<div id="cov-banner">` element + `updateCovBanner(state)` JS function that show an amber "Bridge disconnected — coverage data shown is from the last connected session" warning whenever `state.connected === false`. Updated on every poll tick so it appears/disappears as the radio link drops and recovers.
  - **Clear log button** on the Coverage tab toolbar (red border styling to flag it as destructive). Calls a new `POST /api/coverage/clear` endpoint that truncates `~/.mesh-llm/coverage.jsonl` and resets the in-memory throttle cache so the next sample is logged immediately.
  - **`CoverageLogger.clear()`** method added to `coverage_logger.py`: counts samples first (so the API can report removed count), truncates the file, and clears `self._last` so the throttle doesn't carry over.

- Why:
  - User reported "I don't have a node connected but I see a heatmap" — confused by stale samples persisting across sessions and the lack of any indication that the data was historical. The auto-refresh keeps the view current while connected, the banner explicitly tells the user when data is stale, and the Clear button gives them a way to start fresh without manually deleting the JSONL file.

- Files modified:
  - `meshtastic-bridge/coverage_logger.py` — `clear()` method
  - `meshtastic-bridge/dashboard.py` — `/api/coverage/clear` endpoint, banner HTML, Clear button, `updateCovBanner` + `clearCoverage` JS, poll-loop auto-refresh, `App.lastCovRefresh` state

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
