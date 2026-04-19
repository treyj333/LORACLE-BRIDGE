# Project Context

This file tracks the history of changes, decisions, and current state of LORACLE BRIDGE.

---

## [2026-04-19 01:05] — Cross-protocol DM via `!dm <name-or-id> <text>`

- What changed:
  - **New `ContactsStore.find_by_name(name, protocol=None)`** — case-insensitive substring lookup across `custom_name`, `long_name`, `short_name` (channels excluded). Returns matches ranked so exact custom-name hits outrank substring matches on long-names; optional protocol filter lets the caller ask "only look on the MC side." Used by the new `!dm` command to resolve nicknames to concrete contact ids on the other mesh.
  - **New `!dm <target> <text>` command** in `StandaloneBridge._command_registry`. Target resolution: raw unified ids (`mc:abcdef` / `mt:!abc12345`) and bare Meshtastic ids (`!abc12345`) bypass the lookup; anything else is matched against the contacts DB across both protocols, preferring the *other* mesh when a name matches on both sides (the whole point of the command is cross-mesh reach). Ambiguous matches return a candidate list instead of guessing. Cross-protocol sends are prefixed `from <source> (<sender>): <text>` so the recipient knows who and where from, mirroring the existing public-channel relay.
  - **New `_infer_protocol(node_id)` helper** — tiny utility to detect a sender's protocol from a unified / raw node id without threading new args through every command handler's signature. `mc:`-prefixed ids are MeshCore; everything else is Meshtastic.
  - **Opt-in behaviour behind `cross_protocol_dm_enabled` SettingsStore flag** — off by default because bridging private messages across protocols is a trust decision, same reasoning as the existing `Relay.observe()` DM guard. Same-mesh `!dm` by nickname is always allowed (it's a convenience, not a trust decision; the sender could reach the target natively). Cross-mesh `!dm` returns a "disabled on this bridge" hint when the flag is off so remote users know why.
  - **New `GET/POST /api/cross-dm` endpoints** + a CONFIG-tab CROSS-PROTOCOL DM section with a single checkbox. `loadConfigData()` seeds the checkbox from the persisted flag; `cfgToggleCrossDm()` POSTs changes and fires a toast.
  - **Seven new unit tests** in `TestCrossProtocolDm` covering: missing-args usage hint, not-found, cross-protocol refused when flag off, cross-protocol forwarded when flag on (with sender-tag prefix), same-mesh DM always allowed (no tag), ambiguous name → candidate list returned, concrete unified id bypasses the lookup.
- Why:
  - The earlier phases delivered equal treatment for the *local* bridge operator — they could DM any peer, regardless of protocol, because the RadioManager was already protocol-agnostic. What was missing was cross-mesh reach *for remote users*: an MT node on the edge of the mesh had no way to DM an MC peer without also running this bridge themselves. Public channel 0 auto-relayed broadcasts, but `Relay.observe()` explicitly dropped DMs at the bridge layer (a privacy default). `!dm` is the opt-in escape hatch: operators who want their bridge to carry cross-protocol DMs can flip the CONFIG checkbox, and users on either mesh can then message each other by nickname.
- Impact on project goals:
  - Closes the last big "equal citizens" gap: the two user bases really can intermingle now, not just see each other's public traffic. The trust posture is preserved — off by default, explicit per-operator opt-in — so nothing about the default flow changes for anyone who doesn't want their bridge carrying private messages. `!dm` also works purely as a same-mesh convenience (nickname-to-id resolver) without needing the flag, which lowers the adoption cost: operators who just want the nickname ergonomic without the cross-mesh step can leave the flag off and still get value.
- Files modified:
  - `meshtastic-bridge/db/contacts.py` — `find_by_name(name, protocol=None)` with ranked substring match across the three name columns.
  - `meshtastic-bridge/standalone_bridge.py` — `_infer_protocol` helper, `_cmd_dm` handler, `_CROSS_DM_KEY` settings key, `!dm` registered in `_init_commands`.
  - `meshtastic-bridge/dashboard.py` — CROSS-PROTOCOL DM CONFIG section, `GET/POST /api/cross-dm` endpoints, `cfgToggleCrossDm()`, and `loadConfigData()` seed.
  - `meshtastic-bridge/tests/test_standalone_bridge.py` — new `TestCrossProtocolDm` class with seven cases.
- Tests: 315/315 pass.

---

## [2026-04-19 00:15] — BRIDGE tab: per-direction stats + coloured flow-log arrows

- What changed:
  - **`Relay` tracks per-direction counters.** `bridge/relay.py` gained `_relayed_by_direction` + `_dropped_by_direction` dicts keyed by `"meshtastic->meshcore"` / `"meshcore->meshtastic"`, bumped alongside the existing aggregate counters at every in-loop drop site (DM guard, policy reject, dedup, rate limit) and every successful relay. `stats()` surfaces them under a new `by_direction` block that always populates both canonical keys even at zero, so the UI never has to guess-and-default to "render the other side as empty."
  - **Two new unit tests.** `test_stats_by_direction_always_present` proves the block is symmetric from birth (both keys, both zeroed). `test_stats_by_direction_splits_relay_and_drop` uses a `ChannelAllowlist([("meshtastic", 0)])` policy to route one MT→MC message through and one MC→MT message into a policy drop, then asserts the per-direction buckets split correctly and the aggregate totals still add up.
  - **BRIDGE tab stats row redesigned.** `#bridge-stats` used to be a flat trio (`relayed / dropped / dedup`) that didn't distinguish direction. Now it's three bordered columns: MT → MC (teal), MC → MT (purple), DEDUP. Each direction column shows its own `relayed` and `dropped` counts. The legacy `#bridge-relayed` / `#bridge-dropped` elements are kept as hidden aggregates so any external integration or test that still reads the old ids keeps working.
  - **Flow-log arrows colour by source.** Each `.lo-bridge-flow-row .dir` cell now gets a `dir-mt` (teal) or `dir-mc` (purple) class based on `event.source`. Users can eyeball direction from the colour instead of parsing the `mt→mc` / `mc→mt` text — matches the scope selector and the canvas node colours.
  - **CSS additions.** `.lo-bridge-stat-col` (bordered flex column), `.lo-bridge-stat-dir` (upper-case direction label), `.dir.dir-mt` / `.dir.dir-mc` (protocol-coloured arrows in the flow log).
- Why:
  - Phase 4 of the equal-citizens work. The relay stats were technically symmetric (MT+MC traffic both counted in the same totals) but that framing hid which direction was actually doing the heavy lifting — made it impossible to tell at a glance whether an MT-heavy mesh was dominating or whether the bridge was balanced. Showing two mirrored columns gives MT and MC equal screen real estate in the stats row, which is exactly the visual grammar the rest of the app already uses (scope selector, badges, self-nodes).
- Impact on project goals:
  - The BRIDGE tab now mirrors the rest of the equal-citizens UI: every protocol-tagged surface shows MT and MC with matching weight and protocol-coded colour. Per-direction numbers also make it much easier to notice when one half of the bridge is broken (e.g. MT→MC relaying fine, MC→MT dropping everything) — previously that would be invisible behind the aggregate. No schema changes; the `by_direction` block is additive so existing consumers of `/api/bridge/stats` ignore it cleanly.
- Files modified:
  - `meshtastic-bridge/bridge/relay.py` — `Dict` import; two new instance dicts; `_bump_direction` helper; bumps added at DM-drop / policy-drop / dedup-drop / rate-limit-drop / relay-success; `stats()` includes the `by_direction` block with both canonical keys always populated.
  - `meshtastic-bridge/dashboard.py` — `#bridge-stats` rewritten into three bordered columns (MT→MC, MC→MT, dedup); legacy aggregate spans kept hidden; `bridgePollStats()` reads `d.by_direction` and fills the new DOM ids; `bridgeRenderFlow()` tags arrows with `dir-mt` / `dir-mc`; new CSS for the columns and flow-row colours.
  - `meshtastic-bridge/tests/test_bridge_relay.py` — two new test cases on `Relay.stats().by_direction`.
- Tests: 308/308 pass.

---

## [2026-04-18 01:55] — MT/MC equal-citizens pass (Phase 2b: MeshCore-first primary connect)

- What changed:
  - **Primary-connect modal respects the protocol dropdown.** `connectFromModal()` in the dashboard used to ignore the `connect-protocol` value and always POST to `/api/connection/switch` (the legacy Meshtastic-only endpoint), so if a user picked "MeshCore" in the dropdown the connect silently failed. It now reads the dropdown and, when the user picked `meshcore`, routes instead to `/api/backends/add` (the existing MC-add endpoint) with the transport + address fields translated to that endpoint's shape (`serial_port` / `tcp_host`+`tcp_port` / `ble_address`). `auto` and `meshtastic` keep the legacy path, so nothing about the default first-run flow changes.
  - **Success panel is protocol-aware.** The `connect-modal-success` panel used to hard-code "MESHTASTIC CONNECTED" in teal with "add a MeshCore radio" in the CTA. Now `_showPrimarySuccessPanel()` inspects `App.state.backends` to detect which protocol actually connected (falling back to the dropdown value if the backend list hasn't arrived yet), sets the heading + color + check-glyph accordingly, toggles an `is-mc` class on the panel so the CSS `::before` dot flips to the purple MeshCore diamond, and rewrites the CTA to pitch the *other* protocol as the second-radio add-on. Gave the static heading / paragraph / check-glyph dedicated ids (`connect-modal-success-title`, `connect-modal-success-desc`, `connect-modal-success-check`) so the JS has stable targets.
  - **Dropped the last MT-primary fallback.** `/api/radios` used to synthesise a fake `{"id": "mt-primary", "protocol": "mt", …}` entry when no backends were registered. That framed Meshtastic as the implicit "something is here" default and leaked into the self-node + scope logic. Removed; the endpoint now returns an empty list when nothing's connected and the frontend handles that cleanly.
- Why:
  - Completes the structural half of the equal-citizens work. With Phase 1 (scope selector) and Phase 3 (graceful feature fallback) already in place, the only remaining way MT was "privileged" was that you literally couldn't use the first-run flow to connect a MeshCore radio first — you had to skip step 1 and fall through to the secondary modal, which still framed MC as an add-on. Now someone running an MC-only rig can pick MeshCore from the protocol dropdown and connect in the same flow as a Meshtastic user, and the success screen greets them in the right colour/wording.
- Impact on project goals:
  - The user's original complaint ("it seems like Meshtastic is the primary program, and the MeshCore is just kind of an add-on") is now addressed end-to-end. The connect modal, the success panel, the scope selector, the node badges, the self-node labels, the feature endpoints, and the wizard copy all treat MT and MC as equals. Still not touched (deliberately): `/api/connection/switch` is still wired to the legacy Meshtastic-specific `switch_connection()` method — it's only used when the dropdown is `auto` or `meshtastic`, so there's no user-visible asymmetry, but the API naming is a leftover artefact from the single-protocol era and could be renamed in a later refactor.
- Files modified:
  - `meshtastic-bridge/dashboard.py` — `api_radios` fallback dropped; `connectFromModal` splits on `proto === 'meshcore'` and POSTs to `/api/backends/add` with translated fields; success-panel HTML has stable ids + `is-mc` toggle class; `_showPrimarySuccessPanel` reads connected-backend protocol and sets heading/color/CTA; matching `#connect-modal-success.is-mc h3::before` CSS.
- Tests: 306/306 pass.

---

## [2026-04-18 01:35] — MT/MC equal-citizens pass (Phase 2: wizard copy + fallback)

- What changed:
  - **Wizard step labels dropped protocol names.** `STEP 1 OF 2 — MESHTASTIC` → `STEP 1 OF 2 — PRIMARY RADIO`; `STEP 2 OF 2 — MESHCORE` → `STEP 2 OF 2 — SECOND RADIO (OPTIONAL)`. Primary-modal title `CONNECT YOUR MESHTASTIC` → `CONNECT YOUR FIRST RADIO`; add-radio modal title `ADD SECONDARY RADIO` → `ADD A SECOND RADIO`. Step descriptions rewritten to frame MT + MC as equal rather than "MT-required, MC-optional-addon."
  - **Skip buttons renamed.** `SKIP — NO MESHTASTIC` → `SKIP — MESHCORE ONLY`; `SKIP — NO MESHCORE` → `SKIP — SINGLE RADIO`. Same flow, honest framing (the `/api/connection/switch` endpoint is still MT-only underneath, so stepping past primary with `SKIP` really does mean "my first radio is MC").
  - **Success-modal paragraph resymmetrized.** Was: *"Your Meshtastic radio is up and running. Next, let's try a MeshCore radio — it's optional."* Now: *"Your first radio is up. Want to reach the other network too? Add a MeshCore radio — optional. With both connected, MT and MC peers can message each other through the auto-bridge."* Pitches the second radio as a cross-network bridge, not a bolted-on extra.
  - **Dropped hardcoded `mt-primary` fallback in `buildGraph`.** The placeholder self-node inserted when no backend info has arrived yet now honours `App.scope`: if the user has scoped to MC, the fallback gets `protocol: 'mc'` instead of unconditionally assuming MT. When scope is `all`, the fallback's `protocol` is left empty (previously `mt`), so nothing downstream assumes MT-by-default during the first poll tick.
- Why:
  - Phase 2 of the equal-citizens work: once the scope selector + graceful feature-parity infra (Phase 1 + 3) landed, the remaining asymmetry was all copy. The wizard was the most visible — it literally told users "MT is the main thing; MC is the optional add-on" with every label, step, and skip button. Doesn't change flow (MT is still the connect-modal target because `/api/connection/switch` is MT-only), but stops framing MT as the product and MC as the plugin. The `mt-primary` fallback was a second-order bug: even with the scope selector set to MC, a zero-backend first paint drew a "MY NODE" teal circle in the middle of the canvas.
- Impact on project goals:
  - Every piece of first-run copy now reads as "MT and MC are equals" instead of "MT first, maybe MC second." Combined with Phase 1 (scope selector), Phase 3 (graceful feature fallback), and Phase 2 (copy), the dashboard no longer frames either protocol as primary. Remaining known asymmetries: the primary-connect API is MT-only (a flow refactor, not copy), and `/api/connection/switch` route naming still reflects the MT-origin design. Both are non-blocking for the equal-citizens experience today.
- Files modified:
  - `meshtastic-bridge/dashboard.py` — wizard step labels (`connect-modal-wizard-step`, `ar-wizard-step`), titles + descriptions in `_applyWizardChromePrimary` / `_applyWizardChromeSecondary` and in the static HTML for step 2, skip-button labels, success-modal paragraph, `buildGraph` fallback protocol.
- Tests: 306/306 pass.

---

## [2026-04-18 01:10] — MT/MC equal-citizens pass (Phase 1 + 3)

- What changed:
  - **Global protocol scope selector** — new top-bar segmented control `[ ALL | MT | MC ]` right after the connection dots. Drives an `App.scope` state (persisted in `localStorage` as `loracle_scope`) and a new `nodeInScope(n)` helper. Filter is applied at render-time (not data-build-time, so positions are preserved) in seven places: `renderNodeList`, the canvas node-draw loop, the canvas link-draw loop, `onCanvasClick`, `_updateHoverCursor`, the mouse-magnetism closest-node scan, and `updateMapMarkers`. Active button colours match the protocol (teal MT, purple MC).
  - **Symmetric protocol badges** — MT nodes in the sidebar now get a teal `mt` badge alongside MC's purple `mc`. Previously only MC was badged, with a comment that said "Meshtastic is the default (no badge = less clutter)" — that exact framing was making MT feel primary. Added `.lo-ns-proto-mt` CSS so the two badges have matching geometry and contrast.
  - **Symmetric "MY NODE" label** — the self-node label in `buildGraph` used to special-case MC as `MY MC` while MT said `MY NODE` when alone. Now both read `MY NODE` in single-radio mode and `MY MT` / `MY MC` only when two radios are connected.
  - **New `FeatureNotSupported` exception** in `radio/backend.py` (subclass of `NotImplementedError`). Three optional methods added to the `RadioBackend` base with defaults that raise `FeatureNotSupported`: `send_traceroute`, `get_radio_config`, `set_radio_config`. `MeshtasticBackend` overrides all three (delegating to `sendTraceRoute` / `localConfig.lora` / `writeConfig` with the same logic that used to live in the dashboard handlers). `MeshCoreBackend` overrides only `get_radio_config` to return a read-only `{"read_only": true, "device": {…}}` view built from `send_device_query`; traceroute and writable config stay at the base-class default.
  - **RadioManager dispatchers** — added `send_traceroute(unified_node_id, hop_limit)` (dispatch by protocol prefix), `get_radio_config(backend_id=None)` (picks backend by id or falls back to primary; stamps `backend_id` + `protocol` onto the result), and `set_radio_config(config, backend_id=None)`. Also a new private `_pick_backend` helper that raises `ValueError` when the requested backend isn't connected.
  - **Dashboard endpoints rewritten** — `/api/traceroute`, `GET/POST /api/radio/config` now route through the RadioManager dispatchers instead of reaching into `_bridge.interface.*`. Config endpoints accept an optional `backend_id` (query param on GET, body field on POST). All three catch `FeatureNotSupported` and return HTTP 501 with `{"feature_not_supported": true, "error": …}` so `callApi` surfaces a clear toast.
  - **Config tab degrades gracefully** — new `_radioCfgReadOnly` flag + `cfg-radio-save` button id. When `cfgLoadRadio()` sees `read_only: true`, it disables the save button and inserts a dashed-border notice reading "This radio (MC) exposes a read-only config. LoRa tuning is Meshtastic-only." `cfgSaveRadio()` also short-circuits with an error toast if the user somehow tries to save anyway.
- Why:
  - User report: *"Right now, it seems like Meshtastic is the primary program, and the MeshCore is just kind of an add-on. But I would like for them to both be equal and have equal feature sets."* The diagnostic pass identified three rot vectors: (1) the unified node tree already worked, but there was no way to filter to just MT or just MC across the app; (2) the sidebar badged only MC, which framed MT as the default; (3) three server endpoints were hardwired to `_bridge.interface.*` and would silently fail (or worse, pretend to succeed) on MC. Phase 1 is the most visible fix (the selector), Phase 3 is the infra that prevents silent feature-divergence going forward.
- Impact on project goals:
  - The dashboard now treats MT and MC as equal citizens — every view has a protocol scope, every node has a badge, and every MT-only endpoint fails honestly on MC with a user-visible toast instead of an empty result. `FeatureNotSupported` is the mechanism for any future feature-parity gap: add the method on the base class, let MC raise by default, let the dashboard catch and surface it. Three remaining pieces for full equality: copy/wizard reframing (Phase 2 — "STEP 1 — MESHTASTIC" still reads as MT-first), the `mt-primary` hardcoded fallback in `buildGraph` ([dashboard.py:3130]), and bridge-visibility polish (Phase 4 — direction labels + per-protocol stat columns).
- Files modified:
  - `meshtastic-bridge/dashboard.py` — scope selector HTML/CSS + `App.scope` + `setScope` + `nodeInScope`; filter applied at 7 render sites; symmetric `lo-ns-proto-mt` badge + label; `FeatureNotSupported` import; three endpoint rewrites; `cfg-radio-save` id + `_radioCfgReadOnly` flag + read-only notice in `cfgLoadRadio`; read-only guard in `cfgSaveRadio`; symmetric `MY NODE` label.
  - `meshtastic-bridge/radio/backend.py` — new `FeatureNotSupported` exception + three optional methods with raising defaults.
  - `meshtastic-bridge/radio/manager.py` — imports `FeatureNotSupported`; new `send_traceroute` / `get_radio_config` / `set_radio_config` / `_pick_backend`.
  - `meshtastic-bridge/radio/meshtastic_backend.py` — concrete implementations of the three optional methods (same logic that used to live in the dashboard).
  - `meshtastic-bridge/radio/meshcore_backend.py` — read-only `get_radio_config` that wraps `get_self_info()` under a `read_only` flag.
  - `meshtastic-bridge/tests/test_radio_backends.py` — four new `TestRadioManager` cases covering the default-raises-FeatureNotSupported path, correct protocol dispatch, `backend_id` routing, and the read-only raise on set.
  - `README.md` — new "Protocol Scope Selector" sub-section under the dashboard Views table.
- Tests: 306/306 pass (all four new cases green).

---

## [2026-04-18 00:20] — Multiple float windows can stay open simultaneously (MT + MC side-by-side)

- What changed:
  - **Float-window behaviour is no longer single-window mutex.** `openFloatWindow` and `openSelfWindow` previously called `Object.keys(_openWindows).forEach(k => closeFloatWin(k))` right before opening the new panel, so clicking a second node always closed the first. That made it impossible to see an MT thread and an MC thread at the same time — the user had to pick one or the other. Both close-others calls removed; users now close panels explicitly via the `×` button.
  - **Cascading positions for new windows.** A 2nd+ simultaneously-open panel now appears offset (28×28 px per existing window) from the default top-right slot so panels don't fully overlap on open. When the cascade would run off the left edge, wraps with a small modulo so it stays on-screen.
  - **Click-to-front.** Introduced a `_bringWinToFront(win)` helper that bumps a monotonic `_winZ` and assigns it as `zIndex` on the panel. Called: (a) when a panel is created/opened, (b) when the user starts dragging it, and (c) on any `mousedown` anywhere in the panel body. So two overlapping MT + MC panels are both usable — clicking either one raises it above the other.
- Why:
  - User report: "I'm having a hard time switching between interacting with the meshcore node and the meshtastic node once I select meshcore it's like that's the only nodes I can use and no way to see both." Root cause was the close-others-on-open behaviour inherited from the original single-panel design; now that MT and MC threads share the same dashboard, that exclusivity felt like a mutex the user couldn't escape.
- Impact on project goals:
  - The dashboard's "MT + MC unified on the same tabs" story actually works end-to-end now — the user can keep an MT peer's panel open, click an MC peer, and both stay visible. Pure client-side change; no backend or API change, no test surface touched.
- Files modified:
  - `meshtastic-bridge/dashboard.py` — removed the close-others loops in `openFloatWindow` + `openSelfWindow`, added cascading `existing * 28` offset for second-and-later panels, introduced `_bringWinToFront` + `_winZ` with hooks in panel-open / drag-start / panel-mousedown.
- Tests: 302/302 pass.

---

## [2026-04-18 00:05] — MC nodes now visible on the mesh canvas

- What changed:
  - **`_secondary_radio_ingest_loop` now uses the unified node id** (`mc:<native>`) throughout instead of the bare native id. Every downstream surface that feeds the dashboard or the DB — `_known_nodes`, `_persist_incoming` (contact_id), `_node_last_active` (rate-limit key), `self._relay.observe(...)`, `addon.on_message(...)`, `self._request_queue.put(...)` — now receives the prefixed form. Before: MC peers landed in `_known_nodes` as `abcdef012345`; the dashboard's `isMC = nid.indexOf('mc:') === 0` check then classified them as Meshtastic and they rendered as teal circles instead of purple diamonds (or collided with the MT self-exclusion logic).
  - **`_send_raw` / `_send_response` are now idempotent to the `mc:` prefix** — they normalise a `routing_id = node_id if node_id.startswith("mc:") else f"mc:{node_id}"` before passing to `_radio_manager.send(...)`. This keeps the send path safe now that the ingest loop hands unified ids through the request queue.
  - **`MeshCoreBackend.get_node_positions()` + `get_node_meta()`** implemented — previously the base-class default returned `{}` so the periodic `_node_sync_loop` pulled nothing from MC. Positions are built from each contact's `adv_lat` / `adv_lon` (skipping 0,0 and None), with a wall-clock `last_update` so the UI's freshness heuristic treats them as just-heard. Meta carries `short_name` + `long_name` so the dashboard labels match the user's configured device names.
  - **`_node_sync_loop` extended** — it now also merges `get_all_nodes().keys()` into `_known_nodes`, not just positions and meta. Advertised-but-silent MC peers now show on the dashboard without needing to send any traffic first. Refactored the body into a reusable `_sync_nodes_from_backends()` method.
  - **Faster first-sync**: the periodic loop's initial wait is now 3 seconds instead of the full 30-second refresh interval, and `_spawn_secondary_radio` spawns a 2.5-second-delayed one-shot sync after a successful `add_backend`. Net effect: when a user adds a MeshCore radio through the dashboard, advertised MC contacts appear on the canvas within ~3 seconds instead of up to 30.
- Why:
  - User report: "I don't think the meshcore nodes are displaying on the node map when I connect it?" Confirmed: two separate bugs were at play. First, the ingest loop stored raw-native ids (`abcdef012345`) in `_known_nodes`, so the dashboard's prefix-based classifier treated MC peers as if they were Meshtastic — wrong shape, wrong colour, and they conflicted with the MT self-node exclusion logic. Second, `MeshCoreBackend.get_node_positions()` / `get_node_meta()` were never overridden, so the periodic sync pulled nothing from MC and peers that weren't actively transmitting never showed up at all.
- Impact on project goals:
  - MC peers finally render with the right shape + colour on the canvas, in the node-list sidebar, and in the map view. Click-to-DM works because the in-memory `_known_nodes` id matches the DB `contact_id` matches the dashboard's `mc:`-prefixed unified id. Cross-protocol relay continues to work because the relay engine always took a sender string and didn't care about its format — the prefix is just tag content. No schema changes, no new dependencies.
- Files modified:
  - `meshtastic-bridge/standalone_bridge.py` — `_secondary_radio_ingest_loop` uses `msg.node.id` for every non-send-path sink; `_send_raw` + `_send_response` normalise `mc:` prefix idempotently; `_node_sync_loop` split into `_sync_nodes_from_backends()` + wrapper; `_spawn_secondary_radio` fires a 2.5s-delayed one-shot sync after `add_backend`
  - `meshtastic-bridge/radio/meshcore_backend.py` — `get_node_positions()` and `get_node_meta()` implemented
- Tests: 302/302 pass (behaviour changes live downstream of tested surfaces; relay/dedup/identity/integration suites all validate against the prefix-agnostic sender string).

---

## [2026-04-17 23:40] — Connect-modal dot alignment + per-success-panel dot colors

- What changed:
  - `.lo-connect-box h3::before` moved from the Unicode `◉` (U+25C9 + trailing space) glyph to a CSS-drawn circle — 9×9px, `border-radius:50%`, `background: var(--lo-accent)`, `vertical-align: middle`, `margin-right: 10px`, with a `top:-1px` nudge so it optically centers against the uppercase cap-height instead of sitting at baseline. Kept inline (not flex) so `text-align: center` on the success-panel parents still applies to the whole `dot + heading` row.
  - New success-panel overrides: `#connect-modal-success h3::before { background: var(--lo-accent-2); }` (teal dot next to teal "MESHTASTIC CONNECTED") and `#ar-success h3::before { background:#9b59b6; border-radius:0; transform: rotate(45deg) translateY(-1px); }` (purple diamond next to purple "MESHCORE CONNECTED") — matches the big glyphs above each success message.
  - Two other inline-text glyph spots replaced with CSS shapes for the same reason: (a) the "◆ MESHCORE CONNECTED" line in the add-radio modal's manage panel (`#ar-active-row`) now uses a flex-aligned purple diamond `<span>`, and (b) the self-node float window (`loadSelfData`) was assembling `"<span>◆</span> MESHCORE"` / `"<span>●</span> MESHTASTIC"` with bare inline Unicode — now emits a proper CSS shape + `display:flex; align-items:center; gap:8px` so the icon is centered with the label.
- Why:
  - User feedback: "where it says Connect your meshtastic or MESHTASTIC CONNECTED etc the dot next to it is too close and not centered can we make that look a little cleaner and anywhere else something is like this?" Unicode glyphs are subject to font metrics we don't control and can't be reliably centered against text; switching to CSS shapes + flex/vertical-align gives pixel-level control and avoids per-font drift. Colour-matching the success-panel dot to the success-panel heading also removes the old orange-on-teal/orange-on-purple visual mismatch.
- Impact on project goals:
  - Pure UI polish — no behavior change, no test surface touched. The wizard + post-wizard "+ RADIO" modal + self-node detail panel all read as more intentional now (dot color = protocol color, dot centered cleanly with the uppercase label). Zero risk to relay/backend code.
- Files modified:
  - `meshtastic-bridge/dashboard.py` — the two `.lo-connect-box h3::before` rules + per-panel overrides, the `#ar-active-row` diamond markup, the `loadSelfData` protocol-badge emitter.
- Tests: 302/302 pass.

---

## [2026-04-17 23:25] — Wizard fix: success panel now stays up after CONNECT

- What changed:
  - `connectFromModal` and `connectModalPickDevice` no longer set `_userAckedModal = true` the moment the user clicks CONNECT / picks a BLE device. That pre-ack was short-circuiting the wizard success panel: when the next poll returned `connected=true`, `checkConnectionForModal` hit the `_userAckedModal` branch and called `hideConnectModal()` immediately, so the user would see the primary connect modal silently disappear the instant the radio came up — with no visible success message and no automatic advance to step 2. The fix defers the ack: in wizard mode it happens when the user clicks NEXT (or DONE) on the success panel; in non-wizard mode it happens when the 3s auto-dismiss timer fires (which was already wired to flip the flag). Net effect: the wizard's "MESHTASTIC CONNECTED" panel stays visibly up while node population starts in the background, and the non-wizard reconnect modal now shows its "RADIO CONNECTED" confirmation for the intended 3 seconds instead of closing instantly. Also added a short informational comment at the top of `connectFromModal` so the next maintainer doesn't re-introduce the bug.
- Why:
  - User feedback: "when it populates nodes after step 1 connecting to meshtastic it turns off the hub and i have to go find step 2 can we make it populate the nodes but keep the pop up up for step 2." The "hub turning off" was the primary modal silently closing as soon as the radio handshake finished, leaving the user on the bare canvas with nodes streaming in and no path to the MeshCore prompt except the `+ RADIO` button in the top bar — which defeats the purpose of a guided wizard.
- Impact on project goals:
  - Restores the intended paced wizard UX — the success panel is unmissable, and the user explicitly decides when to advance to step 2. Node population / map rendering proceeds normally underneath; the modal is just an overlay that no longer gets torn down before the user sees it. Zero backend change, purely a client-side flag-ordering fix.
- Files modified:
  - `meshtastic-bridge/dashboard.py` — removed premature `_userAckedModal = true` assignment in `connectFromModal` and `connectModalPickDevice`; added a status-text string that's wizard-aware ("this screen will update when the radio comes up" vs the old "modal will close when connected").
- Tests: 302/302 pass (no behavior change on tested surfaces).

---

## [2026-04-17 23:05] — Onboarding disclaimer: use native app first

- What changed:
  - **Onboarding tour** — new `data-step="1"` slide inserted right after WELCOME, before ORGANIC MESH TOPOLOGY. Styled with an amber warning triangle (SVG "!"), amber-colored `BEFORE YOU CONNECT` heading, and body copy: "Do initial radio setup in the native app first. Install the official Meshtastic app (or MeshCore app) and pair your radio over BLE/USB. Set your region / frequency, pick a short name, configure any channel keys, and confirm the radio is talking on the mesh. LORACLE Bridge reads and drives the radio — it isn't a first-time-setup tool, and a radio without a region set will look connected but receive nothing." Existing steps 2–8 had their `data-step` attrs bumped by 1 for clarity (DOM-order still drives the active-step selection so the renumber is cosmetic but matches the visible progress dot). `_obTotal` bumped 8 → 9 so the progress indicator and NEXT-vs-DONE button transition land on the correct slide.
  - **README** — prominent blockquote added to the top of the Quick Start section, with the same disclaimer: pair in the native app, set region/frequency, confirm mesh connectivity, THEN plug into LORACLE. Links out to [meshtastic.org/docs/software/](https://meshtastic.org/docs/software/) and [meshcore.co.uk](https://meshcore.co.uk) for the official apps.
- Why:
  - User feedback: "when onboarding make sure to put a disclaimer in to tell the user to use the native apps to do initial setup and set frequency and settings before using loracle bridge or their nodes may not work with the software." A factory-new radio with no region set will look connected via the Meshtastic Python API (serial/BLE handshake succeeds) but won't actually transmit or receive anything on-air because the radio firmware refuses to emit RF without a region. This is a classic early-user trap that costs support time and makes LORACLE look broken; surfacing the expectation up-front (both in the onboarding tour and the README Quick Start) cuts that off.
- Impact on project goals:
  - First-run UX is now explicit about the hard dependency on the native app: a user who opens the dashboard and takes the default onboarding tour sees the disclaimer as the second slide, and a user who reads the README before running anything sees it as the first thing under Quick Start. Zero code-path change — pure content + one JS constant bump. All behaviours (bridge, wizard, tests) unaffected.
- Files modified:
  - `meshtastic-bridge/dashboard.py` — onboarding: new step 1 (disclaimer), `_obTotal` → 9, step-2-through-8 `data-step` attrs bumped
  - `README.md` — Quick Start preamble blockquote with the disclaimer + links to the official apps
- Tests: 302/302 pass (no behavior change).

---

## [2026-04-17 22:45] — Paced wizard success screens + serial-port scanner

- What changed:
  - **Paced wizard success panels** — the step-1 modal no longer auto-dismisses 1.5s after a successful primary connect. Instead, the form rows are swapped out for a confirmation panel with a large teal `✓` glyph, a `MESHTASTIC CONNECTED` headline, a short description that explicitly calls out MeshCore as optional ("`…it's optional. Skip if you don't have one.`"), a small detail line showing the transport + address that was connected, and two clear buttons: `DONE — JUST MESHTASTIC` (closes the wizard) and `NEXT — ADD MESHCORE →` (advances to step 2). Step-2 gets an equivalent purple-diamond MESHCORE CONNECTED panel after a successful MC connect, with a single `DONE →` button and a plain-English confirmation that public-channel relay is live — no more 900ms-auto-close. Non-wizard "+ RADIO" flows keep the old brief-then-close UX so adding a radio after setup isn't slower than before. Panels toggle form/success state via new `#connect-modal-form` / `#connect-modal-success` and `#ar-form` / `#ar-success` wrappers; four new JS helpers (`_showPrimarySuccessPanel`, `_resetPrimaryPanels`, `_showSecondarySuccessPanel`, `_resetSecondaryPanels`) + three wizard-button handlers (`wizardAdvanceFromPrimarySuccess`, `wizardPrimaryDone`, `wizardFinishFromSecondarySuccess`).
  - **Serial-port scanner** — new `GET /api/serial/scan` endpoint wraps `pyserial.tools.list_ports` (already a transitive meshtastic-python dep, zero new dependencies). Returns each port's `device`, `description`, `manufacturer`, `vid`, `pid`, and a heuristic `likely_radio` flag. The heuristic matches CP210x / CH340 / FT232 drivers, Silicon Labs / WCH manufacturer names, and a bunch of known-device paths (`/dev/cu.usbserial`, `/dev/ttyUSB*`, `/dev/ttyACM*`, `/dev/cu.SLAB`). Dashboard: both connect modals (primary and secondary) grew a `SCAN PORTS` button next to their device/address input; clicking it populates a click-to-select list showing the port path, description, manufacturer, and VID:PID, with radio-shaped ports sorted to the top and marked with a green `●`. Shared JS (`_fetchSerialPorts`, `_renderSerialPorts`) keeps the two modal implementations in sync. SCAN PORTS only renders when the transport dropdown is set to `serial` — switching to TCP / BLE hides the button and its port list automatically.
- Why:
  - User feedback after the wizard shipped: "ok once it successfully connects to meshtastic it automatically closes but i want it to move on to the next screen and let them know meshtastic connected and then asks to connect a meshcore radio but it is optional and vice versa." The old 1500ms auto-dismiss swapped the title to "MESHTASTIC CONNECTED" for a blink, but a new user would miss that and just see the next modal appear out of nowhere. The paced success panel makes the success message unmissable and the next action explicit.
  - Also: "for serial connection can you make a scan button to select from the systems comports." Typing `/dev/cu.usbserial-XYZ` by hand is error-prone and varies by OS; a SCAN PORTS button that surfaces the actual available ports (with radio-shape hints) closes that UX gap.
- Impact on project goals:
  - The first-run experience is now genuinely self-guided: the user sees explicit success for each radio, is told MeshCore is optional in plain English, and picks serial ports from a list rather than typing Unix device paths. Zero backend-protocol change — all client-side plus the one read-only pyserial wrapper endpoint. The wizard's paced flow also makes the demo story cleaner (no awkward "the modal flashed a thing and disappeared, what did it say?" during live demos).
- Files modified:
  - `meshtastic-bridge/dashboard.py` — new `/api/serial/scan` endpoint + `_looks_like_radio_port` heuristic, primary connect modal wrapped in form/success panels + serial SCAN PORTS UI, secondary (add-radio) modal wrapped + serial SCAN PORTS UI, JS `_fetchSerialPorts` / `_renderSerialPorts` / `connectModalSerialScan` / `addRadioSerialScan`, success-panel show/reset helpers, `checkConnectionForModal` replaced the wizard-mode auto-timer with `_showPrimarySuccessPanel`, `submitAddRadio` shows the success panel in wizard mode instead of auto-closing
  - `README.md` — wizard + serial scanner documented in the Dual-Radio section
- Tests: 302/302 pass (unchanged — UI work only).

---

## [2026-04-17 22:15] — Two-step connect wizard, one-click bridge toggle, auto-seed at startup

- What changed:
  - **First-run connect wizard** — on very first dashboard load (detected via `localStorage.loracle-setup-complete` being absent), the connect flow is now a 2-step chain: STEP 1/2 asks for the Meshtastic radio (primary modal), STEP 2/2 asks for the MeshCore radio (add-secondary-radio modal). Either step can be skipped. New buttons: `SKIP — NO MESHTASTIC` (step 1) and `SKIP — NO MESHCORE` (step 2); CANCEL becomes BACK on step 2 for mid-wizard nav. On successful primary connect OR skip, the chain auto-advances to step 2; on skip/cancel of step 2 or successful MC connect, `wizardComplete()` sets the localStorage key so subsequent sessions skip the wizard. Implemented as three small JS additions (`_wizardActive`, `_applyWizardChromePrimary/Secondary`, `wizardSkipPrimary/Secondary/Complete`) plus hooks in `showConnectModal`, `dismissConnectModal`, `showAddRadioModal`, `hideAddRadioModal`, `submitAddRadio`, and `checkConnectionForModal`. Users who already had LORACLE running before this change see the wizard once, since the localStorage key won't be set yet.
  - **BRIDGE tab simplified** — the tab previously led with `GLOBAL` master switch + `PER-CHANNEL RULES` editor, both of which are more than the 95% case needs. New top section `AUTO-BRIDGE PUBLIC CHANNEL` has a single prominent toggle: "Auto-relay public channel 0 between Meshtastic and MeshCore." Flipping it seeds/unseeds the default `(mt, 0, always)` + `(mc, 0, always)` rules via the new `POST /api/bridge/public-channel` endpoint and shows live status ("Public channel is bridging both ways." / "Off."). The old master-enable checkbox + per-channel rules editor moved into a collapsed `ADVANCED RULES (most users don't need this)` disclosure. `bridgeReloadConfig` now also syncs the simple toggle's checked state (on iff enabled AND both default rules are present), so advanced edits and simple-toggle state stay consistent.
  - **New `/api/bridge/public-channel` endpoint** — `POST {"enabled": bool}` delegates to `_bridge._seed_default_bridge_rules(force=True)` / `_bridge._unseed_default_bridge_rules()`. Keeps all seed/unseed logic server-side so the JS doesn't have to read-modify-write the full config.
  - **`_seed_default_bridge_rules(force: bool)` refactored** — previously forced the bridge on unconditionally, which could resurface a config the user had deliberately disabled. Now has two modes: `force=False` (FIRST-TIME SEED) — refuses to do anything if any rules are already configured, so user edits survive restarts; `force=True` — the old behavior, used by explicit user actions (add-radio "seed_bridge" checkbox, BRIDGE-tab simple toggle). Existing callers updated.
  - **New `_unseed_default_bridge_rules()` helper** — inverse of the seed call for the BRIDGE-tab toggle. Removes only the default `(mt|mc, ch=0, mode=always)` rules — custom rules are preserved. If removing defaults leaves the rules list empty, the global `enabled` flag also flips off.
  - **Auto-seed on every startup secondary-radio connect** — `_spawn_secondary_radio` now calls `_seed_default_bridge_rules(force=False)` after successful `add_backend`. Effect: a user who started LORACLE with `--second-radio ...` (or a persisted spec restored from settings) gets bidirectional public-channel relay out of the box on first run, without clobbering any config they've saved on subsequent runs. Closes the last gap where the CLI / auto-restore path behaved differently from the dashboard add path.
- Why:
  - User feedback: "when it asks to connect a radio at the beginning ask to connect a meshtastic radio then a mesh core radio (its fine if they have one and not the other but i want the option)" — the single primary-connect modal didn't surface the MC option, and the `+ RADIO` button was only discoverable after page load. The wizard answers that directly.
  - "the cross protocol bridge options are confusing can we have it auto set those up or does it need user input?" — the per-channel rules UI is faithful to the v2 FSD's design but hides the simple common case ("just bridge public channel both ways") behind multiple dropdowns. Elevating the common case to a one-click toggle makes the UI match the user's mental model — AND auto-seeding on every startup radio-attach means the user doesn't have to think about the bridge at all unless they want to customise.
- Impact on project goals:
  - The dashboard's first-touch UX now asks for BOTH radios explicitly (no mental-model gap about "wait, where do I add the MeshCore one?"), gives sensible defaults that match the defense-tech demo story ("public channel bridges both ways with a human-readable source tag"), and keeps the advanced controls available for power users. Zero backend protocol change — everything is client-side UI plus one new endpoint that's a thin wrapper over existing seed/unseed helpers. Wizard is gated on localStorage so prior users see it once; no state migration required.
- Files modified:
  - `meshtastic-bridge/dashboard.py` — new `POST /api/bridge/public-channel` endpoint, BRIDGE-tab HTML rewritten (simple toggle + advanced details), `bridgeSimpleToggle` + `bridgeSyncSimpleToggle` JS, wizard state + `_applyWizardChrome*` + `wizardSkip*` + `wizardComplete` JS, `showConnectModal` / `dismissConnectModal` / `showAddRadioModal` / `hideAddRadioModal` / `submitAddRadio` / `checkConnectionForModal` updated to run the chain, step-indicator + skip button added to both modals' HTML
  - `meshtastic-bridge/standalone_bridge.py` — `_seed_default_bridge_rules(force)` parameterized, new `_unseed_default_bridge_rules()` helper, `_spawn_secondary_radio` calls the first-time seed after `add_backend`, `add_secondary_radio` uses `force=True` when user opts in via the seed-bridge checkbox
  - `README.md` — wizard + simple-toggle documented in the Dual-Radio section
- Tests: 302/302 pass (no behavior change in the tested surfaces — bridge engine, identity, dedup, rate-limit, integration suite all unaffected).

---

## [2026-04-17 21:40] — Dashboard add-secondary-radio UI, unified MT/MC send path, natural-language bridge prefix, default public-channel relay

- What changed:
  - **Secondary-radio UI in the dashboard** — the `--second-radio` CLI flag is no longer the only way to attach a MeshCore radio. A new `+ RADIO` button in the top bar (next to the MT/MC status dots) opens an `ADD SECONDARY RADIO` modal with transport picker (Serial / TCP / BLE), address/host/port/BLE-addr inputs, and an `Auto-relay public channel 0 both directions` checkbox. Clicking CONNECT posts to the new `/api/backends/add` endpoint which constructs a `MeshCoreBackend`, calls `RadioManager.add_backend()`, and blocks until the backend is live or errors out. The modal turns into a manage view once a MC backend is attached (shows transport + self node id, exposes a DISCONNECT button that hits `/api/backends/remove`). The top-bar button morphs: `+ RADIO` when none attached → `◆ MC` (purple, on-state) when active. Kept behind the existing `lo-connect-modal` styling so it matches the primary-radio modal visually.
  - **New runtime API** — `POST /api/backends/add` takes `{transport, serial_port|tcp_host/tcp_port|ble_address, seed_bridge}`. Returns the new backend's info dict on success, 400 on bad transport, 501 if the `meshcore` library isn't installed, 500 on connect failure. `POST /api/backends/remove` (optional `backend_id`, defaults to first mc-backend) disconnects and clears the persisted spec.
  - **`StandaloneBridge.add_secondary_radio(...)` / `remove_secondary_radio(...)` public helpers** — handle the connect-then-persist-then-seed orchestration so the Flask handler stays thin. `_connect_secondary_radio` was refactored to delegate to a new `_spawn_secondary_radio(cfg)` that raises (rather than logs-and-returns) so the runtime add path can surface errors to the user. `_seed_default_bridge_rules()` writes `(meshtastic, 0, always)` + `(meshcore, 0, always)` rules and flips `enabled=true` if it wasn't already — only when those rules aren't already present, so manual policy edits survive.
  - **Persisted second-radio spec** — `load_settings()` already had `second_radio` in defaults but it was never written. `add_secondary_radio` now serializes the config back to a `meshcore:serial:/dev/…` / `meshcore:tcp:host:port` / `meshcore:ble:addr` string via new `_encode_second_radio_spec()` helper and saves it. `main()` reads the persisted spec and falls back to it when no `--second-radio` CLI flag is set, logging `Restoring persisted second-radio: ...` so the provenance is obvious in logs. Remove clears the persisted value.
  - **Sender prefix is now natural language** — `[mt-Alice] text` / `[mc-Alice] text` became `from meshtastic (Alice): text` / `from meshcore (Alice): text`. `bridge/identity.py` regex + `format_bridged()` + tests (`test_bridge_identity.py`, `test_bridge_relay.py`, `test_bridge_integration.py`, `test_bridge_events_store.py`) all updated in one pass. `addons/base.py` docstring example updated. The `_PROTOCOL_LONG` map carries `mt`/`mc` aliases so callers using the short code still resolve correctly. Cost: ~12–14 extra bytes per relayed message; still well under the 233-byte LoRa budget. Benefit: a recipient's stock Meshtastic app literally shows "from meshcore (Alice): help" — no mental decoding of bracketed short codes.
  - **Unified send path** — `POST /api/threads/<id>/send` previously hardcoded `_bridge.interface.sendText()` (meshtastic-only), so DMing a MC peer from the dashboard silently sent nowhere and the contact was auto-created with the wrong protocol. Now: infers protocol from the `mc:` / `mt:` id prefix at auto-create time; routes via `_bridge._radio_manager.send(unified_id, text, channel, is_dm)` whenever a matching connected backend exists; falls back to the legacy Meshtastic interface only when RadioManager can't route. MC threads without a connected MC backend now return a clear `MeshCore radio not connected — add a secondary radio first` error instead of silently 0-byte-sending.
  - **Ingest loop always spawned** — previously `_secondary_radio_ingest_loop` only started if `_second_radio_config` was set at init time. Now it always runs as a daemon thread (no-op on empty queue) so a runtime-added MC radio is fully picked up without a bridge restart.
  - **Default public-channel relay** — when the user adds a MC radio via the dashboard with the `seed_bridge` checkbox on (default), the bridge config gains `(mt, ch 0, always)` + `(mc, ch 0, always)` rules and global `enabled=true` is set. This fulfils the user's ask: "when I get a meshcore message on public channels my meshtastic radio retransmits the message over meshtastic and says from meshcore, and vice versa." Works out of the box after one modal submission.
- Why:
  - User reported they couldn't figure out how to connect both radios (the CLI-only `--second-radio` flag isn't discoverable from the dashboard). Also asked for the two networks to coexist on the same tabs differentiated by shape/color only — the canvas was already unified after the previous commit, but the send path was still meshtastic-only, so DMs to MC peers silently failed and the contact-auto-create path tagged them with the wrong protocol. Finally, they wanted automatic public-channel relay with a human-readable "from meshcore / from meshtastic" tag that would make sense to someone reading on a stock Meshtastic phone app.
- Impact on project goals:
  - Removes the last hardware-config step that required shell access. A fresh user can now plug both radios in, open the dashboard, click `+ RADIO`, and be bridging in one modal submission. The "defense-tech portfolio demo" story gets much shorter: the user shows two phones on two different mesh protocols, clicks one button in the dashboard, and traffic on public channel starts crossing — tagged so the audience can see which side each message came from. The wire format is no longer a codegolf-looking `[mc-xx]` token but a sentence anyone can read. No new security or storage surface — the runtime endpoints reuse the existing `SettingsStore` + `RadioManager` plumbing from v2 Phases 1–5.
- Files modified:
  - `meshtastic-bridge/bridge/identity.py` — new prefix format + regex
  - `meshtastic-bridge/tests/test_bridge_identity.py` — rewritten for new format with added parens-required + unknown-proto guards (+1 test = 9 total)
  - `meshtastic-bridge/tests/test_bridge_relay.py` / `test_bridge_integration.py` / `test_bridge_events_store.py` — prefix assertions updated
  - `meshtastic-bridge/addons/base.py` — `on_bridged_message` docstring example updated
  - `meshtastic-bridge/standalone_bridge.py` — `add_secondary_radio` / `remove_secondary_radio` / `_spawn_secondary_radio` / `_seed_default_bridge_rules` / `_encode_second_radio_spec` public methods, ingest loop always spawned, `main()` falls back to persisted `second_radio` spec when no CLI flag
  - `meshtastic-bridge/dashboard.py` — `/api/backends/add` + `/api/backends/remove` endpoints, top-bar `+ RADIO` button + CSS, `ADD SECONDARY RADIO` modal (HTML), `showAddRadioModal` / `submitAddRadio` / `removeSecondaryRadio` / `arTransportChanged` / `refreshAddRadioModal` / `arSetStatus` JS, `poll()` paints the top-bar button state, `/api/threads/<id>/send` rewritten to route via RadioManager with legacy fallback and correct inferred-protocol contact creation
  - `README.md` — Dual-Radio Mode section now documents the dashboard modal + describes the natural-language prefix
- Tests: 302/302 pass (300 previous + 1 parens-required guard + 1 unknown-proto guard in test_bridge_identity).

---

## [2026-04-17 20:45] — Dashboard: dual self-nodes, protocol shape/color, self-click telemetry, dual top-bar status

- What changed:
  - **Top bar — independent per-backend status**: the single `hdr-dot` / `hdr-conn-label` that previously collapsed all backends into one "CONNECTED/DISCONNECTED" state is replaced with two independent indicators — `hdr-mt-dot` + `hdr-mt-label` (teal circle, shows `MT ON/OFF/--`) and `hdr-mc-dot` + `hdr-mc-label` (purple diamond, shows `MC ON/OFF/--`). `poll()` drives both from `state.backends[]` by matching `b.protocol ∈ {mt,meshtastic}` vs `{mc,meshcore}`. Legacy single-flag `connected` derived as `mt.connected || mc.connected || state.connected` so the modal / disconnect toast path is unchanged. New CSS: `.lo-bar .lo-conn-row` flex container, `.lo-bar .lo-dot.mc` (square + `transform: rotate(45deg)`), `flex-shrink: 0` on dots + rows so the header doesn't squish them.
  - **Dual self-nodes on the canvas**: `buildGraph()` now creates one self-node per connected backend instead of the single hardcoded `__self__`. Keys are `__self_<backend_id>__` with back-compat alias `nodeMap['__self__']` pointing at the primary. Two backends → two offset dots at `cx±18, cy` (MT on left, MC on right); single-backend rig still centered. `selfIds` set excludes every backend's real node id from the peer iteration so self-nodes don't double-render. New `isSelfId(id)` helper used by the traffic-pulse walker (`while (current && !isSelfId(current) && ...)`) so pulses terminate on any self-node regardless of which prefix key it has.
  - **Protocol shape differentiation on the canvas**: MeshCore nodes render as diamonds via a new `drawDiamond(ctx, cx, cy, r)` helper (self + peers). Half-diagonal is `r * 1.15` so the diamond reads as the same visual weight as a circle of radius `r`. Meshtastic stays as circles. Colors unchanged from existing logic — `nodeFillColor(node, accent2, mcColor)` returns `mcColor` (`#9b59b6`) for any `isMC` node, so channel messages, links, labels, and the ring flash all follow the same palette.
  - **Self node fills match its protocol**: MT self stays orange `accent`; MC self now fills with purple `mcColor`. Sonar ring stroke matches the self's protocol color. A dual-radio rig thus shows two clearly-distinct "my radio" dots.
  - **Protocol-aware link tree**: peer linking now prefers a same-protocol ancestor so MT and MC sub-trees stay untangled. New `mtRoot` / `mcRoot` derived from the self-node set; the "closest already-linked node" search filters out cross-protocol candidates unless the preferred root is the only option. Channels still link to `mtRoot` (public channel 0 is a meshtastic concept for now).
  - **Clickable self-node with live telemetry panel**: `onCanvasClick()` no longer short-circuits to a toast for self-nodes — it calls `openFloatWindow(closest)` uniformly. `openFloatWindow(node)` detects `node.isSelf` and forwards to the new `openSelfWindow(node)` which builds a panel with no thread history and no composer (self-node has no inbox). `loadSelfData(key)` populates it from `App.state` — protocol badge + glyph, status (CONNECTED/DISCONNECTED colored), transport, unified node ID, battery % / voltage (with low-battery warning at ≤20%), temperature, humidity, channel util, hardware model, uptime, nodes seen, messages, LLM model. Data is sourced entirely from `state.backends[]` + `state.device_metrics[self_node_id]`, so refresh is free — poll loop now calls `loadSelfData` every tick when a self panel is open (vs. the 5-poll `/api/threads` debounce used for peer panels). `loadFloatData(nodeId)` redirects `__self_*` ids to `loadSelfData` as a safety net.
  - **Always-visible protocol legend**: new `#proto-legend` div under the HUD shows `● MESHTASTIC` + `◆ MESHCORE` with matching colors. Unlike the existing `#hw-legend` (hidden unless HW-color mode is on), the protocol legend is always visible so operators see the shape/color key by default.
  - **Peer-panel protocol fallback**: `loadFloatData()` now prefers `contact.protocol` but falls back to the `mc:`/`mt:` id prefix, so a newly-seen MC peer with no contact record yet no longer mislabels its panel as "PROTOCOL: MT".
- Why:
  - User observed that the dashboard only reported a single collapsed CONNECTED/DISCONNECTED state even when running both radios, and that self-nodes were unclickable (dead-ended in a toast) so there was no live view of battery / hardware / uptime for your own radio. Also asked for a visible way to tell MT vs MC nodes apart — color alone wasn't enough, and the legend was buried. The request explicitly called out the meshtastic+meshcore dual-radio case ("when 2 nodes are connected ... display both but make them 2 separate colors and make meshcore nodes and meshtastic nodes different shapes to differentiate").
- Impact on project goals:
  - Closes a usability gap exposed by v2 Phase 1 (dual-backend support shipped, but the UI still treated the bridge as a single radio). The dashboard is now first-class bilingual: every connection state, every self-node, every peer node, and every label/link line on the canvas tells you which protocol you're looking at. For the defense-tech portfolio narrative this matters — a demo of "AI-gated cross-protocol relay" is a lot more legible when the UI shows both sides at once rather than hiding one behind a flag. Zero backend / schema / API changes — all work is client-side in `DASHBOARD_HTML` plus a small refactor to the connection-status paint logic.
- Files modified:
  - `meshtastic-bridge/dashboard.py` — HTML (top bar + HUD legend), CSS (`.lo-dot.mc`, `.lo-conn-row`, flex-shrink fixes), JS (`buildGraph` multi-self + same-proto linking, `renderCanvas` diamond shape + MC self-fill, `onCanvasClick` unified, new `openSelfWindow` + `loadSelfData`, `loadFloatData` self redirect + protocol fallback, `poll()` dual-dot paint + self-panel per-tick refresh, `isSelfId` / `drawDiamond` helpers)
  - `README.md` — "Dashboard dual-radio UI" subsection under the dual-radio docs
- Tests: 300/300 pass (no new tests — changes are DOM / canvas rendering; existing dashboard-API tests cover the Python surface, which is unchanged).

---

## [2026-04-17 18:00] — LORACLE Bridge v2 Phases 2–5: cross-protocol relay, AI gating, hardening (software-complete)

- What changed:
  - **Phase 2 — Bridge core** (commit `faf54e7`): New `meshtastic-bridge/bridge/` package ships the engine. `bridge/identity.py` owns the `[mt-Alice] payload` sender-prefix format and the `looks_bridged()` loop guard. `bridge/dedup.py` provides `RelayDedupCache` — thread-safe TTL cache keyed on `(source_protocol, dest_protocol, sender, payload_hash)` with a whitespace-insensitive fingerprint. `bridge/policy.py` introduces an ABC `Policy` + four implementations (`DisabledPolicy`, `AlwaysRelay`, `ChannelAllowlist`, `AIGatedPolicy`); Phase 2 ships `AIGatedPolicy` as a pass-through stub that Phase 4 fills in. `bridge/config.py` parses a JSON blob persisted in `SettingsStore` under the `bridge_config` key and composes the right Policy graph from per-channel rules; malformed rules are silently dropped rather than crashing startup. `bridge/relay.py` is the entry point — `Relay.observe(source, sender, text, channel, is_dm)` applies the loop guard, iterates candidate destinations, consults policy + dedup, formats with the sender prefix, and asks a caller-supplied `send_fn(dest, text, channel)` to deliver. `set_policy()` enables hot-swap on config reload. `StandaloneBridge.__init__` gains `_bridge_config`, `_bridge_events` (200-row ring buffer), and `self._relay`. New helpers: `_load_bridge_config`, `_save_bridge_config` (persist + hot-swap), `_bridge_send` (protocol-agnostic broadcast — meshtastic via `self.interface.sendText`, meshcore via `_radio_manager.send("mc:", …, is_dm=False)`), `_bridge_sender_display` (custom_name → long_name → short_name → last-6), `_on_bridge_relay` (ring-buffer append). `_on_receive` and `_secondary_radio_ingest_loop` both call `self._relay.observe()` after persistence, wrapped in try/except so relay failures don't break the receive path. Dashboard API: `GET /api/bridge/config`, `POST /api/bridge/config` (validates shape, hot-swaps live policy), `GET /api/bridge/stats`, `GET /api/bridge/events?since=<ts>`. 54 new unit tests.
  - **Phase 3 — BRIDGE UI tab** (commit `9582269`): New top-level BRIDGE tab in `dashboard.py` (button after AI, hidden from activity ribbon). Header: title + live ON/OFF badge + relayed/dropped/dedup counters. GLOBAL section: master-enable checkbox (when off, no relay happens regardless of per-channel rules). PER-CHANNEL RULES: add/remove rows with source picker (mt/mc), channel number input (blank = wildcard), mode dropdown (off/always/ai-gated), APPLY saves + hot-swaps the live policy, RELOAD pulls persisted config. LIVE FLOW: 200-row scrolling log with `mt→mc` / `mc→mt` arrows, timestamp, sender display, and the relayed text. Polling is on-demand — 2.5s interval only while the tab is visible, cancels on `setView` away. New `.lo-bridge-*` CSS classes. `_on_bridge_relay` now also emits `"bridge.relay"` SSE events via `_emit_sse` so future clients can subscribe. 252/252 tests still pass.
  - **Phase 4 — AI-gated relay** (commit `ff5067a`): `bridge/urgency.py` ships `HeuristicUrgencyClassifier` — keyword + structure heuristic, sub-millisecond decisions, no LLM call in the hot path. Vocabulary: distress (mayday/sos/urgent/alert/critical), casualty/medical (medic/medevac/injured/wounded/bleeding/unconscious), fire/disaster (fire/smoke/flood/earthquake), threat (shot/shots/shooting/attack/hostile), stuck/lost (stranded/trapped/missing/crashed). Word-variant aware: matches flood/floods/flooding/flooded, shot/shots, attack/attacks/attacked/attacking. Chatter allowlist (hi/hello/roger/copy/wilco/thanks) always false regardless of shouty formatting. Weak fallback: multi-`!` + meaningful uppercase = probable urgency. `HeuristicUrgencyClassifier(extra_urgent_keywords=[...])` lets operators extend the vocabulary for unit callsigns / operation-specific terms. `build_policy` now plugs the real classifier into `AIGatedPolicy` for every ai-gated rule (replaces the Phase 2 stub); `cfg.urgent_keywords` piped through from `bridge_config`. Force-relay: `Relay.observe` recognises `!urgent` / `!priority` / `!sos` / `!mayday` at message start (case-insensitive, optional `:`/`,`/`-` separator), strips the prefix, bypasses policy entirely (even DisabledPolicy). DMs still never cross — bridging private conversations is a trust decision, not a priority decision; `!urgent` on a DM is still dropped. Dedup still applies post-strip. Bang-word-alone drops as a no-op rather than relaying an empty message. 24 new tests (13 urgency + 11 force-relay). 276/276 pass. LLM-rewrite mode (Ollama summarisation/translation) deferred to v2.1.
  - **Phase 5 — Hardening** (commit `9a1de79`): `bridge/rate_limit.py` ships `RelayRateLimiter` — thread-safe sliding-window counter keyed per `(source_protocol, dest_protocol, channel)`, default 30 events / 60s, tunable via `bridge_config.rate_limit_max` / `rate_limit_window_s`. Rejected events don't consume quota; force-relayed (`!urgent`) messages bypass the limiter. `db/bridge_events.py` introduces `BridgeEventStore` backed by a new `bridge_events` SQLite table with CHECK-constrained outcome (`relayed`/`blocked`/`rate_limited`/`deduped`/`loop_guard`), indexed on `(timestamp DESC)` and `(outcome, timestamp DESC)`; 30-day retention pruned on startup; text truncated at 2000 chars to bound table growth. New `GET /api/bridge/history?limit&since&outcome` endpoint. `Addon.on_bridged_message(event)` default-no-op hook fires for every successful relay — lets Sentinel/Triage/Brief observe cross-protocol traffic without SSE/polling; exceptions in the hook are caught so one broken addon can't break the relay path. `Relay.stats()` now includes `rate_limited` count, per-direction current-window view, and the configured limit. Integration test harness (`tests/test_bridge_integration.py`) uses `FakeRadio` + `MockAddon` to exercise the full pipeline end-to-end without hardware: bidirectional relay with sender prefix, echo-doesn't-loop, ai-gated chatter-vs-urgent, `!urgent` bypasses `DisabledPolicy`, DMs never cross (even with `!urgent`). 24 new tests (10 rate-limit + 9 bridge_events store + 5 integration). 300/300 pass total.
- Why:
  - User asked to keep going through all FSD phases: "once done keep going until all tasks are complete on the fsd i will validate later". Phase 1 proved the dual-backend infrastructure worked; Phase 2 added the actual cross-protocol relay (the thing that makes LORACLE more than two side-by-side radios); Phase 3 surfaced operator controls; Phase 4 plugged in the defense-tech differentiator (AI gate on what crosses — a dumb bridge is Akita, an AI-gated bridge is the pitch); Phase 5 made the whole thing production-grade with rate limiting, audit log, addon hook, and mock-backend integration tests.
- Impact on project goals:
  - LORACLE Bridge v2 is now a full cross-protocol relay platform with AI-gated routing. The defense-tech portfolio narrative (Anduril / Palantir / SOCOM) has a concrete demonstrable artifact: messages on Meshtastic auto-forward to MeshCore with a sender-network prefix; chatter stays local while urgent traffic crosses; every relay decision is auditable from the SQLite `bridge_events` table; operators can tune rules live from the BRIDGE tab; addons observe cross-protocol events via a stable hook API. Zero behaviour change when `bridge_config.enabled=false` or `--second-radio` isn't set — the v1 code path is untouched. `LORACLE_BRIDGE_V2_FSD.md` and this CONTEXT entry give any fresh-context agent a complete picture of the v2 arc, the decisions locked in, and the hardware-verification gates the user still owns.
- Files modified:
  - `LORACLE_BRIDGE_V2_FSD.md` — all phases marked complete, status header updated, progress log appended for Phases 2–5
  - `meshtastic-bridge/bridge/` — new package with 7 modules (`__init__`, `identity`, `dedup`, `policy`, `config`, `relay`, `urgency`, `rate_limit`)
  - `meshtastic-bridge/db/bridge_events.py` — new BridgeEventStore
  - `meshtastic-bridge/db/schema.py` — new `bridge_events` table + indexes
  - `meshtastic-bridge/addons/base.py` — new `on_bridged_message(event)` default-no-op hook
  - `meshtastic-bridge/standalone_bridge.py` — Relay instantiation, `_bridge_*` helpers, `_on_bridge_relay` with audit-log persist + addon hook + SSE emit, rate-limiter plumbing, bridge_events prune on startup, `observe()` calls from both receive paths
  - `meshtastic-bridge/dashboard.py` — BRIDGE tab (HTML + CSS + JS), `/api/bridge/config`, `/api/bridge/stats`, `/api/bridge/events`, `/api/bridge/history` endpoints
  - `meshtastic-bridge/tests/` — 8 new test files covering identity / dedup / policy / relay / urgency / force-relay / rate-limit / bridge_events / integration. 300 tests total.
  - `README.md` — dual-radio section (updated during Phase 1 finish)

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
