# Project Context

This file tracks the history of changes, decisions, and current state of LORACLE BRIDGE.

---

## [2026-04-17 16:30] — LORACLE Bridge v2 Phase 1: dual-radio Meshtastic + MeshCore (software-complete)

- What changed:
  - **New source-of-truth document**: `LORACLE_BRIDGE_V2_FSD.md` at worktree root — living coordination file with phased plan, decisions log, progress log, and explicit "update on every change" instructions for agents across context windows. Scope: keep LORACLE's UI + AI layer; add MeshCore bridging like AkitaBridge but AI-enhanced. Decisions locked for v2: per-channel allowlist bridge policy (AI-gated as upgrade in Phase 4), branch not fork, rely on upstream `meshcore` lib over porting Akita's serial variants.
  - **Protocol parameterization in `standalone_bridge.py`**: 9 hardcoded `protocol="meshtastic"` strings and 3 hardcoded `f"meshtastic:channel:{channel}"` format strings replaced with a local/unpacked `protocol` variable. `_on_receive`, `_load_nodedb_positions`, and `_processing_loop` all derive protocol dynamically. `_request_queue` payload extended to 5-tuple `(protocol, sender, text, channel, is_dm)` so the processing loop knows which network to route a reply through.
  - **`--second-radio` now actually wired**: module-level `_parse_second_radio(spec)` helper supports `meshcore:serial:/dev/ttyUSB1`, `meshcore:tcp:HOST[:PORT]`, `meshcore:ble:[ADDR]`. `StandaloneBridge.__init__` accepts `second_radio` param and stores parsed config; `main()` passes `args.second_radio` through; `start()` spawns a background connect thread + ingest thread when a valid config is present. (Previously the flag was only saved to settings and did nothing.)
  - **New `_persist_incoming` helper method** (protocol-agnostic SQLite DB writes) extracted from `_on_receive` — shared between the Meshtastic pubsub path and the new MeshCore ingest loop. Single DB-write path across both networks.
  - **New `_connect_secondary_radio` method**: constructs `MeshCoreBackend` from the parsed config and registers via `_radio_manager.add_backend()`. Import-guarded against missing `meshcore` lib (logs warning, no-op), exception-guarded for connection failures (logs error, bridge continues on primary radio).
  - **New `_secondary_radio_ingest_loop` method**: drains `_radio_manager.get_message()` queue, converts `Protocol` enum (`"mc"`) to DB name (`"meshcore"`) via new `_PROTOCOL_SHORT_TO_DB` dict, rate-limits, persists via `_persist_incoming`, notifies addons, and enqueues onto the shared `_request_queue`. Skips meshtastic-tagged messages to avoid double-persist (pubsub path owns those).
  - **Protocol-aware send path**: `_send_raw` / `_send_response` accept `protocol="meshtastic"` parameter. When `"meshcore"`, routes via `_radio_manager.send(f"mc:{node_id}", …)` instead of `self.interface`. MeshCore sends use simple truncation at `MAX_LORA_TEXT` for Phase 1 — no paging / `!more` (Phase 2 defers). All 4 send call sites in `_processing_loop` pass protocol from the queue unpack.
  - **UI protocol badge**: `renderNodeList` in `dashboard.py` carries `isMC` through item mapping and renders a small purple `mc` badge next to MeshCore contacts. Meshtastic is the default (no badge = less visual clutter). New CSS classes `.lo-ns-proto` / `.lo-ns-proto-mc`.
  - **9 new unit tests** for `_parse_second_radio` covering valid serial/TCP/BLE specs, default ports, malformed inputs, unsupported primary protocol. 198/198 tests pass.
  - **README.md**: expanded "Supported Protocols" section with dual-radio usage examples (serial / TCP / BLE), Phase 1 limitation callouts (no cross-network bridging yet, truncation instead of paging on MeshCore, best-effort addon compat), and pointer to the FSD for the v2 roadmap.
- Why:
  - User reviewed an initial scoping analysis comparing LORACLE's architecture to AkitaBridge and asked for a concrete plan for a v2 that keeps LORACLE's UI + AI layer but adds Meshtastic ⇄ MeshCore bridging. The pitch: a dumb bridge is Akita — an AI-gated cross-protocol mesh relay is meaningfully differentiated for the defense-tech portfolio narrative (Anduril / Palantir / SOCOM). Audit revealed LORACLE was already ~90% there — `RadioManager` + `MeshCoreBackend` + multi-protocol DB schema already existed but `RadioManager` was deliberately disconnected (see the now-deprecated comment at standalone_bridge.py:338-341 warning against fighting with the proven `_radio_connection_loop`). Phase 1 wires the second backend through without disturbing the primary Meshtastic pubsub path; Phase 2 (deferred) adds the actual cross-network relay and policy layer.
- Impact on project goals:
  - LORACLE now runs as a true dual-backend bridge. Operators on Meshtastic and MeshCore networks can send DMs and channel messages to the same AI, with both persisted in the same SQLite, surfaced through the same dashboard, and visually distinguishable via the `mc` badge. This is the foundation for v2 Phase 2 (cross-protocol relay) and Phase 4 (AI-gated bridging — the differentiator). Zero behavior change when `--second-radio` is not provided: the existing Meshtastic-only path is identical to v1. The `LORACLE_BRIDGE_V2_FSD.md` gives any future agent (or a fresh context window) a clear picture of what's done, what's next, and what design decisions were locked in — so v2 development can continue without re-deriving context.
- Files modified:
  - `LORACLE_BRIDGE_V2_FSD.md` — new file (worktree root)
  - `meshtastic-bridge/standalone_bridge.py` — protocol parameterization, `_parse_second_radio`, `_persist_incoming`, `_connect_secondary_radio`, `_secondary_radio_ingest_loop`, protocol-aware send methods, `second_radio` init param, main() wiring
  - `meshtastic-bridge/dashboard.py` — `mc` badge in `renderNodeList`, CSS classes for protocol badge
  - `meshtastic-bridge/tests/test_bridge_data_structures.py` — 9 new unit tests for `_parse_second_radio`
  - `README.md` — dual-radio section with usage examples + Phase 1 limitations

---

## [2026-04-17 14:00] — Node-population fix + AI chat tab + send status + livelier sim + HTML docs

- What changed:
  - **New-node population bug**: Nodes that announced themselves via NodeInfo, telemetry, or traceroute packets never entered `_known_nodes`, so they were invisible until the 30s nodeDB rescan caught them (or forever if they never sent a text/position). Fixed by adding `self._known_nodes.add(sender)` to `_on_position`, `_on_telemetry`, `_on_traceroute`, and subscribing to a new `_on_user` handler on `meshtastic.receive.user` (NodeInfo broadcasts). `_on_user` also captures short_name/long_name/hw_model so labels and HW colors populate immediately.
  - **Map tab interaction**: Clicking a marker now opens the same thread panel used by the mesh canvas (via a new `_openMapNode(nid)` helper) instead of showing a popup with a two-click "Open" link. Marker tooltip replaces the popup. Favorited nodes on the map show a gold ring (`.lo-map-marker.fav`). Map markers use the resolved canvas label (custom_name → long → short).
  - **Message status**: `/api/threads/<id>/send` now inserts the outbound message with `delivery_status='sending'` up-front, then transitions to `sent` on success or `failed` if Meshtastic raises. Frontend `renderDeliveryStatus()` covers five states — sending (pulsing ⧗), sent (→), acked (✓), delivered (✓✓), failed (✗) — with a distinct CSS color/animation per state. `floatSend` is now optimistic: the message appears in the thread instantly with `sending` before the POST returns, then reconciles with server truth.
  - **AI chat tab**: New top-level "AI" tab lets the user chat with the local Ollama model directly from the dashboard — no radio required. Keyed off a reserved node id `__dashboard_ai__` so its history is isolated from mesh conversations. Endpoints: `POST /api/ai_chat`, `GET /api/ai_chat/history`, `POST /api/ai_chat/clear`. Uses the existing `OllamaClient.chat()` with its system prompt.
  - **HW color on by default**: New installs start with HW-model coloring enabled. localStorage is only consulted to let a user explicitly turn it off.
  - **Emergency-Preparedness pack install fixes**:
    - Added HTML/HTM support to the RAG extractor via the existing `_strip_html()` helper + BeautifulSoup — the Ready.gov, CDC, and NHC docs now ingest cleanly instead of erroring "Unsupported file type: .html".
    - Fetcher now rejects zero-byte downloads (treats as retryable failure + removes the empty file) so `irp.fas.org`'s intermittent empty responses don't poison the install.
    - `extractors.extract_file` now pre-checks for zero-byte files with a clear error.
    - Bumped manifest to v1.1.0 — marked FEMA P-320, FM 4-25.11, and both Hesperian Health docs as `required: false` (their upstream URLs move without redirect and 404 frequently), so those failures don't trip the installer's "less than 50% of required docs succeeded" abort gate.
    - `PackDocument` dataclass gained an optional `note` field for per-doc caveats; `from_file` now tolerates unknown keys so forward-compatible manifests don't break older bridges.
  - **Livelier mesh simulation**: Tuned the d3 force sim so nodes feel "alive" at rest — `alphaDecay` 0.04 → 0.012 (slower cool-down), `velocityDecay` 0.5 → 0.32 (momentum carries), `alphaMin` 0.002 (never freezes). Added a custom `jitter` force (~0.35 strength × alpha) that applies small random velocity kicks to non-self nodes each tick, and a 6-second reheat interval that nudges alpha back up if it drops below 0.08. Self-node and any `fx`-pinned nodes are excluded from jitter.
- Why:
  - The user reported new nodes weren't populating after the bridge was running — confirmed by code read that NodeInfo/telemetry-only nodes never hit any tracking set. The pack download logs showed a broken install path (dead URLs + no HTML support + 0-byte files). The map tab had only tooltip-level interaction vs the mesh tab's full thread panel. They also asked for clearer message state and a local AI chat surface — and wanted the canvas to feel more alive.
- Impact on project goals:
  - New nodes appear in the UI the moment their first packet lands, regardless of packet type. The Emergency-Preparedness pack installs on the happy path for the docs whose URLs still work, and HTML docs (Ready.gov, CDC) now actually ingest. Map-tab and mesh-tab interaction parity means the app now has a consistent "click a node to work with it" affordance across all views. The AI tab gives users a zero-radio on-ramp to the local LLM — useful for offline reference queries that don't need to traverse the mesh.
- Files modified:
  - `meshtastic-bridge/standalone_bridge.py` — NodeInfo subscription, `_known_nodes.add` in position/telemetry/traceroute handlers, new `_on_user` handler
  - `meshtastic-bridge/dashboard.py` — `_openMapNode`, map marker rebind, sending/sent/failed status flow, optimistic `floatSend`, AI tab HTML/CSS/JS, `/api/ai_chat*` endpoints, HW color default flip, `_jitterForce` + reheat timer, simulation config tune
  - `meshtastic-bridge/rag/extractors.py` — HTML/HTM support + empty-file pre-check + `extract_html` helper
  - `meshtastic-bridge/packs/fetcher.py` — zero-byte response treated as retryable failure
  - `meshtastic-bridge/packs/manifest.py` — `note` field on PackDocument, `notes` field on PackManifest, forward-compatible unknown-key tolerance
  - `meshtastic-bridge/packs/bundled/emergency-preparedness-v1.json` — v1.1.0, marked four best-effort docs as optional

---

## [2026-04-17 11:00] — Hardware-debug pass + favorites/rename + richer node interaction

- What changed:
  - **TX/RX diagnostic fixes**: `/api/threads/<id>/send` now checks `_is_interface_alive()` before sending and returns `503` with a clear error; the frontend composer preserves the user's text on failure so they can retry. `_send_raw()` now logs a `WARNING` on silent drops instead of failing quietly. New `DEBUG_WANT_ACK=1` env var flips `wantAck=True` on every send for live diagnosis without changing the battery-friendly default.
  - **Animation polish**: signal pulses now ease out (`t * (2 - t)`) so they accelerate off the source and settle at the destination; breathing bumped to ±1.8px radius + ±0.08 alpha oscillation; sonar ring uses smooth quadratic fade instead of hard clamp; new-node entrance is 1.2s with an expanding ring flash; renderer targets 60fps only while animations are active, 30fps at rest.
  - **New-node-detected toast**: mirrors the disconnect-alert pattern — tracks the known-nodes set across polls and fires a single info toast when a new node appears mid-session (guards against toast spam on initial load).
  - **Node/line overlap fix**: link endpoints are now shortened by `nodeRadius(n) + 2px` on each side, so the lines stop at the edge of the circle instead of bleeding through the middle.
  - **HW-model color toggle**: new HUD button colors nodes by hardware model (T-Beam blue, Heltec green, RAK amber, T-Deck red, Station teal, Nano purple). Legend renders under the HUD listing models currently in view. Preference persists in `localStorage`.
  - **Favorites**: new `is_favorite` column on `contacts` with idempotent ALTER-TABLE migration. `POST /api/threads/<id>/favorite` toggles. Canvas renders a gold star next to favorited nodes; sidebar sorts favorites to the top within any sort mode.
  - **Custom nicknames**: new `custom_name` column. `POST /api/threads/<id>/rename` (null or empty clears). Double-click the panel header to edit inline (Enter commits, Esc cancels); RENAME button in the actions row does the same. Label resolution is `custom_name → long_name → short_name → last-4-of-id`.
  - **Richer map-node interaction**: clicking a node on the mesh canvas now opens a larger (420×540) thread panel positioned in the upper-right (so it feels persistent, not tooltip-like), shows full message history instead of truncating to 15, sets `App.selectedNode` (which already drove a selection ring in the renderer but was never populated), and closes other panels so the focused node is unambiguous. Click radius widened 40→60px. `cursor: pointer` now appears when hovering a hittable node. Dangling `openNodePanel()` reference wired up to a real function.
- Why:
  - The user reported messages sometimes not transmitting on the hardware and asked me to rule out the software side before they poke the radio. That turned into a broader UX pass covering polish items they'd been sitting on.
- Impact on project goals:
  - Silent TX failures are now surfaced in logs and the UI, so hardware debugging is actionable instead of guess-and-check. Map nodes behave like first-class contacts (message, star, rename, see history) rather than read-only tooltips. Favorites + nicknames make it possible to personalize a mesh with dozens of nodes.
- Files modified:
  - `meshtastic-bridge/dashboard.py` — send-endpoint alive check, `contact_meta` in `/api/state`, favorite + rename endpoints, animation loop polish, new-node toast, link shortener, HW color toggle, favorite/rename frontend (buttons, inline edit, star glyph), widened click radius, hover cursor, larger thread panel
  - `meshtastic-bridge/standalone_bridge.py` — `_send_raw` warning log, `DEBUG_WANT_ACK` wiring in both send paths
  - `meshtastic-bridge/db/schema.py` — new `is_favorite` and `custom_name` columns + idempotent `_ensure_contact_columns()` migration
  - `meshtastic-bridge/db/contacts.py` — `toggle_favorite`, `set_custom_name`, `get_display_meta` methods
  - `meshtastic-bridge/tests/test_dashboard_api.py` — mock now stubs `get_display_meta` so `/api/state` serializes cleanly
  - `README.md` — documented favorites, rename, HW color toggle, new-node toast, DEBUG_WANT_ACK env var
  - `.claude/launch.json` — added `loracle-bridge-verify` profile on port 8001 for safe verification alongside a running production instance

---

## [2026-04-17] — Feature Roadmap: Meshtastic Parity + UX Improvements

- What changed:
  - **Canvas panning**: click-drag to pan viewport, double-click to reset
  - **Realistic mesh topology**: hop-chained links instead of star pattern
  - **Unread badges**: orange count badges on canvas nodes
  - **Node list sidebar**: sortable/filterable node panel
  - **Interactive map view**: 4th view (MAP) with Leaflet, node markers, auto-fit bounds
  - **Battery/device metrics**: live telemetry (battery, voltage, temp, humidity, channel util, HW model) in float windows and canvas (red dot for low battery)
  - **Traceroute**: TRACE button in float windows
  - **Device admin**: REBOOT and SHUTDOWN buttons in CONFIG
  - **Message delivery status**: checkmarks on outbound messages (✓ acked, ✓✓ delivered, ✗ failed)
  - **Channel management**: CONFIG section showing all radio channels with role, encryption, uplink/downlink
  - **Radio configuration**: CONFIG section for LoRa region, modem preset, TX power, hop limit — reads/writes to radio
  - **Page crash fix**: moved canvas mousemove listener inside initCanvas()
  - **Connect modal fix**: stays visible on boot until user interaction
  - **Float window chat fix**: messages now persist in thread history
  - **Magnetism fix**: closest-only attract, others repel
- Why:
  - Feature comparison against official Meshtastic web client revealed core gaps. Implemented all priority features while keeping LORACLE's retro-modern canvas identity.
- Impact on project goals:
  - Dashboard is now a fully functional mesh management tool: map, channels, radio config, device admin, telemetry — alongside the unique AI/LLM features.
- Files modified:
  - `meshtastic-bridge/dashboard.py` — all frontend + API endpoints
  - `meshtastic-bridge/standalone_bridge.py` — telemetry subscription, device metrics
  - `meshtastic-bridge/db/messages.py` — update_status method

---

## [2026-04-16 22:00] — Phase 1: Canvas Panning, Mesh Topology, Unread Badges, Node Sidebar

- What changed:
  - **Canvas panning**: click-and-drag to move the viewport, double-click to reset. All hit detection and magnetism properly offset by pan coordinates.
  - **Realistic mesh topology**: replaced star pattern (all nodes → center) with hop-chained links. Direct nodes connect to MY NODE, 2-hop nodes link to nearest direct node, etc. Creates tree-like layout mimicking actual relay paths.
  - **Unread badges**: orange count badges drawn on canvas nodes with unread messages. Fetched from /api/threads every ~10s.
  - **Node list sidebar**: hamburger button in title bar opens a right-side panel listing all nodes. Sort by name/hops/last heard/unread, filter by search, click row to open floating window.
  - **Page crash fix**: moved canvas mousemove listener inside initCanvas() — was crashing at parse time when App.canvas was null.
  - **Magnetism fix**: only closest node attracts, others repel. Subtle forces prevent blobbing.
  - **Float window chat fix**: now uses /api/threads/<id>/send so messages persist in thread history.
  - **Connect modal fix**: stays visible on boot until user interaction, shows "RADIO CONNECTED" on auto-connect.
- Why:
  - Feature comparison against Meshtastic web client revealed core UX gaps. Canvas was fixed-viewport with star topology — nodes outside view were invisible and layout didn't reflect actual mesh routing.
- Impact on project goals:
  - Dashboard now visualizes actual network topology. Users can pan to see all nodes, browse via sidebar, and see unread counts at a glance.
- Files modified:
  - `meshtastic-bridge/dashboard.py` — all changes (HTML, CSS, JS)

---

## [2026-04-16 21:00] — Fix: Connect Modal Stays Visible on Boot

- What changed:
  - Fixed race condition where the connect modal flashed invisibly on page load because the backend auto-connected before the first poll completed, immediately hiding the modal
  - Added `_userAckedModal` gate — poll loop cannot auto-hide the modal until user clicks CONNECT, DISMISS, or picks a BLE device
  - When backend auto-connects while modal is still showing, text updates to "RADIO CONNECTED" and auto-dismisses after 3 seconds
  - Auto-dismiss timer is cancelled if radio disconnects while modal is still visible
- Why:
  - Connecting a radio is the mandatory first step. Users were never seeing the connect modal because it appeared for one frame then vanished due to the auto-connect race condition.
- Impact on project goals:
  - Ensures first-run UX works correctly — users always see the connect dialog on boot
- Files modified:
  - `meshtastic-bridge/dashboard.py` — modal HTML (added IDs), JS state vars, checkConnectionForModal rewrite, user interaction tracking in dismiss/connect/pick functions

---

## [2026-04-16] — Network-as-Canvas: Force-Directed Mesh UI

- What changed:
  - **Complete frontend rewrite** of dashboard.py. The mesh IS the interface — one living force-directed graph replaces the previous messenger sidebar + dashboard split.
  - **Canvas visualization** using d3-force: MY NODE at center (orange, sonar pulse), peer nodes on hop rings, channel nodes as hexagons, links weighted by signal strength, idle breathing animations, packet pulse trails.
  - **Click-to-interact**: clicking a node opens a detail panel (slides in from right) with telemetry, message thread, DM composer, and AI toggle. Canvas stays alive behind the panel.
  - **HUD overlay**: top-left stats (nodes, messages, model, uptime) + SCAN MESH button. Bottom: packet activity ribbon.
  - **View filters**: MESH (default), TRAFFIC (active links), CONFIG (settings).
  - **Full CONFIG view** rebuilt: connection, AI replies, model, model routing with classifier test, response settings, knowledge base, knowledge packs, data & storage with factory reset, appearance, about.
  - **SCAN MESH button** + `/api/nodes/refresh` endpoint for on-demand nodeDB rescan after initial connection.
  - **Dashboard.py reduced** from 4869 → ~2500 lines (49% reduction).
  - **Dependencies**: d3-force v3 + d3-quadtree + d3-dispatch + d3-timer (~17KB total).
- Files changed:
  - `meshtastic-bridge/dashboard.py` — complete DASHBOARD_HTML rewrite
  - `meshtastic-bridge/static/js/d3-*.min.js` — new (4 files)
  - `README.md` — updated for canvas UI
  - `CONTEXT.md` — this entry
- What was NOT changed:
  - All Python backend code (zero API changes)
  - SQLite persistence, thread infrastructure, model routing, knowledge packs
  - All 51 API endpoints

---

## [2026-04-16] — Session 5: RAG Packs + Emergency Preparedness Pack

- What changed:
  - **New `packs/` module** — named, discoverable bundles of documents installable with one click:
    - `manifest.py`: PackManifest/PackDocument dataclasses, JSON loader
    - `registry.py`: discovers bundled manifests from `packs/bundled/*.json`
    - `fetcher.py`: downloads docs from publisher URLs with retry + SHA-256 + progress callback
    - `installer.py`: orchestrates fetch → ingest pipeline, records to DB, supports uninstall + reingest
  - **Emergency Preparedness pack** (`packs/bundled/emergency-preparedness-v1.json`): 12 curated docs from FEMA, U.S. Army (FM 21-76, FM 4-25.11, FM 3-25.26), Hesperian (Where There Is No Doctor/Dentist), NOAA, CDC, Ready.gov. ~280MB estimated. All public domain or CC BY-NC.
  - **New DB tables**: `installed_packs` (pack install records + manifest snapshots) and `pack_documents` (per-doc fetch/ingest status + SHA-256 + chunk counts)
  - **Pack API endpoints**: `GET /api/packs`, `GET /api/packs/<id>`, `POST /api/packs/<id>/install`, `POST /api/packs/<id>/uninstall`, `POST /api/packs/<id>/reingest`
  - **KNOWLEDGE PACKS** CONFIG section: shows available packs with install status, detail drawer with document list + chunk counts, install/uninstall/reingest buttons
  - Install progress emitted via SSE for real-time UI updates
  - **Metadata-only distribution**: no document binaries in repo — everything fetched from publisher URLs at install time
- Files changed:
  - `meshtastic-bridge/packs/` — new module (5 files + bundled manifest)
  - `meshtastic-bridge/db/schema.py` — added installed_packs + pack_documents tables
  - `meshtastic-bridge/dashboard.py` — pack API endpoints + KNOWLEDGE PACKS config section

---

## [2026-04-16] — Session 3: Auto Model Routing

- What changed:
  - **New `routing/` module** — three-tier auto model routing:
    - `tiers.py`: Tier enum (TINY/STANDARD/BIG), TierConfig dataclass, RAM-based defaults (<8GB: big disabled, 8-16GB: big disabled, 16GB+: all enabled), load/save to settings table
    - `classifier.py`: hybrid length + keyword classifier (v1.0). Rules: trivial <=20 chars → TINY, BIG keywords (explain, triage, debug, etc.) → BIG, TINY patterns (what is, who is, etc.) → TINY, >160 chars → BIG, multi-question → BIG, default → STANDARD. Pure function, <1ms runtime.
    - `router.py`: resolves query + optional override to (tier, model). Validates tier enabled + model installed. Raises ModelDisabledError / ModelNotInstalledError with user-facing <=180 char error messages.
  - **CLI prefix overrides**: `!tiny <query>`, `!std <query>`, `!big <query>` force a tier. Case-insensitive, prefix stripped before Ollama call. Prefix-only sends usage hint.
  - **Tier-aware RAG**: TINY skips RAG (fast answers), STANDARD uses 3 chunks, BIG uses 6 chunks.
  - **Per-call model override** in `ollama_client.py`: `chat()` accepts `model_override` parameter.
  - **Dashboard**: tier tags `[TINY]/[STD]/[BIG]` on AI messages in both messenger and dashboard feed. New MODEL ROUTING config section with auto-routing toggle, show-tag toggle, per-tier model/enabled inputs, save button, and live classifier test tool.
  - **API endpoints**: `GET/POST /api/routing/config`, `POST /api/routing/classify` (test classifier without LLM call)
  - **16 new tests**: classifier corpus (42 queries, >=80% accuracy), prefix parsing, router validation, speed benchmark
- Files changed:
  - `meshtastic-bridge/routing/` — new module (4 files)
  - `meshtastic-bridge/ollama_client.py` — model_override on chat()
  - `meshtastic-bridge/standalone_bridge.py` — routing integration in processing loop
  - `meshtastic-bridge/dashboard.py` — tier tags, MODEL ROUTING config, classifier test, routing API
  - `meshtastic-bridge/tests/test_classifier.py` — 16 tests

---

## [2026-04-16] — Session 2b: Full Messenger UI + SQLite Persistence

- What changed:
  - **New `db/` module** — SQLite persistence at `~/.mesh-llm/loracle.db`:
    - `contacts` table: per-contact AI toggle (inherit/off/on), unread counts, protocol, last heard
    - `messages` table: full message history with direction, author (human/ai), delivery status, channel origin tracking
    - `settings` table: key-value store replacing settings.json
    - Retention pruning: 500 messages/contact cap + 90-day cutoff, runs hourly
    - Migration from settings.json on first run (renames to .bak)
  - **Messenger view** — new default landing page:
    - MESSENGER/DASHBOARD segmented toggle in title bar + gear icon for CONFIG
    - Two-pane layout: 320px sidebar + thread view
    - Sidebar: DMs/Channels/All tabs, search, contact list with square avatars + protocol indicators + unread badges
    - Thread view: header with AI toggle (inherit/off/on cycle), scrollable messages with direction arrows + AI badges, composer with 233-char counter
    - Auto-refresh every 3s via polling
  - **Channel support**: channel messages stored under channel contacts (`meshtastic:channel:N`), AI replies redirect to sender as DM (never posted to channel), `originating_channel_id` tracked
  - **Per-contact AI toggle**: effective state resolves per-contact override vs global default; commands always processed regardless
  - **SSE events**: `/api/events` now pushes real `thread_updated` events when messages arrive
  - **DATA & STORAGE** section in CONFIG: db path, stats, retention info, prune now + clear all buttons (replaces old HISTORY section)
  - **Onboarding**: expanded to 6 steps with new "EVERY CONTACT IS A THREAD" step
  - **13 new API endpoints**: `/api/threads`, `/api/threads/<id>`, `/api/threads/<id>/messages`, `/api/threads/<id>/open`, `/api/threads/<id>/send`, `/api/threads/<id>/ai-toggle`, `/api/events`, `/api/db/stats`, `/api/db/prune`, `/api/db/clear-messages`
- Files changed:
  - `meshtastic-bridge/db/` — new module (5 files)
  - `meshtastic-bridge/standalone_bridge.py` — db init, contact upsert, message persist, channel logic, per-contact AI
  - `meshtastic-bridge/dashboard.py` — messenger view, SSE, new endpoints, data storage config, onboarding
  - `meshtastic-bridge/tests/test_db.py` — 28 tests
  - `meshtastic-bridge/tests/test_dashboard_api.py` — mock updates

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
