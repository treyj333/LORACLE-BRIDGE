<div align="center">

# LORACLE BRIDGE
### Offline AI + Cross-Protocol Mesh Radio Bridge

**Merges two separate mesh-radio networks (Meshtastic + MeshCore) into one, with a private AI assistant built in — no internet, no cellular, no cloud.**

</div>

---

## What Is This? (Plain-English)

LORACLE Bridge does two big things that normally require the internet, a phone signal, or a server in the cloud — and it does both **completely offline**, powered by nothing but a laptop and a couple of radios:

### 1. It connects two separate radio-mesh networks that normally can't talk to each other.

**Meshtastic** and **MeshCore** are two popular long-range text-messaging radio networks. They both run on LoRa (the same radio technology) but they speak different languages — a Meshtastic radio can't hear a MeshCore radio, and vice-versa. That means a neighborhood, a unit, or a trail group running one network can't message a group running the other. It's like having Verizon and AT&T phones that couldn't call each other.

**LORACLE fixes that.** Plug a Meshtastic radio and a MeshCore radio into the same computer and LORACLE becomes a *translator* between the two networks. When someone on Meshtastic sends a message on the public channel, LORACLE re-broadcasts it on the MeshCore network — tagged `from meshtastic (Alice): …` so the people on the other side know where it came from. Same in the other direction. The two networks start behaving like one big network.

### 2. It runs an AI assistant that anyone on the mesh can text — with no internet needed.

Normally when you ask an AI a question, your message goes to a data center, gets answered there, and comes back. LORACLE runs the entire AI **on your own laptop** — using a local model served by [Ollama](https://ollama.com). Anyone in radio range who sends a text to your node gets a real answer back, even if there's no cell service, no wifi, no power grid.

You can also drop PDFs and text files (survival guides, SOPs, maintenance manuals, maps, whatever) into the `CONTEXT FILES/` folder, and the AI will use those documents to ground its answers. Ask "what's the procedure for a casualty evac" and it can cite your actual field manual instead of guessing.

### Put them together and you have:

A **battery-powered, cellular-free, internet-free** communications + AI platform where a whole mix of radios (Meshtastic *and* MeshCore) show up in one dashboard, messages cross between them automatically, and everyone on either network can ask an on-device AI real questions and get grounded answers back as text over radio. Useful for: disaster response, backcountry groups, off-grid communities, tactical / SAR teams, anywhere you have radios but no infrastructure.

```
    [Meshtastic mesh]                          [MeshCore mesh]
         radios                                     radios
           ⇅                                          ⇅
    ┌──────────────┐                          ┌──────────────┐
    │  Meshtastic  │                          │   MeshCore   │
    │    radio     │◀───── LORACLE host ─────▶│    radio     │
    │  (plugged    │     (laptop + Ollama     │  (plugged    │
    │   into USB)  │       + this software)   │   into USB)  │
    └──────────────┘                          └──────────────┘
           ▲                                          ▲
           │          public-channel traffic          │
           │      re-broadcast in both directions     │
           │                                          │
           └── AI assistant answers DMs from ─────────┘
                       either network
```

Either radio is optional — you can run just Meshtastic, just MeshCore, or both. With both plugged in, the cross-protocol bridge turns on automatically for public channel 0, no configuration required.

**No internet. No cloud. No API keys. No phone plan.** Everything runs locally.

---

## Technical Summary (For the Rest)

Below the plain-English section, the rest of this README gets more detailed. Skip to:

- **[What You Need](#what-you-need)** — hardware and software requirements
- **[Quick Start](#quick-start--3-steps)** — three-command install and run
- **[Web Dashboard & Control Panel](#web-dashboard--control-panel)** — the mesh-visualization UI
- **[Supported Protocols](#supported-protocols)** — Meshtastic + MeshCore details, dual-radio setup, cross-protocol bridge
- **[Connection Methods](#connection-methods)** — Serial / BLE / TCP transports
- **[Knowledge Base (RAG)](#knowledge-base-rag)** — dropping your own PDFs/docs for grounded answers
- **[Mesh Commands](#mesh-commands)** — `!help`, `!drop`, `!triage`, `!brief`, `!nav`
- **[All Command-Line Options](#all-command-line-options)** — every CLI flag
- **[CONTEXT.md](CONTEXT.md)** — timestamped changelog with the "what / why / impact" of every change
- **[LORACLE_BRIDGE_V2_FSD.md](LORACLE_BRIDGE_V2_FSD.md)** — the v2 dual-radio/bridge design doc

---

## What You Need

### Hardware

| Item | What It Does |
|------|-------------|
| **Computer** | Runs the AI model and this bridge software. Mac or Linux. |
| **Meshtastic radio** | The LoRa radio that connects to the mesh network. Any Meshtastic-compatible board works (T-Beam, Heltec, RAK, etc.) |
| **USB-C cable** | Connects the radio to your computer. This is the default and simplest connection method. |

> **Other people on the mesh only need their own Meshtastic radio.** They don't need a computer — they just type messages on their radio or phone (via the Meshtastic app) and your bridge responds automatically.

### Software (auto-installed)

You don't need to install any of this yourself. The launch script handles everything on first run:

| Software | What It Does | How It's Installed |
|----------|-------------|-------------------|
| **Homebrew** | macOS package manager used to install everything else | Auto-installed if missing |
| **Python 3.10+** | Runs the bridge code (3.10+ required for MeshCore support) | Auto-installed via Homebrew if missing |
| **Ollama** | Runs AI models locally on your machine — no cloud needed | Auto-installed via Homebrew (macOS) or install script (Linux) |
| **AI Model** | Auto-selected based on your RAM (see below) | Auto-pulled by Ollama on first run |

### Smart Model Selection

The bridge automatically picks the best AI model for your system:

| Your RAM | Model Selected | Why |
|----------|---------------|-----|
| **16GB+** | `phi4:14b` | Best reasoning, connects dots across RAG documents |
| **8GB+** | `qwen3:8b` | Latest generation, excellent instruction-following |
| **< 8GB** | `gemma3:4b` | Lightweight fallback, runs on low-RAM devices |

Other supported models (auto-selected if installed): `qwen3:14b`, `qwen2.5:14b`, `qwen2.5:7b`

You can override with `--model <name>` (e.g., `./mesh-llm.sh --model qwen3:14b`).

---

## Quick Start — 3 Steps

> ⚠️ **Before you start — set up your radio in its native app first.**
> LORACLE Bridge is a host-side runtime that *reads and drives* an already-provisioned radio — it is **not** a first-time-setup tool. Before you plug the radio into LORACLE:
>
> - Pair the radio with the official **[Meshtastic](https://meshtastic.org/docs/software/)** (or **[MeshCore](https://meshcore.co.uk)**) phone/desktop app.
> - Set your **region / frequency** (e.g. `US`, `EU_868`, `ANZ`). A radio with no region set will look connected in LORACLE but transmit and receive nothing.
> - Give the node a short name, configure any channel keys / PSKs, and confirm the radio is actually talking to other nodes on your mesh.
>
> If you skip this step your nodes may not work at all with LORACLE, or will look idle because they're not legally / correctly on air. Set it up in the native app, verify it works there, *then* come back.

### Step 1: Clone This Repo

```bash
git clone https://github.com/treyj333/LORACLE-BRIDGE.git
cd LORACLE-BRIDGE
```

### Step 2: Plug In Your Radio

Connect your Meshtastic radio to your computer with a USB-C cable. That's it — the bridge auto-detects it.

### Step 3: Run It

**Option A — Double-click:** Open `LORACLE BRIDGE.command` in the project folder. It launches the bridge and opens the dashboard in your browser automatically.

**Option B — Terminal:**
```bash
./mesh-llm.sh
```

**That's the whole setup.** On first run, the script will:

1. Install Homebrew (if you don't have it)
2. Install Python (if you don't have it or it's too old)
3. Install Ollama (if you don't have it)
4. Start the Ollama service
5. Download the `gemma3:4b` AI model (if no models are installed)
6. Pull the `nomic-embed-text` embedding model for the knowledge base
7. Auto-ingest any documents in `CONTEXT FILES/`
8. Create a Python virtual environment and install dependencies
9. Auto-detect your radio on USB
10. Start listening for messages and responding with AI

You'll see output like this:

```
==========================================
  LORACLE BRIDGE
==========================================

OK Python 3.12.13
OK Ollama installed
OK Ollama running
OK 2 model(s) available
OK Python environment ready

==========================================
  Bridge starting...
  Connection: USB Serial (auto-detect)
  RAG: enabled
  Dashboard: http://localhost:8000
  Press Ctrl+C to stop
==========================================
```

Now anyone on your mesh network can send a text message and get an AI response back.

Press **Ctrl+C** to stop.

---

## Web Dashboard & Control Panel

The bridge includes a full-featured web control panel that starts automatically. Open it in your browser:

```
http://localhost:8000
```

### The Network Canvas

LORACLE's interface is a **living force-directed mesh visualization**. Every node is a dot, every connection is a line, every packet is a pulse. The mesh breathes — nodes gently pulse, and the layout reorganizes as topology changes.

| Element | What It Is |
|---------|-----------|
| **Orange circle (center)** | Your Meshtastic radio — `MY MT` |
| **Purple diamond (center)** | Your MeshCore radio — `MY MC` (appears when an MC radio is attached) |
| **Teal circles** | Meshtastic peer nodes on the mesh |
| **Purple diamonds** | MeshCore peer nodes on the mesh |
| **Empty ring (teal or purple)** | Public channel 0 for that protocol — click to broadcast |
| **Hop-chained links** | Nodes link through their relay path (direct → 1-hop → 2-hop), not a star. MT and MC keep separate trees; cross-protocol peers never share an edge |
| **Line thickness** | Signal strength — thick = strong, dotted = weak |
| **Orange badges** | Unread message count per node |
| **Gold star** | Favorited node — sorted to the top of the sidebar |
| **Red dot** | Low battery warning (≤20%) |

Shape + colour language is the universal "which protocol is this?" cue across every surface — canvas nodes, sidebar icons, status pills, legend, self-info panels. **Circle = Meshtastic, diamond = MeshCore, always.**

**Click any node** to open a thread panel with full message history (up to 50 messages), signal info, battery/voltage, temperature, hardware model, hop count, and a composer. Toggle AI auto-reply, star as a favorite, rename with a custom nickname, or run a traceroute — all from the same panel.

- **Rename a node**: double-click its name in the panel header, or hit the **RENAME** button. Nicknames persist across sessions and override the default short-name everywhere (canvas, sidebar, thread list).
- **Favorite a node**: hit the **★ FAV** button. Favorites float to the top of the node sidebar and show a gold star on the canvas.
- **Color nodes by device type**: the **HW COLOR** button in the HUD is **on by default** — T-Beam, Heltec, RAK, T-Deck, Station, Nano each get a distinct color. A legend appears under the HUD listing the models in view. Toggle off and your preference persists across sessions.
- **Message send status**: outbound messages show a pill next to the text — ⧗ (sending, pulsing), → (sent), ✓ (radio ACK), ✓✓ (delivered), ✗ (failed). Failures are surfaced in-UI instead of vanishing silently.

**Drag the canvas** to pan around and see nodes outside the viewport. **Scroll wheel or trackpad-pinch** to zoom in and out (zoom anchors on the cursor, like a map app). **Double-click** to reset both pan and zoom. Nodes gently drift and breathe when the mesh is idle. A toast fires whenever a new node comes online mid-session.

### Views

| View | What It Shows |
|------|-------------|
| **MESH** | Default — the live force-directed mesh canvas with all nodes and links |
| **TRAFFIC** | Same canvas, emphasis on active packet flow (inactive nodes dim) |
| **MAP** | Geographic Leaflet map with node markers, auto-fit bounds, coverage heatmap layer — click any marker to open the same thread panel used on the mesh canvas |
| **AI** | Direct chat with the local Ollama model — no radio needed. Useful for quick reference queries when you're not using the mesh. History is isolated from per-node conversations |
| **BRIDGE** *(v2)* | Cross-protocol relay controls — live ON/OFF badge, per-channel rule editor (off / always / ai-gated), relayed/dropped/dedup counters, scrolling flow log. See [Cross-Protocol Bridge](#cross-protocol-bridge-v2-phases-25--software-complete) below |
| **CONFIG** | Full settings: connection, channels, radio, model, routing, RAG, knowledge packs, data, appearance |

### Protocol Scope Selector — `[ ALL | MT | MC ]`

Sits in the top bar next to the connection dots. Switches every view — canvas, node-list sidebar, map markers, click targets, and the HUD `NODES` counter (shown as `visible / total` when filtered) — to show only Meshtastic nodes, only MeshCore nodes, or both. The active button glows teal for MT and purple for MC to match the node colours. Choice is persisted in `localStorage`, so reloading the page remembers what you were looking at.

Neither protocol is the "default"; every node gets an equal teal `mt` or purple `mc` badge in the sidebar, and when two radios are connected the centre of the mesh shows both "MY MT" and "MY MC" self-nodes side-by-side. Feature endpoints that only one protocol implements (traceroute, writable LoRa config) return a clean "not supported on this radio" toast instead of silently failing.

The first-run wizard is protocol-agnostic: step 1 is labelled "PRIMARY RADIO" (not "MESHTASTIC"), step 2 is "SECOND RADIO (OPTIONAL)", and success copy frames the second radio as a cross-network bridge rather than an optional add-on. Pick `MeshCore` from the protocol dropdown in step 1 and the first-connect flow will wire up an MC radio (same CTA, same success panel, just in MC purple) — users running a MeshCore-only rig never have to touch the "add secondary" path.

The BRIDGE tab's stats row is split into two mirrored direction columns — `MT → MC` (teal) and `MC → MT` (purple) — each showing its own `relayed` / `dropped` counts pulled from `Relay.stats().by_direction`. Flow-log arrows are colour-coded by source protocol, so you can eyeball which direction a relay event came from without reading the `mt→mc` / `mc→mt` text.

### Cross-Protocol DM — `!dm <name-or-id> <text>`

A new mesh command lets users on either protocol DM someone on the *other* mesh. The bridge resolves `<name-or-id>`:
- Raw unified ids (`mc:abcdef`, `mt:!abc12345`) and bare Meshtastic ids (`!abc12345`) are used directly.
- Anything else is looked up by `custom_name` / `long_name` / `short_name` against the contacts DB. When a name matches on both meshes, the *other* mesh wins (the command's whole point is cross-mesh reach). Ambiguous matches get a candidate list back instead of a guess.

Cross-mesh delivery is **off by default** — bridging private messages across protocols is a trust decision. Flip the `CROSS-PROTOCOL DM` toggle in the CONFIG tab to enable it on your bridge. Same-mesh `!dm` by nickname always works and is just a convenience. Cross-mesh sends are prefixed `from meshtastic (…): <text>` (or `from meshcore (…): …`) so the recipient knows where the message came from, mirroring the public-channel relay tag.

### Node List Sidebar

Click the **☰** button in the title bar to open the node sidebar. At the very top sit two **pinned PUBLIC CHANNEL rows** — one teal-circle row for Meshtastic channel 0 and one purple-diamond row for MeshCore channel 0 (each visible when that radio is connected). One click opens a composer that broadcasts on the 915 MHz public channel for the matching protocol, no matter which scope filter you're in. Below the pinned rows the regular list shows all peers with hops, last-heard time, and unread count. Sort by name, hops, heard, or unread. Filter with the search box. Click any row to open that node's floating window.

### HUD Overlay

The top-left corner shows live stats (nodes, messages, model, uptime). The control rail underneath **swaps based on the scope filter**: in `ALL` / `MT` it shows the Meshtastic `SCAN MESH` + `HW COLOR` pair plus the per-hardware colour legend; switching to `MC` replaces both with MeshCore-flavoured controls (`PULL CONTACTS` to force a contact-list refetch, `FAVORITES` to hide everyone you haven't starred, live counts of `contacts / with GPS / starred`, and an advertisement-based legend). The bottom strip shows a packet activity ribbon.

### Header Radio Pills — `● MT` + `◆ MC`

Right of the two connection indicators (`MT ON` / `MC ON`) sit a pair of **management pills**. Each opens that radio's self-info panel (status, transport, node id, battery/voltage/temperature, hardware model, uptime) — the same panel you get by clicking the MY NODE dot on the canvas. Shape + colour match the protocol: **teal circle (`● MT`)** for Meshtastic, **purple diamond (`◆ MC`)** for MeshCore. When a radio isn't connected the MT pill reads `○ MT` and falls through to the connect modal; the MC pill reads `+ RADIO` and opens the add-secondary-radio wizard.

### Public Channel Quick-Send

The node sidebar pins two **PUBLIC CHANNEL rows** at the very top — one for each connected radio. Clicking either opens a composer that broadcasts on public channel 0 for that protocol. With the auto-bridge on, **typing in either composer also echoes through the relay to the other protocol**, so a message you type on `PUBLIC CHANNEL · MESHCORE` appears on the Meshtastic mesh as `from meshcore (<your-name>): <text>` (and vice versa). The relayed copy is also written back into the destination thread's message store, so **both composers show the message crossing in real time** — no need to hop to the BRIDGE tab to see the relay fire. All the usual guards still apply: DMs never cross, already-bridged text is loop-suppressed, and the dedup cache prevents echoes.

### Bridge Smoke Test

The BRIDGE tab has a **VALIDATE — SMOKE TEST** section with a one-click `RUN SMOKE TEST` button. It (1) broadcasts a tagged ping on public channel 0 from every connected radio so nearby peers hear it, then (2) injects a synthetic "incoming peer" message through `Relay.observe()` in both directions so the relay policy + dedup + cross-protocol send actually fire (broadcasts from your own radio never trigger the relay — it only observes incoming peer traffic). The result panel shows send-level outcomes, the pre/post counter delta, and a pass/fail summary within ~1 s. Use this any time you want to confirm the bridge is wired through end-to-end without needing a second device in range.

The dashboard updates every 2 seconds. To change the port:

```bash
./mesh-llm.sh --dashboard-port 9000
```

---

## Supported Protocols

LORACLE Bridge supports two mesh radio protocols:

| Protocol | Library | Radios | Status |
|----------|---------|--------|--------|
| **Meshtastic** | `meshtastic>=2.3.0` | T-Beam, Heltec, RAK, etc. | Full support (serial, TCP, BLE) |
| **MeshCore** | `meshcore>=2.2.1` | MeshCore companion devices | Secondary-radio support via `--second-radio` (serial, TCP, BLE). Requires Python 3.10+. |

### Dual-Radio Mode (LORACLE v2)

You can run **both radios simultaneously** — Meshtastic as the primary and MeshCore as a secondary — by passing `--second-radio`:

```bash
# Meshtastic on /dev/ttyUSB0 + MeshCore on /dev/ttyUSB1 (serial)
./mesh-llm.sh --second-radio meshcore:serial:/dev/ttyUSB1

# Or MeshCore over TCP
./mesh-llm.sh --second-radio meshcore:tcp:192.168.1.50:4000

# Or MeshCore over BLE
./mesh-llm.sh --second-radio meshcore:ble:AA:BB:CC:DD:EE:FF
```

Messages from both networks land in the same dashboard — same node list, same canvas, same message threads. MT and MC are differentiated by **shape + color** (circle/teal vs diamond/purple), not by separate tabs.

**Connecting your radios — zero CLI required.** The first time you open the dashboard, a two-step wizard walks you through connecting a Meshtastic radio (step 1) and a MeshCore radio (step 2). Either step can be skipped if you only have one kind of radio. Each step has a **SCAN PORTS** button that lists every available serial device on your system with a green-dot hint on the ones that look like a LoRa radio (CP210x / CH340 / FT232 / manufacturer-name match) — click a row to fill in the path. After each successful connect you get a confirmation screen ("✓ MESHTASTIC CONNECTED" / "◆ MESHCORE CONNECTED") with a clear NEXT / DONE button so the flow is user-paced, not a flash-then-close. After the wizard, the `+ RADIO` button in the top bar opens the same MeshCore connect/disconnect modal any time. The spec is persisted to `~/.mesh-llm/settings.json` so the bridge auto-restores it on the next restart — the `--second-radio` CLI flag still works and wins if both are set.

**Bridge just works.** When both radios are up for the first time, LORACLE automatically turns on bidirectional public-channel (channel 0) relay so messages cross without any config. Messages are tagged `from meshtastic (Alice): …` / `from meshcore (…): …` so recipients on the other network see at a glance where each message came from. If you want to turn the auto-bridge off (or back on), the BRIDGE tab has a single-click toggle at the top labeled **Auto-relay public channel 0 between Meshtastic and MeshCore**. The old per-channel rule editor is still there behind an `ADVANCED RULES` disclosure for multi-channel setups and the AI-gated urgency filter.

**Dashboard dual-radio UI:**
- **Top bar** shows each backend's connection state independently — a teal circle for Meshtastic (`● MT ON/OFF/--`) and a purple diamond for MeshCore (`◆ MC ON/OFF/--`). Right of those sit matching management pills: `● MT` (circle) opens the Meshtastic self-info panel, `◆ MC` (diamond) opens the MeshCore self-info panel. An unconnected MC pill reads `+ RADIO` and launches the add-secondary-radio wizard.
- **Mesh canvas** draws both "my radios" at center when both backends are connected — an orange circle labeled `MY MT` ~90 px to the left, a purple diamond labeled `MY MC` ~90 px to the right, so the two trees root at visibly-distinct hubs instead of piling up. Peers render with the same shape + colour convention (circle = Meshtastic, diamond = MeshCore) and the two meshes stay as visually-separate sub-trees — cross-protocol peers never share an edge.
- **Scroll-wheel / trackpad-pinch zoom** on the canvas (anchored on the cursor). Double-click resets both pan and zoom.
- **Protocol legend** under the HUD (`● MESHTASTIC` / `◆ MESHCORE`) is always visible.
- **Unified messaging.** Clicking any peer (MT or MC) opens the same panel and send flow. DMs route through RadioManager so MeshCore peers are first-class — send arrives on the right radio automatically. Public-channel broadcasts on either protocol also auto-cross via the relay (messages show up in both PUBLIC CHANNEL threads in real time).
- **Clicking your own radio** opens a live telemetry panel with protocol, transport, node ID, battery % / voltage, temperature, humidity, channel util, hardware model, uptime, and node/message counts. Works the same for MT and MC — the panel tints teal or purple based on which self-node you clicked.

### Cross-Protocol Bridge (v2 Phases 2–5 — software-complete)

When both a Meshtastic and a MeshCore radio are attached, channel messages cross between the two networks automatically. **Public channel 0 relay is ON by default** the first time a second radio is attached — no configuration required for the common case. Toggle it on/off from the BRIDGE tab's `Auto-relay public channel 0` checkbox, or hand-edit per-channel rules in the `ADVANCED RULES` disclosure.

Features shipped:

- **BRIDGE tab in the dashboard** — live ON/OFF badge, relayed/dropped/dedup counters split into mirrored `MT → MC` / `MC → MT` columns, per-channel rule editor (source / channel / mode), scrolling flow log with colour-coded direction arrows showing every relay as it happens, plus a **VALIDATE — SMOKE TEST** button that exercises the full relay path both ways in ~1 s (no second device required).
- **Self-echo for dashboard sends** — messages typed on either PUBLIC CHANNEL composer inside the bridge also feed into `Relay.observe()` so they cross to the other mesh. Without this, user-initiated sends never triggered the relay (it only observes incoming peer traffic). Relayed copies are written back to the destination thread's message store, so both composers display the message traveling cross-protocol.
- **Per-channel rules** — three modes:
  - `off` — channel does not bridge.
  - `always` — every channel broadcast crosses (Akita-parity dumb bridge).
  - `ai-gated` — messages are scored by a heuristic urgency classifier; only urgent traffic crosses. The classifier knows defense-tech mesh vocabulary (distress, medical, fire, threat, stuck/lost) and ignores chatter (hi / roger / copy / thanks).
- **Force-relay prefixes** — a sender can prepend `!urgent`, `!priority`, `!sos`, or `!mayday` to force a message across the bridge past every policy. The prefix is stripped before the message reaches the other network.
- **Sender tagging** — relayed messages are rendered as `from meshtastic (Alice): original text` (or `from meshcore (Alice): …`) so recipients on the other network can see where the message came from in plain English. Custom nicknames / long names are preferred over raw node ids.
- **Loop prevention** — two guards: the `from meshtastic (…):` / `from meshcore (…):` prefix short-circuits (bridge-originated text never re-relays), and a 5-minute dedup cache catches any that slip through.
- **Per-direction rate limiting** — default 30 events / 60s per (source, dest, channel) tuple. `!urgent` bypasses the limit.
- **Persistent audit log** — every successful relay writes to the `bridge_events` SQLite table with 30-day retention. `GET /api/bridge/history` queries it.
- **Addon hook** — `Addon.on_bridged_message(event)` fires for every relay so custom addons can observe cross-protocol traffic.

DMs never cross the bridge — bridging private conversations is a trust decision, not a priority decision.

#### v2.5 — Relay Health panel + synthetic self-test

**v2.5 is the integration-reliability + operator-observability release that seals the MT↔MC public-channel relay for v1.0 ship.** Prior phases shipped the relay core; v2.5 closes the last gaps that kept the bridge at "90% there."

Three changes:

1. **Startup auto-seed backstop.** Previously, default bridge rules only seeded when the secondary radio was added via the CLI `--second-radio` flag or the dashboard "+ RADIO" modal. A persisted-config restore on restart never triggered seeding, so operators who set up the bridge once and rebooted could find it silently disabled. v2.5 adds a one-shot poller that, at startup, waits until two backends are connected and — if no rules exist — seeds the defaults with `enabled=true` regardless of which path brought the secondary up. It fires at most once per process and never overwrites custom rules.

2. **Diagnostic log at the top of `Relay.observe()`.** An INFO line now fires the moment a message reaches the relay, distinctly from the existing drop/relay logs. This separates "did the message get here?" from "did it pass the guards?" — the diagnostic question operators hit first at 11pm.

3. **RELAY HEALTH panel in the BRIDGE tab.** Four blocks, always visible:
   - **Live status card** — four traffic-light indicators: master switch (with inline toggle), rules configured, backends connected, and **relay wiring** (goes green when `observe()` has been called for real traffic in the last 60s; the single most diagnostic field — it separates wiring problems upstream from policy problems downstream). Hover `?` gets a plain-English explanation.
   - **Stats table** — mirrored MT→MC / MC→MT columns for relayed, dropped, and rate-limited, plus a subline with dedup cache size, rate-limit window, and bridge uptime.
   - **Live log tail** — last 20 `[bridge]` log lines, colour-coded (green = relay success, yellow = drops/dedup/rate-limit, red = send failure, cyan = `[SELFTEST]`). Auto-scroll, copy-last-20 button, 2.5s refresh.
   - **Run Relay Self-Test** — a button that injects synthetic public-channel messages both ways via `Relay.observe(..., is_selftest=True)`. The full guard chain fires (policy, dedup, rate-limit) but `send_fn` is short-circuited — no radio traffic ever leaves the laptop. Every drop reason in `relay.py` now maps to a readable string (`policy:no_rule_matched source=meshcore channel=0`, `rate_limited:60s`, `send_error:ConnectionError:...`) so failures are actionable, not mysterious. Tagged `is_selftest=True` on the on_relay hook so self-test traffic is filtered from the main messages tab but still appears in Block 3's log tail with a `[SELFTEST]` prefix.

Under the hood:

- `Policy.should_relay()` now returns `(bool, reason: str)` instead of bare `bool`. Every concrete policy (`DisabledPolicy`, `AlwaysRelay`, `ChannelAllowlist`, `AIGatedPolicy`) returns a stable reason vocabulary that surfaces through the self-test endpoint and the log tail.
- `Relay.observe()` gains a keyword-only `is_selftest` arg; existing callers are untouched. `Relay.stats()` adds `last_drop_reason_by_direction`, `rate_limited_by_direction`, `last_observe_at_s_ago`, and `uptime_s` to the existing `by_direction` block.
- Three new endpoints: `POST /api/bridge/selftest` (direction selector + structured per-direction results), `GET /api/bridge/health` (compact state for the status card), `GET /api/bridge/logs?limit=N` (filtered log tail).

Phase 1 limitations still apply:

- **MeshCore sends truncate** at the Meshtastic byte budget (233 bytes) — no `!more` paging yet.
- **Addon compatibility** on MeshCore-side incoming messages is best-effort; addons written for Meshtastic packet dicts may log warnings when processing raw MeshCore events.
- **LLM rewrite mode** (Ollama-backed summarisation / translation of bridged text) is deferred to v2.1.

See `LORACLE_BRIDGE_V2_FSD.md` for the full roadmap and phase-by-phase details.

CLI flags:
- `--protocol <auto|meshtastic|meshcore>` — primary-radio protocol detection
- `--second-radio protocol:transport:params` — connect a second radio (see examples above)
- `--ai-replies <on|off>` — toggle AI auto-replies globally

---

## Connection Methods

The bridge needs to talk to your Meshtastic radio. There are three ways to do this:

### USB Serial (Default — Recommended)

**What it is:** A physical USB-C cable between your computer and the radio.

**Why it's the best default:** Simplest, most reliable, no configuration needed. The bridge scans your USB ports and finds the radio automatically.

```bash
./mesh-llm.sh                                    # Auto-detect (just plug in the radio)
./mesh-llm.sh --serial /dev/cu.usbserial-0001    # Or specify the exact port
```

### TCP (Network Connection)

**What it is:** Connects to the radio over your local network (WiFi or Ethernet). The radio must have WiFi enabled and be on the same network as your computer.

**When to use it:** When you want to run the bridge AND the Meshtastic web client at the same time. Both connect over the network, so they can work simultaneously. Also useful if the radio is in a different room or mounted somewhere.

**Offline mode:** Your radio can create its own WiFi hotspot (AP mode) — no router or internet needed. Your Mac joins the radio's WiFi directly.

```bash
./mesh-llm.sh --tcp                        # Uses default: 192.168.1.1:4403
./mesh-llm.sh --tcp 192.168.1.100:4403     # Specify IP and port
```

> **Tip:** The default IP `192.168.1.1` is the standard Meshtastic WiFi AP address. If your radio is in AP mode, this just works.

### Bluetooth LE (Wireless, No Network)

**What it is:** A direct Bluetooth connection from your Mac to the radio. No cables, no WiFi.

**When to use it:** When you can't use USB and don't have WiFi set up on the radio. Note: only one Bluetooth client can connect at a time, so you can't use the web client simultaneously.

**Requires:** Python 3.11+ (auto-installed if needed).

```bash
./mesh-llm.sh --ble                        # Scan for nearby radios
./mesh-llm.sh --ble "AA:BB:CC:DD:EE:FF"   # Connect to a specific device
```

### Which Should I Use?

| Scenario | Connection | Command |
|----------|-----------|---------|
| Radio plugged into my computer | USB Serial | `./mesh-llm.sh` |
| Radio on WiFi, want web client too | TCP | `./mesh-llm.sh --tcp` |
| Radio on WiFi hotspot, fully offline | TCP | `./mesh-llm.sh --tcp` |
| Wireless, no WiFi on radio | BLE | `./mesh-llm.sh --ble` |

---

## Knowledge Base (RAG)

RAG (Retrieval-Augmented Generation) lets the AI answer questions using your own reference material. Think field manuals, survival guides, technical docs, or any PDF/text file.

**RAG is enabled by default.** Use `--no-rag` to disable it.

### How It Works

1. You drop files into the `CONTEXT FILES/` folder
2. On first run, the bridge breaks them into small chunks and creates searchable embeddings
3. When someone asks a question on the mesh, the bridge searches your documents for relevant info
4. That context is injected into the AI prompt so the answer is grounded in your actual documents
5. Files are only processed once — subsequent launches skip already-ingested files

### Setting It Up

**Step 1:** Put your files in the `CONTEXT FILES/` folder in the project root:

```
loracle/
  CONTEXT FILES/
    ranger-handbook.pdf
    survival-guide.txt
    medical-reference.pdf
```

**Step 2:** Just run the bridge — RAG is on by default:

```bash
./mesh-llm.sh
```

The script will automatically:
1. Pull the embedding model (`nomic-embed-text`) if needed
2. Auto-ingest all files from `CONTEXT FILES/` (skips files already ingested)
3. Start the bridge with knowledge base search enabled

**Supported file types:** `.pdf`, `.zim`, `.txt`, `.md`

### Adding Web Pages

You can also add web pages to the knowledge base directly from the dashboard:

1. Open `http://localhost:8000` and go to the **CONFIG** tab
2. Scroll to **Knowledge Base (RAG)**
3. Paste a URL and click **Add URL**
4. The bridge fetches the page, extracts the text, and ingests it

The page is saved as a `.txt` file in `CONTEXT FILES/` so it persists across restarts.

### Managing Documents

From the **dashboard** (CONFIG > Knowledge Base), you can view all ingested documents and delete individual ones.

From the **command line:**
```bash
./mesh-llm.sh --docs                    # List all ingested documents
./mesh-llm.sh --docs-stats              # Show knowledge base statistics
./mesh-llm.sh --ingest path/to/file.pdf # Manually ingest a single file
```

---

## Mesh Commands

Anyone on the mesh network can send these special commands (prefix with `!`):

| Command | What It Does |
|---------|-------------|
| `!help` | Shows the list of available commands |
| `!more` | Gets the next page of a long response (see "How Messages Are Sent" below) |
| `!status` | Shows bridge info: which model, uptime, node count, message count, RAG stats |
| `!model <name>` | Switches the AI model (e.g., `!model mistral`) |
| `!models` | Lists all installed Ollama models |
| `!clear` | Resets conversation history for your node (starts fresh) |
| `!ping` | Simple connectivity test — confirms the bridge is alive |
| `!rag on/off` | Toggles knowledge base search on or off for your node |
| `!docs` | Lists all ingested documents in the knowledge base |
| **Dead Drop Commands** | *(enabled by default)* |
| `!drop-key <passphrase>` | Register your encryption key for Dead Drop |
| `!drop <node> <message>` | Leave an encrypted message for another node |
| `!pickup` | Retrieve your pending encrypted messages |
| `!pending` | Check how many Dead Drop messages are waiting |
| **Triage Commands** | *(enabled by default)* |
| `!triage <question>` | Query the offline medical reference (TCCC/field medicine) |
| `!triage topics` | List available medical topics in the knowledge base |
| `!triage status` | Show medical knowledge base statistics |
| **Brief Commands** | *(enabled by default)* |
| `!brief` | Get the latest AI-generated situation report |
| `!brief now` | Generate a fresh SITREP immediately |
| `!brief history` | List available SITREPs by timestamp |

### Bridge Force-Relay Prefixes (v2)

When the cross-protocol bridge is enabled, senders can prepend one of these prefixes to force a channel message across the bridge past every policy (even the AI gate). The prefix is stripped before the message reaches the other network. DMs still never cross regardless of prefix.

| Prefix | Intent |
|--------|--------|
| `!urgent` | Force relay — elevated-priority traffic |
| `!priority` | Same as `!urgent`, different word choice |
| `!sos` | Distress signal |
| `!mayday` | Distress signal (radio convention) |

All prefixes are case-insensitive and accept an optional `:` / `,` / `-` separator, e.g. `!urgent: building on fire`. A bang-word alone (just `!urgent` with no body) is a no-op — nothing crosses.

---

## All Command-Line Options

```
./mesh-llm.sh [OPTIONS]

Connection (primary radio; default: USB serial, auto-detected):
  --serial <port>         Serial port for radio (default: auto-detect USB)
  --tcp <host:port>       TCP address for radio (e.g. 192.168.1.1:4403)
  --ble [address]         Connect via Bluetooth LE (scan if no address given)

Multi-protocol (v2):
  --protocol <auto|meshtastic|meshcore>
                          Primary-radio protocol detection (default: auto)
  --second-radio <spec>   Connect a secondary radio (MeshCore via
                          --second-radio meshcore:serial:/dev/ttyUSB1,
                          meshcore:tcp:HOST[:PORT], or meshcore:ble:ADDR)
  --ai-replies <on|off>   Global AI auto-reply toggle (default: on)

Model:
  --model <name>          Ollama model to use (default: auto-selected by RAM)
  --ollama-url <url>      Ollama API URL (default: http://localhost:11434)
  --list-models           List available Ollama models and exit

Response:
  --max-length <int>      Max response characters (default: 200)
  --system-prompt <text>  Custom system prompt for the AI
  --no-compression        Disable zlib compression on chunks

Knowledge Base (on by default):
  --no-rag                Disable RAG knowledge base
  --rag-dir <path>        RAG storage directory (default: ~/.mesh-llm/rag)
  --ingest <file|dir>     Ingest a file or directory into the knowledge base
  --docs                  List ingested documents
  --docs-stats            Show knowledge base statistics

Public Channel:
  --public-talk           Respond to trigger-word messages on public channels (default: on)
  --no-public-talk        Disable — DM-only mode

Greeter:
  --auto-greet            Proactively DM new nodes a welcome (default: on)
  --no-auto-greet         Disable
  --greet-message <text>  Override the default greeting text

Addons (all enabled by default):
  --enable-dead-drop      Enable Dead Drop
  --enable-triage         Enable Triage
  --triage-dir <path>     Triage medical KB directory (default: ~/.mesh-llm/triage)
  --enable-brief          Enable Brief
  --brief-interval <int>  SITREP generation interval in minutes (default: 60)
  --enable-navigation     Enable Navigation (bearing/distance helper)
  --enable-all-addons     Enable all available addons (default: on)

Other:
  --dashboard-port <int>  Web dashboard port (default: 8000)
  --help                  Show help and exit
```

### Examples

```bash
# Basic — plug in radio, run with defaults (RAG on, gemma3:4b)
./mesh-llm.sh

# Use a different AI model
./mesh-llm.sh --model mistral

# Connect to radio over TCP (WiFi AP mode)
./mesh-llm.sh --tcp

# Disable knowledge base
./mesh-llm.sh --no-rag

# Dual-radio: Meshtastic primary + MeshCore secondary over serial (v2)
./mesh-llm.sh --second-radio meshcore:serial:/dev/ttyUSB1

# MeshCore secondary over TCP
./mesh-llm.sh --second-radio meshcore:tcp:192.168.1.50:4000

# Custom system prompt for a specific use case
./mesh-llm.sh --system-prompt "You are a wilderness survival expert. Be concise."

# List what models you have installed
./mesh-llm.sh --list-models
```

---

## Addons — LORACLE Ecosystem

The bridge supports pluggable addons that extend its capabilities. Each addon adds mesh commands, a dashboard tab, and API endpoints. All addons are enabled by default.

### LORACLE DEAD DROP — Encrypted Async Messaging

Leave encrypted messages for mesh nodes that get picked up when they reconnect. Store-and-forward over LoRa.

- Nodes register encryption keys with `!drop-key <passphrase>`
- Messages encrypted with Fernet (AES-128-CBC + HMAC) at rest on the bridge
- Auto-expire after 72 hours
- CONFIG tab shows pending/delivered status

### LORACLE TRIAGE — Offline Medical Reference

Offline TCCC (Tactical Combat Casualty Care) and field medicine reference. Queryable over mesh or from the dashboard.

- Separate medical knowledge base (doesn't mix with general RAG)
- Optimized for concise, actionable medical guidance
- Ingest TCCC PDFs, wilderness medicine references, trauma protocols
- Every response includes a medical disclaimer
- High-contrast dashboard UI designed for speed under stress

### LORACLE BRIEF — AI-Generated Situation Reports

Watches mesh traffic and generates structured SITREPs (situation reports) using the local LLM. To customize the interval:

```bash
./mesh-llm.sh --brief-interval 30
```

- Aggregates all mesh traffic (messages, commands, alerts)
- Auto-generates SITREPs on a configurable schedule (default: hourly)
- Military SITREP format: SITUATION, KEY ACTIVITY, NODE STATUS, ASSESSMENT
- Export as text or PDF
- Falls back to template-based reports if LLM is unavailable

### LORACLE NAVIGATION — Bearing & Distance

Single-packet navigation helper. From a node with a GPS fix, send a destination coordinate and the bridge replies with the bearing and distance:

```
!nav 34.0522,-118.2437
```

```
NAV
Hdg: 067° ENE
Dist: 1.24 km / 0.77 mi
From: 34.0500,-118.2500
To:   34.0522,-118.2437
GPS age: 2m
```

- Pure Python: Haversine + initial-bearing math, no LLM call, no internet
- Coordinates only (no geocoder), so it stays fully offline
- Reply fits in one LoRa packet — no chunking, no `!more`

All addons load automatically — no flags needed. Just run `./mesh-llm.sh`.

---

## Spatial Features (web dashboard)

### MAP View — Interactive Node Map

Switch to the **MAP** view to see all nodes with GPS fixes on a geographic Leaflet map:

- Node markers with labels, hop count, and battery level in popups
- Your node (self) shown with an orange marker
- Click any marker → popup with coordinates and an **Open** link to the floating window
- Auto-fits bounds to all visible nodes on first load
- Coverage heatmap layer (toggle on/off) showing signal strength across the area
- Tiles served from local cache (`~/.mesh-llm/tiles/`) with OpenStreetMap fallback

### Coverage Data

The bridge logs `(time, node, lat, lon, RSSI, SNR)` for every mesh packet that has both signal info and a known position to `~/.mesh-llm/coverage.jsonl`. This data powers the coverage heatmap layer on the MAP view. API endpoints: `/api/coverage/samples`, `/api/coverage/stats`, `/api/coverage/clear`.

Coverage data persists across restarts. Use the clear endpoint to start fresh after moving locations.

### Device Telemetry

Floating windows show live device metrics from telemetry packets:
- **Battery** level (%) and voltage
- **Temperature** and humidity (if sensors available)
- **Channel utilization** and air time
- **Hardware model**
- Low-battery nodes (≤20%) get a red dot on the canvas

### Device Admin

From CONFIG > CONNECTION (visible when connected):
- **REBOOT** — restart the radio device
- **SHUTDOWN** — power off the radio (requires manual restart)

### Disconnect Alerts

If the radio drops off the USB/TCP/BLE connection, a **red toast alert** appears immediately at the bottom-right of the dashboard: *"⚠ RADIO DISCONNECTED — USB connection lost"*. When it comes back online, you'll see *"✓ Radio reconnected"*. The connection dot in the title bar also flips from green to grey. If the disconnection lasts more than 10 seconds, the CONNECT modal reappears so you can switch to a different transport.

Outbound sends from the dashboard composer now also verify the radio is alive before transmitting — if it isn't, you get an immediate error toast and the message stays in the input so you can retry, instead of silently vanishing. Set `DEBUG_WANT_ACK=1` in the environment to request radio-level ACKs on every send (useful when diagnosing whether a lost message is a software or hardware issue).

### Onboarding Tour

First-time visitors see a **5-minute guided walkthrough** covering all dashboard features. You can replay it any time from **CONFIG > APPEARANCE > LAUNCH TOUR**. Keyboard shortcuts: `←` previous step, `→` next step, `ESC` skip.

### Channel Management

CONFIG > CHANNELS shows all active radio channels with:
- Channel index, name, and role (primary/secondary)
- Encryption status (locked/unlocked icon)
- Uplink/downlink indicators

### Radio Configuration

CONFIG > RADIO lets you read and write LoRa settings directly on the radio:
- **Region** (US, EU_868, EU_433, and 15+ others)
- **Modem preset** (Long Fast, Short Fast, etc.)
- **TX power** (dBm)
- **Max hops** (1-7)

Save writes the config to the radio — it may restart to apply changes.

---

## Auto-Greeter

When a brand-new mesh node first appears (via text packet, position broadcast, or nodeDB sync), the bridge sends them a one-time DM letting them know there's an offline AI assistant on the channel. The default greeting:

```
[LORACLE] Hi! I'm an AI assistant running fully offline on this mesh.
DM me anything to ask — I'll reply in chunks. Send !help for commands.
```

Safety:
- **One-shot per node, persisted forever** in `~/.mesh-llm/greeted_nodes.json` — restarts never re-greet
- **Self-DM-proof** — never DMs the local node id
- **Broadcast-proof** — never DMs `^all`, `!ffffffff`, or non-hex ids
- **First-deployment safe** — on the very first launch with an empty greeted-list, every node already in the radio's nodeDB is silently marked as "known" so a fresh deploy doesn't blast the entire mesh
- **Startup grace period**: no greetings during the first 90 s after the bridge process starts
- **Rate-limited**: at most 1 greeting every 10 s, queued FIFO
- **Single LoRa packet** — fits in one chunk

CLI flags:
- `--no-auto-greet` to disable
- `--greet-message "Custom welcome text"` to override the default

Greeter status (counts, queue length, grace remaining, current message) is visible at `/api/state.greeter`.

### Ask LORACLE from the Dashboard

Use the **AI tab** to chat with the local LLM directly from the dashboard — no radio required. History is isolated from per-node conversations so dashboard chats don't mix with mesh threads. This is the fastest way to query the knowledge base when you're not on the mesh (RAG context is included automatically).

To transmit an AI answer over the mesh, open a node's thread panel from the MESH or MAP view and use the composer there — responses are DM'd to the node and go through the existing `!more` pager for long content.

---

## Public Channel Mode

DMs to the bridge always get a private reply, the same as before. On a public channel, the bridge stays silent during normal chat — it only responds when explicitly addressed.

A public-channel message gets a reply when **any** of these are true:
- It starts with `!` (e.g. `!nav 34,-118`, `!help`, `!triage`, …)
- It contains one of the trigger words anywhere in the first 50 characters: `agent`, `ai`, `oracle`, `loracle`, `bridge`, `help`, `hey` (case-insensitive)

So `hey loracle, what is the capital of france` will trigger a reply on the same channel it came in on. `discussing the weather today` will not.

Safety:
- Real DM detection compares the packet's `toId` against the local `!hex` node id. DMs to other nodes never trip the bridge.
- 8-second per-channel cooldown — a chain of trigger-word messages can't saturate the channel with bot replies.
- Per-sender rate limit (5 s) still applies on top of the channel cooldown.
- Long replies still go through the existing `!more` pager, so a single big response on a public channel is one truncated message + an opt-in continuation, not a flood of chunks.

CLI flags:
- `--no-public-talk` — disable entirely, DM-only
- `--public-talk` — explicit on (this is the default)

---

## How Messages Are Sent

LoRa has a **233-byte message limit**. The bridge sends responses as plain text so they're readable on any Meshtastic device or app.

### Message Flow

When someone sends a question:

1. **"Thinking..."** — the bridge immediately sends a status message so the user knows their question was received and not to resend
2. **AI response** — the actual answer, ending with `[End]` to indicate the response is complete
3. If the response is too long for one message, it's truncated at a sentence boundary with `... (!more)` — send `!more` to get the rest

```
You:   "How do I build a debris shelter?"
AI:    "Thinking..."
AI:    "Find a ridgepole and prop it between a tree and the ground.
        Lean branches along both sides to form an A-frame, then pile
        leaves and debris thickly over the frame... (!more)"
You:   "!more"
AI:    "The debris layer should be 2-3 feet thick for insulation.
        Stuff the interior with dry leaves for bedding. Build it
        just big enough to fit your body to retain heat. [End]"
```

### Why Single Messages?

The Meshtastic app groups messages by sender and only displays the most recent one — sending `[1/3]`, `[2/3]`, `[3/3]` as separate messages would result in only `[3/3]` being visible. The `!more` pager approach lets the user control when they're ready for the next part.

### Responses Are Always DMs

All AI responses are sent as **direct messages (DMs)** back to the person who asked. This means:
- Other people on the mesh don't see AI responses cluttering the public channel
- Each person's conversation with the AI is private
- Anyone on the mesh can ask questions independently

### LoRa Technical Notes

- **Half-duplex radio**: LoRa radios can only transmit OR receive at any given time, never both. The bridge sends with `wantAck=False` so the radio transmits once (~1-2 seconds) and immediately returns to receive mode, minimizing the window where incoming messages could be missed.
- **Conversation history**: The bridge maintains per-node conversation history (up to 10 messages) so follow-up questions have context. History is automatically cleared after 1 hour of inactivity.
- **Deduplication**: Mesh relay can cause duplicate messages. The bridge uses a content hash + 5-minute TTL cache to filter these out.
- **Rate limiting**: A 5-second cooldown per node prevents message flooding during inference.

---

## Architecture Diagram

```
┌────────────────────────────────────────────────────────────────┐
│                        YOUR COMPUTER                           │
│                                                                │
│  ┌─────────────────┐     ┌──────────────────────────────┐     │
│  │   Ollama         │     │   LORACLE BRIDGE              │     │
│  │   (local AI)     │◄───►│   standalone_bridge.py        │     │
│  │                  │     │                               │     │
│  │  gemma3:4b       │     │  ├── Dashboard (:8000)        │     │
│  └─────────────────┘     │  ├── RAG Engine                │     │
│                           │  ├── Addons (DeadDrop/Triage/ │     │
│                           │  │           Brief/Nav)        │     │
│                           │  └── Bridge Relay (v2)         │     │
│                           │      ├─ Policy + Dedup         │     │
│                           │      ├─ Urgency classifier     │     │
│                           │      ├─ Rate limiter           │     │
│                           │      └─ Audit log (SQLite)     │     │
│                           └──────┬────────────────┬────────┘     │
│                                  │ USB/TCP/BLE    │ USB/TCP/BLE │
└──────────────────────────────────┼────────────────┼─────────────┘
                                   │                │
                          ┌────────┴────────┐  ┌────┴──────────┐
                          │  Meshtastic     │  │  MeshCore     │  ← v2: --second-radio
                          │  (primary)      │  │  (secondary)  │
                          └────────┬────────┘  └──────┬────────┘
                                   │ LoRa             │ LoRa
                 ┌─────────────────┤                  ├────────────────┐
                 │                 │                  │                │
          ┌──────┴────┐    ┌──────┴────┐       ┌─────┴─────┐  ┌──────┴─────┐
          │  Node A   │    │  Node B   │       │ MC Node X │  │ MC Node Y  │
          │  (hiker)  │    │  (camp)   │       │  (fireteam)│  │  (ops ctr) │
          └───────────┘    └───────────┘       └───────────┘  └────────────┘

                          ↕ Bridge relays between networks
                          (off by default; per-channel rules in BRIDGE tab)
```

---

## Project Structure

```
LORACLE-BRIDGE/
│
├── LORACLE BRIDGE.command       # DOUBLE-CLICK TO LAUNCH (macOS)
├── mesh-llm.sh                  # Or run this from the terminal
│
├── README.md                    # This file
├── CONTEXT.md                   # Project-wide change history (dated entries)
├── LORACLE_BRIDGE_V2_FSD.md     # v2 roadmap, decisions log, phase progress
├── CONTRIBUTING.md
├── LICENSE
│
├── CONTEXT FILES/               # Drop PDFs, text files here for the knowledge base
│
├── meshtastic-bridge/           # Python source code
│   ├── standalone_bridge.py     # Main bridge — radio, message routing, LLM, v2 bridge wiring
│   ├── ollama_client.py         # Talks to Ollama's REST API for AI responses
│   ├── protocol.py              # LoRa chunking protocol
│   ├── dashboard.py             # Web control panel + dynamic addon tab injection + BRIDGE tab
│   ├── manage_docs.py           # Document management CLI (ingest, list, stats)
│   ├── coverage_logger.py       # Append-only JSONL of per-packet signal samples
│   ├── greeter.py               # Auto-greeter service
│   ├── requirements.txt         # Python dependencies
│   │
│   ├── bridge/                  # v2: cross-protocol relay (Phases 2–5)
│   │   ├── relay.py             # Relay.observe() — entry point for cross-protocol relay
│   │   ├── policy.py            # Disabled/Always/ChannelAllowlist/AIGated policies
│   │   ├── config.py            # JSON config schema + build_policy(cfg) composer
│   │   ├── dedup.py             # RelayDedupCache — TTL fingerprint cache
│   │   ├── identity.py          # [mt-Alice] prefix formatter + loop-guard recogniser
│   │   ├── urgency.py           # HeuristicUrgencyClassifier for ai-gated policy
│   │   └── rate_limit.py        # RelayRateLimiter — per-direction sliding window
│   │
│   ├── radio/                   # v2: multi-protocol abstraction layer
│   │   ├── events.py            # Protocol/Transport enums, UnifiedMessage, UnifiedNode
│   │   ├── backend.py           # RadioBackend abstract interface
│   │   ├── manager.py           # RadioManager — multiplex multiple backends
│   │   ├── meshtastic_backend.py # Wraps meshtastic-python library
│   │   └── meshcore_backend.py  # Wraps meshcore Python library (Python 3.10+)
│   │
│   ├── routing/                 # LLM model-tier routing (tiny/std/big)
│   │   ├── classifier.py        # Hybrid length+keyword tier classifier
│   │   └── tiers.py             # Tier enum + TierConfig + load_tiers
│   │
│   ├── db/                      # SQLite schema + per-table stores
│   │   ├── schema.py            # CREATE TABLE DDL + init_db + settings migration
│   │   ├── contacts.py          # ContactStore — node/channel contact rows
│   │   ├── messages.py          # MessageStore — DM + channel message history
│   │   ├── settings.py          # SettingsStore — key/value (bridge config lives here)
│   │   └── bridge_events.py     # v2: persistent audit log of relay decisions
│   │
│   ├── rag/                     # Knowledge base subsystem
│   │   ├── engine.py            # SQLite + NumPy vector store with cosine similarity
│   │   ├── extractors.py        # Extracts text from PDFs, ZIM archives, HTML, text
│   │   └── chunker.py           # Splits text into overlapping chunks for embedding
│   │
│   ├── addons/                  # Pluggable addon system
│   │   ├── base.py              # Addon base class (+ v2: on_bridged_message hook)
│   │   ├── dead_drop/           # Encrypted async messaging (Fernet)
│   │   ├── triage/              # Offline TCCC medical reference
│   │   ├── brief/               # AI-generated SITREPs
│   │   └── navigation/          # Bearing/distance helper (pure Haversine, no LLM)
│   │
│   ├── packs/                   # Knowledge-base starter packs
│   │   ├── registry.py          # List available packs
│   │   ├── installer.py         # Download + ingest pack documents
│   │   ├── fetcher.py           # HTTP fetch with zero-byte detection
│   │   ├── manifest.py          # PackManifest / PackDocument dataclasses
│   │   └── bundled/             # Shipped packs (e.g. emergency-preparedness)
│   │
│   ├── static/                  # Dashboard static assets (favicon, tiles)
│   │
│   └── tests/                   # Unit + integration tests (300+ tests)
│       ├── test_bridge_*.py     # v2 bridge: identity / dedup / policy / relay / urgency /
│       │                        # force_relay / rate_limit / events_store / integration
│       ├── test_radio_backends.py
│       ├── test_db.py
│       ├── test_standalone_bridge.py
│       ├── test_dashboard_api.py
│       ├── test_addons_*.py
│       ├── test_protocol.py
│       ├── test_ollama_client.py
│       ├── test_classifier.py
│       ├── test_coverage_logger.py
│       ├── test_greeter.py
│       └── test_security.py
```

---

## Troubleshooting

### "No Meshtastic device found"
- Make sure your radio is plugged in via USB-C
- Try a different USB cable (some are charge-only, no data)
- Check that your radio is powered on
- Run `ls /dev/cu.usb*` to see if the system detects the device

### "Ollama not responding"
- The script auto-starts Ollama, but if it fails: run `ollama serve` in a separate terminal
- Check if it's running: `curl http://localhost:11434/api/tags`

### "Model not found" errors
- The bridge auto-resolves model names (e.g., `gemma3` matches `gemma3:4b`)
- If issues persist: `ollama pull gemma3:4b` to install the default model

### "BLE mode requires Python 3.11+"
- The script auto-installs Python 3.12 for BLE mode
- If it still fails, try: `brew install python@3.12` then re-run

### Protobuf parsing errors
- These are suppressed automatically but may indicate a firmware/library version mismatch
- Check the System Log section in the dashboard for firmware vs library version info
- The bridge continues to work despite these warnings

### Dashboard not loading
- Default: `http://localhost:8000`
- If port is in use: `./mesh-llm.sh --dashboard-port 9000`

### Responses are too slow
- The default `gemma3:4b` is a good balance of quality and speed
- For faster but lower quality: `./mesh-llm.sh --model llama3.2:1b`

### Response was cut off
- Send `!more` to get the next page of a long response
- The bridge automatically truncates at sentence boundaries to fit the 233-byte LoRa limit

### Follow-up messages not received
- LoRa is half-duplex — the radio can't receive while transmitting. Wait a few seconds after getting a response before sending your next message
- Check the dashboard Debug tab for "Rate-limited" or "Duplicate" log entries

---

## License

LORACLE BRIDGE is licensed under the [Apache License 2.0](LICENSE).
