# LORACLE Bridge v2 — Functional Specification Document (FSD)

**Status:** In Progress — Phase 1
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
- [ ] Extend `_request_queue` payload to carry `protocol` (e.g. tuple `(protocol, sender, text, channel, is_dm)` or a small dataclass)
- [ ] Parse `--second-radio PROTOCOL:TRANSPORT:PARAMS` string in `main()` (currently just saved to settings). On valid `meshcore:…`, construct `MeshCoreBackend` and register via `_radio_manager.add_backend()`
- [ ] Add new thread `_secondary_radio_ingest_loop` that pulls `UnifiedMessage` from `_radio_manager.get_message()` and enqueues onto `_request_queue` with `protocol="meshcore"`
- [ ] Parameterize `_on_receive`'s DB inserts to accept `protocol` (remove hardcoded strings at 786/794/800/803/810)
- [ ] Parameterize the channel-id format: `{protocol}:channel:{n}` (derived, not hardcoded) at 800, 1153, 1246
- [ ] Parameterize `_refresh_nodes` contact upsert at line 1069 (this path is Meshtastic-only by definition but should still pass `protocol="meshtastic"` explicitly via constant)
- [ ] Teach `_send_response` / `_send_raw` to route via `_radio_manager.send()` when node id has `mc:` prefix; fall through to `self.interface` for `mt:`/unprefixed (back-compat)
- [ ] UI: protocol badge (mt/mc) on each node in the node list — read from `contacts.protocol` column
- [ ] **Gate:** start bridge with `--serial /dev/ttyUSB0 --second-radio meshcore:serial:/dev/ttyUSB1`. Send a DM from the UI to a `mt:` and a `mc:` node — both land. Node list shows correct badges. No regressions on Meshtastic-only startup.

### Phase 2 — Bridge core
**Goal:** New `bridge/` module relays messages *between* backends. Loops prevented. Identity mapped.

- [ ] Create `meshtastic-bridge/bridge/` package
- [ ] `bridge/relay.py` — subscribes to `RadioManager` message queue, applies policy, re-injects via `manager.send()` to the other protocol
- [ ] `bridge/policy.py` — pluggable base class + implementations: `AlwaysRelay`, `ChannelAllowlist`, `AIGatedPolicy` (stub returning True for now, filled in Phase 4)
- [ ] `bridge/dedup.py` — cache keyed on `(sender, payload_hash, ts_window)`; prevents same message looping back
- [ ] Loop prevention: tag outbound relayed messages with `bridged_from=<backend_id>` and skip bridging them again
- [ ] `db/bridge_identity.py` — identity mapping: how "Alice on Meshtastic" is rendered on MeshCore. Default: `[mt-Alice] payload`
- [ ] Config: per-channel bridge rules in `config.yaml` (channel → allowed / denied / ai-gated)
- [ ] Config loader reads bridge rules and hands them to the `Policy`
- [ ] **Gate:** text sent on Meshtastic channel 0 appears on MeshCore (with sender prefix), no loops during 60s soak test, no duplicates.

### Phase 3 — UI surface
**Goal:** Operators can see and control the bridge from the dashboard.

- [ ] New "BRIDGE" tab in `dashboard.py`
- [ ] Live flow log (left→right / right→left, timestamps, sender, truncated payload)
- [ ] Per-channel bridge toggles (wired to config.yaml persistence)
- [ ] Dedup cache stats panel (hits, misses, drops)
- [ ] Config panel: policy picker per channel (Off / Always / AI-gated)
- [ ] SSE events: emit `bridge.relay` events so the tab updates live
- [ ] **Gate:** user enables bridging for channel 0 via UI, sees live flow log populate as messages cross.

### Phase 4 — AI-gated relay (the differentiator)
**Goal:** LLM decides what crosses. This is the defense-tech pitch.

- [ ] `AIGatedPolicy.should_relay()` calls `routing/classifier.py` for urgency classification
- [ ] Classifier prompt / few-shot for urgency detection (urgent / chatter / admin)
- [ ] Optional LLM rewrite mode: compress long messages for the low-bandwidth destination; translate call-signs if config says so
- [ ] `[AI]` annotation on rewritten messages (never forge originals)
- [ ] Bypass: `!urgent` prefix from user forces relay without AI gating
- [ ] **Gate:** demo — chatty Meshtastic channel, AI drops noise, relays urgent. Show logs of decisions.

### Phase 5 — Hardening
**Goal:** production-ready defaults.

- [ ] Per-direction rate limiting (port Akita's `rate_limiter.py` approach, adapted)
- [ ] SQLite `bridge_events` table — persistent audit log of every relay decision
- [ ] Addon API: expose `on_bridged_message()` hook so Sentinel/Triage/Brief can observe cross-protocol traffic
- [ ] Integration tests with mock `RadioBackend` instances for both sides
- [ ] `README.md` — new "Bridge configuration" section
- [ ] **Gate:** run bridge overnight with rate limiter triggered; addon hook demonstrably fires on cross-protocol events.

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
- **2026-04-17** — Phase 1 foundation landed (commit pending). Done: (a) protocol string parameterization throughout `standalone_bridge.py` — `_on_receive`, `_load_nodedb_positions`, and `_processing_loop` now use a local/unpacked `protocol` variable instead of hardcoded `"meshtastic"`; (b) `_request_queue` payload extended to 5-tuple `(protocol, sender, text, channel, is_dm)`; (c) new module-level helper `_parse_second_radio(spec)` supports `meshcore:serial:PATH`, `meshcore:tcp:HOST[:PORT]`, `meshcore:ble:[ADDR]`; (d) `StandaloneBridge.__init__` accepts `second_radio` and stores parsed config in `self._second_radio_config`; (e) `main()` passes `args.second_radio` through; (f) 9 new unit tests for `_parse_second_radio`. All 198 tests pass. No behavior change yet — the parsed config is not yet used to spawn a backend. Next: the ingest loop and MeshCore backend registration.
