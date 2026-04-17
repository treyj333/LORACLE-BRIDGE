# LORACLE Bridge v2 — Functional Specification Document (FSD)

**Status:** All phases 1–5 software-complete (commits 5ae05aa · e4280c7 · c7c9efa · faf54e7 · 9582269 · ff5067a · 9a1de79). Hardware-verification gates pending user testing. LLM-rewrite deferred to v2.1.
**Started:** 2026-04-17
**Owner:** Marvin Johnson
**Document type:** Living source-of-truth — a temporary coordination doc that tracks v2 work across context windows. Retire / archive once v2 ships.
**Current branch:** `claude/loving-banach-b58fc3` (worktree: `loving-banach-b58fc3`)

---

## 🛑 READ THIS BEFORE ANY WORK — INSTRUCTIONS FOR AGENTS 🛑

**This file is the source of truth for LORACLE Bridge v2 development across context windows.**

When you (any agent / any new context window) pick up work on this project:

1. **READ THIS FILE FIRST**, including the Progress Log and Decisions Log at the bottom, before making any code changes. It tells you *what's done, what's next, and what was decided*.
2. **UPDATE PROGRESS AS YOU GO — the moment a task completes, not batched:**
   - Flip checkboxes (`- [ ]` → `- [x]`) immediately when a task is done
   - Add new sub-tasks (`- [ ] ...`) under the relevant phase when you discover them mid-work
   - Append a dated entry to the **Progress Log** at the bottom summarizing what you changed, the files touched, and any surprises
3. **RECORD DECISIONS:** If a design question gets resolved mid-work, add it to the **Decisions Log** with date + rationale. Don't just fix the code — write down *why*.
4. **FLAG BLOCKERS:** If something blocks progress, add it to **Open Questions** with enough context for a future agent to pick it up cold.
5. **DO NOT DELETE** completed phases, finished checkboxes, or past progress log entries. They're history. This file is additive.
6. **KEEP IT HONEST:** Don't mark tasks done that aren't done. Don't silently move goalposts — if scope changes, say so in the Decisions Log.
7. **PAIR THIS WITH `CONTEXT.md`:** CONTEXT.md is the project-wide change history; this FSD is the v2-scoped working plan. Update both per the project standards checklist when a phase lands.

Treat this file like a commit log: additive, dated, honest about what's incomplete.

---

## Goal

Keep LORACLE Bridge's existing UI + LLM/AI layer. Add Meshtastic ⇄ MeshCore bridging (like AkitaBridge does, but AI-enhanced). Ship as v2 of LORACLE Bridge.

## Why this matters

- **Defense-tech portfolio narrative**: an AI-gated cross-protocol mesh relay is meaningfully differentiated vs. a dumb bridge (Anduril / Palantir / SOCOM angle)
- **Leverage existing infrastructure**: LORACLE is already ~90% there — has `RadioManager`, `MeshCoreBackend`, protocol-aware DB schema
- **Ecosystem positioning**: marks LORACLE as the "AI bridge" layer that addons (Sentinel, Triage, Brief, Dead Drop) plug into across both mesh protocols

---

## Scope Decisions (locked — see Decisions Log for rationale)

1. **Evolve on branch**, not fork. Same repo.
2. **Bridge policy: Per-channel allowlist (C) first; AI-gated (B) layered in Phase 4 as optional per-channel upgrade.** Not always-on relay.
3. **MeshCore serial: rely on upstream `meshcore` Python lib.** Port Akita's three protocol variants only if hardware compat breaks.

## Out of scope for v2

- MQTT bridging (Akita has it; not needed for defense-tech narrative)
- Federation across multiple LORACLE instances
- Non-text message types across the bridge (positions, telemetry) — defer to v2.1
- Re-skinning the UI. Same dashboard; add a BRIDGE tab.

---

## Phased Plan

> **Legend:** `- [ ]` = pending, `- [x]` = done, `- [~]` = in progress / partial. Every phase ends with a **Gate** — a verifiable acceptance check. Don't move to the next phase until its gate passes.

### Phase 1 — Multi-backend parity
**Goal:** LORACLE reliably runs both Meshtastic and MeshCore backends simultaneously. UI shows both. No cross-network bridging yet.

**Architecture decision (2026-04-17, post-audit):** Keep the proven Meshtastic pubsub path (`_radio_connection_loop` + `pub.subscribe`) as primary. Wire the secondary MeshCore radio through the already-instantiated-but-disconnected `_radio_manager`. Both paths feed into the same `_request_queue` (extended to carry protocol) and the same `_message_store` (protocol-tagged correctly). This avoids the known regression the `# kept as library code` comment at `standalone_bridge.py:338-341` warns about.

**Audit findings — confirmed state of the code (2026-04-17):**
- `_radio_manager = RadioManager()` exists at `standalone_bridge.py:200` but is never wired to any backend
- `MeshtasticBackend` is constructed at `standalone_bridge.py:201` but never registered or connected
- `--second-radio` CLI flag exists (`standalone_bridge.py:1795`) and is saved to settings but NOT wired to backend creation
- Hardcoded `protocol="meshtastic"` at lines: **786, 794, 800, 803, 810, 1069, 1153, 1246, 1257** (and channel-id format strings at 800, 1153, 1246)
- `_request_queue` payload is `(sender, text, channel, is_dm)` — no protocol field
- `_send_response` / `_send_raw` go straight to `self.interface` (meshtastic lib); not protocol-aware

**Tasks:**
- [x] Audit `standalone_bridge.py` for hardcoded `protocol="meshtastic"` strings — complete, findings above
- [x] Extend `_request_queue` payload to carry `protocol` (5-tuple `(protocol, sender, text, channel, is_dm)`)
- [x] Parse `--second-radio PROTOCOL:TRANSPORT:PARAMS` string in `main()`. On valid `meshcore:…`, construct `MeshCoreBackend` and register via `_radio_manager.add_backend()` (in `_connect_secondary_radio`)
- [x] Add new thread `_secondary_radio_ingest_loop` that pulls `UnifiedMessage` from `_radio_manager.get_message()` and enqueues onto `_request_queue` with `protocol="meshcore"`
- [x] Parameterize `_on_receive`'s DB inserts via new `_persist_incoming` helper (removes hardcoded strings at 786/794/800/803/810)
- [x] Parameterize the channel-id format: `f"{protocol}:channel:{channel}"` (derived, not hardcoded) at 3 sites
- [x] Parameterize `_refresh_nodes` contact upsert — uses local `protocol = "meshtastic"` variable
- [x] Teach `_send_response` / `_send_raw` to accept `protocol` parameter — when `"meshcore"`, route via `_radio_manager.send(f"mc:{node_id}", …)`; `"meshtastic"` default keeps the proven path
- [x] UI: `mc` badge on each MeshCore node in the node list (purple, `.lo-ns-proto-mc` class)
- [~] **Gate (software):** CLI `--help` parses, imports clean, all 198 tests pass, method signatures inspected — verified
- [ ] **Gate (hardware, user-action):** start bridge with `--serial /dev/ttyUSB0 --second-radio meshcore:serial:/dev/ttyUSB1`. Send a DM from the UI to a Meshtastic and a MeshCore node — both land. Node list shows mt nodes without badge and mc nodes with purple `mc` badge. Regression-check: bridge without `--second-radio` behaves identically to pre-v2.

### Phase 2 — Bridge core
**Goal:** New `bridge/` module relays messages *between* backends. Loops prevented. Identity mapped.

- [x] Create `meshtastic-bridge/bridge/` package
- [x] `bridge/relay.py` — observes via `Relay.observe()`, applies policy, re-injects via caller-supplied `send_fn` (avoids coupling to RadioManager for primary meshtastic path)
- [x] `bridge/policy.py` — pluggable base class + implementations: `DisabledPolicy`, `AlwaysRelay`, `ChannelAllowlist`, `AIGatedPolicy` (Phase 2 stub → real classifier in Phase 4)
- [x] `bridge/dedup.py` — `RelayDedupCache` keyed on `(source_protocol, dest_protocol, sender, payload_hash)` with sliding TTL
- [x] Loop prevention: `bridge/identity.py` — `looks_bridged()` matches `^\[(mt|mc)-[^\]]+\]\s` prefix and short-circuits in Relay
- [x] Identity mapping: sender-prefix rendered as `[mt-Alice] payload`; `_bridge_sender_display` resolves custom_name → long_name → short_name → last-6 of native id
- [x] Config: persisted as JSON blob in SettingsStore under `bridge_config` (per-channel rules: source, channel, mode=off/always/ai-gated)
- [x] Config loader: `bridge/config.py::build_policy` composes the right Policy graph from the blob, gracefully drops malformed rules
- [x] API: `GET/POST /api/bridge/config` hot-swaps the live policy on save; `GET /api/bridge/stats` for counters; `GET /api/bridge/events` ring buffer
- [x] **Gate (software):** 54 new unit tests covering identity/dedup/policy/relay (loop guards, dedup, every policy class, send failures, hot-swap, on_relay hook, stats counters). 252/252 pass.
- [ ] **Gate (hardware):** text on Meshtastic channel 0 appears on MeshCore (with sender prefix), no loops during 60s soak test, no duplicates. → pending user hardware verification.

### Phase 3 — UI surface
**Goal:** Operators can see and control the bridge from the dashboard.

- [x] New "BRIDGE" tab in `dashboard.py` (button after AI, hidden from ribbon since relay stats aren't per-packet)
- [x] Live flow log (mt↔mc direction arrows, timestamps, sender display, truncated payload). 200-row cap matches server ring buffer.
- [x] Per-channel rule editor: source picker / channel input / mode dropdown (off/always/ai-gated) with APPLY + RELOAD. Dirty-state indicator.
- [x] Stats bar: relayed / dropped / dedup cache size — polled every 2.5s while the tab is visible.
- [x] Live ON/OFF badge reflects `_bridge_config.enabled`.
- [x] SSE: `_on_bridge_relay` emits `"bridge.relay"` events via the existing `_emit_sse` channel so future clients can subscribe instead of polling.
- [x] **Gate (software):** 252/252 tests pass; manual UI smoke deferred to hardware verification.

### Phase 4 — AI-gated relay (the differentiator)
**Goal:** LLM decides what crosses. The defense-tech pitch.

- [x] `bridge/urgency.py` — `HeuristicUrgencyClassifier`. Keyword + structure heuristic covering distress / medical / fire-disaster / threat / stuck-lost vocabulary. Word-variant aware (flood/flooding, shot/shots, attack/attacked). Chatter allowlist (hi/roger/copy/thanks) always false. Fail-open.
- [x] `build_policy` plugs classifier into `AIGatedPolicy` for every ai-gated rule; `cfg.urgent_keywords` extends the vocabulary at runtime.
- [x] `!urgent` / `!priority` / `!sos` / `!mayday` prefixes (case-insensitive, optional `:`/`,`/`-` separator) force relay past any policy. Prefix is stripped before relay. DMs still never cross. Bang-word-alone drops as a no-op.
- [x] **Gate (software):** 13 urgency tests + 11 force-relay tests; plus build_policy test updated for live classifier. 276/276 pass.
- [~] LLM rewrite mode (Ollama-backed summarisation/translation) — **deferred to v2.1**. The classifier gate is the Phase 4 defense-tech story; rewrite layers on later without API changes.
- [ ] **Gate (hardware):** demo — chatty Meshtastic channel, AI drops noise, relays urgent. → pending user hardware verification.

### Phase 5 — Hardening
**Goal:** production-ready defaults.

- [x] `bridge/rate_limit.py` — `RelayRateLimiter`, thread-safe sliding-window keyed per (source, dest, channel). Default 30 events / 60s, tunable via `rate_limit_max` / `rate_limit_window_s` in config. Force-relay bypasses the limit. Rejected events don't consume quota.
- [x] `db/bridge_events` SQLite table + `BridgeEventStore`. CHECK-constrained outcome (`relayed`/`blocked`/`rate_limited`/`deduped`/`loop_guard`), 30-day retention pruned on startup. `GET /api/bridge/history` returns recent events with filters.
- [x] Addon API: new `Addon.on_bridged_message(event)` default-no-op hook, fired after every successful relay. Sentinel/Triage/Brief can observe cross-protocol traffic without SSE subscription or API polling.
- [x] Integration tests with mock send_fn + FakeRadio: bidirectional relay, echo-doesn't-loop, ai-gated chatter-vs-urgent, !urgent bypasses DisabledPolicy, DMs never cross.
- [x] README.md updated with "Dual-Radio Mode (LORACLE v2)" section (Phase 1) — bridge-config section covered in CONTEXT.md + FSD for now; user-facing doc expansion deferred to v2 release.
- [x] **Gate (software):** 300/300 tests pass.
- [ ] **Gate (hardware):** overnight run with rate limiter triggered; addon hook demonstrably fires on cross-protocol events. → pending user hardware verification.

---

## Risks

| Risk | Mitigation | Phase | Status |
|------|-----------|-------|--------|
| Message loops (A→B→A) | Dedup cache + bridge tag + max-hops counter | P2 | Planned |
| MeshCore lib bugs under load | Stress test at end of P1 before building on top | P1 | TODO |
| Channel semantics mismatch (MT has 8 channels, MC uses channels differently) | Explicit channel mapping table, not auto-1:1 | P2 | Planned |
| Addon breakage when code switches to `UnifiedMessage` | Compat shim synthesizes Meshtastic-shaped packet dict for legacy addon callbacks | P5 | Planned |
| Identity spoofing across bridge | Always prefix sender network in outbound; never forge native sender ID | P2 | Locked |
| `RadioManager.add_backend` connect-timeout (30s in MeshCoreBackend) blocks startup on missing hardware | Graceful fallback: log error, continue with partial backends | P1 | Planned |

---

## Open Questions

- [ ] Does the `meshcore` Python lib expose channel index cleanly on received messages? (verify during P1 smoke test)
- [ ] How do we render MeshCore public-key-based node IDs vs Meshtastic `!hex` IDs in UI? Short-hash + copy button? (decide before P3)
- [ ] When `AIGatedPolicy` drops a message, do we log it back to the origin channel for transparency, or just to the bridge log? UX call. (decide before P4)
- [ ] Does an addon need to know a message was relayed? Two hooks (`on_message`, `on_bridged_message`) or one with a flag? (decide during P5)

---

## Decisions Log

- **2026-04-17** — Chose per-channel allowlist (C) with AI-gated (B) as optional per-channel upgrade. Rationale: ships faster than AI-always, keeps the differentiator clean for Phase 4, avoids regressing dumb-bridge use cases users may want.
- **2026-04-17** — Branch, not fork. Rationale: ~90% of infrastructure already exists in this repo (`RadioManager`, `MeshCoreBackend`, protocol-aware DB schema). Forking would fight drift without buying isolation we need.
- **2026-04-17** — Rely on upstream `meshcore` Python library; don't port Akita's three serial protocol variants (`json_newline`, `raw_serial`, `companion_radio`). Rationale: less surface area to maintain; port only if hardware compatibility breaks in practice.
- **2026-04-17** — Out of scope for v2: MQTT bridging, federation, position/telemetry relay. Rationale: focus v2 on the defense-tech story (text relay + AI gating) and ship.

---

## Progress Log

- **2026-04-17** — FSD created at project root. Scope decisions locked per plan review with user. TodoWrite seeded with Phase 1 tasks. Phase 1 work starting now.
- **2026-04-17** — Phase 1 audit complete. Surprise: `RadioManager` already instantiated (line 200) but deliberately not connected — comment at lines 338-341 warns against fighting with the proven `_radio_connection_loop`. `--second-radio` flag exists but only saved to settings, not wired. Revised Phase 1 to use RadioManager only for the *secondary* MeshCore backend, leaving the working Meshtastic path alone. Identified 9 hardcoded `protocol="meshtastic"` locations and 3 hardcoded channel-id format strings. Tasks rewritten in Phase 1 to reflect this. Moving to implementation.
- **2026-04-17** — Phase 1 foundation landed (commit `5ae05aa`, pushed). Done: (a) protocol string parameterization throughout `standalone_bridge.py` — `_on_receive`, `_load_nodedb_positions`, and `_processing_loop` now use a local/unpacked `protocol` variable instead of hardcoded `"meshtastic"`; (b) `_request_queue` payload extended to 5-tuple `(protocol, sender, text, channel, is_dm)`; (c) new module-level helper `_parse_second_radio(spec)` supports `meshcore:serial:PATH`, `meshcore:tcp:HOST[:PORT]`, `meshcore:ble:[ADDR]`; (d) `StandaloneBridge.__init__` accepts `second_radio` and stores parsed config in `self._second_radio_config`; (e) `main()` passes `args.second_radio` through; (f) 9 new unit tests for `_parse_second_radio`. All 198 tests pass.
- **2026-04-17** — Phase 1 Chunk B landed (commit `e4280c7`, pushed). Done: (a) extracted `_persist_incoming(protocol, sender, text, channel, is_dm, rssi, snr, hops)` helper from `_on_receive` — used by both the Meshtastic pubsub path and the new MeshCore ingest loop; (b) new method `_connect_secondary_radio()` constructs `MeshCoreBackend` from `self._second_radio_config` and registers it with `_radio_manager` (runs in background thread; import-guarded against missing `meshcore` lib; 30s connect timeout does not block main thread); (c) new method `_secondary_radio_ingest_loop()` drains `_radio_manager.get_message()` queue, converts `Protocol` enum short form (`"mc"`) to DB name (`"meshcore"`) via `_PROTOCOL_SHORT_TO_DB`, rate-limits, persists via `_persist_incoming`, notifies addons, and enqueues onto `_request_queue` with protocol tag; (d) skips meshtastic messages in the ingest loop to avoid double-persist (pubsub path handles those); (e) `start()` spawns both threads only if `_second_radio_config` is set; (f) `_send_raw` / `_send_response` now accept `protocol="meshtastic"` parameter — when `"meshcore"`, routes via `_radio_manager.send(f"mc:{node_id}", …)` (Phase 1 meshcore sends use simple truncation, no paging; Phase 2 defers); (g) all 4 send call sites in `_processing_loop` pass `protocol=protocol` from the queue unpack. All 198 tests still pass.
- **2026-04-17** — Phase 1 finish (commit `c7c9efa`, pushed). Done: (a) UI protocol badge — `renderNodeList` carries `isMC` through item mapping, renders a small purple `mc` badge next to MeshCore contacts (no badge for Meshtastic = default, less clutter); (b) new CSS classes `.lo-ns-proto` / `.lo-ns-proto-mc` in `dashboard.py`; (c) README.md updated with dual-radio usage examples + Phase 1 limitations; (d) software-gate smoke test: `--help` parses cleanly, imports clean, 198/198 tests pass, new method signatures verified via inspect. Hardware gate (send DMs to both networks, confirm both land with correct badges) deferred to user — needs actual Meshtastic + MeshCore devices. **Phase 1 software-complete. Ready for Phase 2 (bridge core / cross-network relay).**
- **2026-04-17** — Phase 2 shipped (commit `faf54e7`, pushed). New `bridge/` package (identity + dedup + policy + config + relay), wired into `StandaloneBridge.__init__` as `self._relay` with helpers `_bridge_send`, `_bridge_sender_display`, `_on_bridge_relay`. `observe()` fires from both the Meshtastic pubsub path and the MeshCore ingest loop after persistence. 4 new API endpoints: GET/POST /api/bridge/config (hot-swap), GET /api/bridge/stats, GET /api/bridge/events. 54 new unit tests. 252/252 pass. No cross-network bridging happens until the user enables it via API — zero behaviour change by default.
- **2026-04-17** — Phase 3 shipped (commit `9582269`, pushed). New BRIDGE tab in dashboard: live ON/OFF badge, relayed/dropped/dedup counters, GLOBAL enable toggle, per-channel rules editor with add/delete/hot-apply, 200-row live flow log with mt↔mc arrows. 2.5s polling only while the tab is visible (poll timer auto-cancels on `setView` away). `_on_bridge_relay` now also emits `"bridge.relay"` SSE events via `_emit_sse` for future client subscription. CSS + HTML added inline to dashboard.py; no build step impact. 252/252 tests still pass.
- **2026-04-17** — Phase 4 shipped (commit `ff5067a`, pushed). `bridge/urgency.py` ships `HeuristicUrgencyClassifier` — keyword + structure heuristic covering distress (sos/mayday/urgent), casualty/medical (medic/medevac/injured/bleeding), fire/disaster (fire/flood/earthquake), threat (shot(s)/attack(s)/hostile), stuck-lost (stranded/trapped/crashed). Chatter allowlist (hi/roger/copy/thanks) always false. Weak heuristic for shouty traffic (multi-! + uppercase). `build_policy` replaces the Phase 2 pass-through AIGatedPolicy stub with the real classifier; per-config `urgent_keywords` extends the vocabulary at runtime. `!urgent` / `!priority` / `!sos` / `!mayday` prefixes (case-insensitive, optional `:` / `,` / `-` separator) strip-and-bypass policy entirely at the Relay level; DMs still never cross (bridging private conversation is a trust decision, not a priority decision); bang-word-alone is a no-op. 24 new tests (13 urgency + 11 force-relay). 276/276 pass. LLM-rewrite deferred to v2.1.
- **2026-04-17** — Phase 5 shipped (commit `9a1de79`, pushed). Hardening pass. (a) `bridge/rate_limit.py` RelayRateLimiter — thread-safe sliding window, 30/60s default, per-(source,dest,channel) bucket; `!urgent` bypasses; rejected events don't consume quota. (b) `db/bridge_events` SQLite table + BridgeEventStore with CHECK-constrained outcomes, 30-day retention pruned on startup, indexed query access; new `GET /api/bridge/history` endpoint. (c) `Addon.on_bridged_message(event)` default-no-op hook fires for every successful relay — lets Sentinel/Triage/Brief observe cross-protocol traffic without SSE/polling. (d) Integration test harness with FakeRadio + MockAddon exercises the full pipeline end-to-end (bidirectional relay, echo-doesn't-loop, ai-gated chatter-vs-urgent, !urgent-bypasses-disabled, DM-never-crosses). 24 new tests (10 rate-limit + 9 bridge_events + 5 integration). 300/300 pass. **All v2 Phase 1–5 software-complete.**
