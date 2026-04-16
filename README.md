<div align="center">

# LORACLE BRIDGE
### Offline AI Over Mesh Radio

**Chat with a local LLM over LoRa — no internet required**

</div>

---

## What Is This?

This project turns a **Meshtastic LoRa radio** into an AI-powered chatbot. Anyone on your mesh network can send a text message to your radio, and it automatically responds with answers from a local LLM running on your computer via **Ollama**.

**No internet. No cloud. No API keys.** Everything runs locally on your machine.

```
Someone on the mesh types a question
  → Their radio sends it over LoRa
  → Your radio receives it via USB
  → This bridge forwards it to Ollama (AI running on your computer)
  → AI generates a response
  → Bridge sends plain-text response back over the mesh
  → They get an answer
```

You can also load documents (PDFs, text files, survival manuals, field guides) into a **knowledge base** so the AI can answer questions grounded in your own reference material. The knowledge base is **enabled by default** — just drop files into the `CONTEXT FILES/` folder.

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

### Step 1: Clone This Repo

```bash
git clone https://github.com/treyj333/loracle.git
cd loracle
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

### Dashboard Tabs

| Tab | What It Does |
|-----|-------------|
| **LIVE** | Animated mesh header, stat strip (messages, nodes, reply time, docs), schematic mesh map with peer nodes, unified message feed with direction filters and search, composer bar for direct LLM chat, collapsible system log viewer, and coverage heatmap section |
| **CONFIG** | Connection management (USB/TCP/BLE), model switching, system prompt, response settings, RAG knowledge base (toggle, URL ingest, file upload, document list), history, geographic Leaflet map, appearance (light/dark theme), about, and addon sections (Dead Drop, Triage, Brief) |

### Composer Bar

The LIVE tab includes a **composer bar** at the bottom where you can type messages and get AI responses directly — no radio needed. This is useful for testing the LLM, tuning the system prompt, and verifying the knowledge base works before deploying over the mesh.

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
| **MeshCore** | `meshcore>=2.2.1` | MeshCore companion devices | DM support (serial, TCP, BLE). Requires Python 3.10+. |

You can run **both simultaneously** with `--second-radio`:

```bash
./mesh-llm.sh --second-radio meshcore:serial:/dev/ttyUSB1
```

New CLI flags:
- `--protocol <auto|meshtastic|meshcore>` — override protocol detection for the primary radio
- `--second-radio protocol:transport:params` — connect a second radio
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

---

## All Command-Line Options

```
./mesh-llm.sh [OPTIONS]

Connection (default: USB serial, auto-detected):
  --serial <port>         Serial port for radio (default: auto-detect USB)
  --tcp <host:port>       TCP address for radio (e.g. 192.168.1.1:4403)
  --ble [address]         Connect via Bluetooth LE (scan if no address given)

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

Addons (all enabled by default):
  --enable-dead-drop      Enable Dead Drop (default: on)
  --enable-triage         Enable Triage (default: on)
  --triage-dir <path>     Triage medical KB directory (default: ~/.mesh-llm/triage)
  --enable-brief          Enable Brief (default: on)
  --brief-interval <int>  SITREP generation interval in minutes (default: 60)
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

Beyond addons, the dashboard exposes two map-based features built on the radio's GPS-aware traffic:

### Live Node Map (CONFIG > Geographic Map)
- Bright pulsing markers for every node with a known GPS fix
- Marker label shows a short node id and **hop count** (e.g. `ac12f3 · 2h`)
- Click any node → popup with lat/lon, hop count, age, and a **DM this node** button that pre-fills the Send Message form so you can reply directly
- Stale fixes (no update >10 min) flip to amber

### Coverage Heatmap (LIVE > Coverage)
The bridge logs `(time, node, lat, lon, RSSI, SNR)` for every mesh packet that has both signal info and a known position to `~/.mesh-llm/coverage.jsonl`. The Coverage tab visualizes that data:

- **Heatmap** mode (default): Leaflet.heat with a high-contrast green→red gradient
- **Grid** mode: solid 40 m × 40 m colored rectangles by best RSSI per cell — crisp at any zoom
- **Both** mode: stacked layers
- **Dead zones** toggle: highlights cells where nodes traveled but signal was poor or missing
- Time window filter: last hour / 6h / 24h / all-time
- Min-RSSI slider, persistent legend
- **Auto-refreshes every ~10 s** while the Coverage section is open — no need to click Refresh manually
- **Disconnected banner** appears whenever the bridge has no radio attached, warning that the data on screen is from the last connected session and not live
- **Clear log** button (red, in the toolbar) wipes `~/.mesh-llm/coverage.jsonl` and resets the in-memory throttle so the next sample logs immediately. Use this when you want to start fresh after moving locations or changing antennas

Coverage data starts populating as soon as the bridge is connected and packets are flowing. Samples are persisted to `~/.mesh-llm/coverage.jsonl` and survive bridge restarts — so opening the tab on a disconnected bridge will show data from the last session (the disconnected banner makes this explicit). Use **Clear log** to reset. Useful for finding antenna sweet spots and identifying mesh dead spots before a patrol.

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

### Manual Broadcast: Welcome → Public

The dashboard's LIVE tab has a **Welcome → Public** button next to the Send button. Click it and the Send Message form pre-fills with the current greeter text, the recipient drops to **Broadcast**, and the channel is set to **Ch 0**. Review the text, then click **Send** to broadcast it. It does **not** auto-send — accidental clicks won't spam the channel.

### Ask LORACLE from the Dashboard

The LIVE tab composer has a **Mode** selector with two options:

- **Raw send** (default) — whatever you type is broadcast or DM'd as-is over the mesh, exactly like before. LORACLE does **not** see it (the bridge's own radio doesn't hear its own transmissions), so don't use this mode to ask questions.
- **Ask LORACLE** — the text becomes a question to the local LLM. The answer is shown in the message log *and* (optionally) transmitted over the mesh.

In Ask mode the recipient dropdown gains a **Local only (don't transmit)** option at the top:
- **Local only** — the answer appears only in the dashboard. Nothing goes out on the radio.
- **Broadcast** — the answer is broadcast on the selected channel (e.g. Ch 0) so everyone on the channel gets it.
- **A specific node** — the answer is DM'd to that node (useful for relaying a Q&A to one person). Long answers still go through the existing `!more` pager, so the target can page through the continuation.

Commands (`!nav`, `!help`, `!triage`, `!brief`, etc.) work from Ask mode too — they're dispatched through the same handler the mesh path uses. Ollama history for dashboard chats lives in an isolated `"!dashboard"` namespace so it never mixes with real-node conversations.

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
┌──────────────────────────────────────────────────────────┐
│                     YOUR COMPUTER                        │
│                                                          │
│  ┌─────────────────┐     ┌──────────────────────────┐   │
│  │   Ollama         │     │   LORACLE BRIDGE         │   │
│  │   (local AI)     │◄───►│   standalone_bridge.py   │   │
│  │                  │     │                          │   │
│  │  gemma3:4b       │     │  ┌── Dashboard (:8000)   │   │
│  └─────────────────┘     │  ├── RAG Engine           │   │
│                           │  └── Chat Panel           │   │
│                           └────────────┬─────────────┘   │
│                                        │ USB / TCP / BLE │
└────────────────────────────────────────┼─────────────────┘
                                         │
                                ┌────────┴────────┐
                                │  Your Meshtastic │
                                │     Radio        │
                                └────────┬────────┘
                                         │ LoRa
                          ┌──────────────┼──────────────┐
                          │              │              │
                    ┌─────┴─────┐  ┌─────┴─────┐  ┌────┴──────┐
                    │  Radio A  │  │  Radio B  │  │  Radio C  │
                    │  (hiker)  │  │  (camp)   │  │  (truck)  │
                    └───────────┘  └───────────┘  └───────────┘
```

---

## Project Structure

```
loracle/
│
├── LORACLE BRIDGE.command       # DOUBLE-CLICK TO LAUNCH (macOS)
├── mesh-llm.sh                  # Or run this from the terminal
│
├── CONTEXT FILES/               # Drop PDFs, text files here for the knowledge base
│
├── meshtastic-bridge/           # Python source code
│   ├── standalone_bridge.py     # Main bridge — radio connection, message routing, LLM calls
│   ├── ollama_client.py         # Talks to Ollama's REST API for AI responses
│   ├── protocol.py              # LoRa chunking protocol
│   ├── dashboard.py             # Web control panel with dynamic addon tab injection
│   ├── manage_docs.py           # Document management CLI (ingest, list, stats)
│   ├── requirements.txt         # Python dependencies
│   ├── rag/                     # Knowledge base subsystem
│   │   ├── engine.py            # SQLite + NumPy vector store with cosine similarity search
│   │   ├── extractors.py        # Extracts text from PDFs, ZIM archives, and text files
│   │   └── chunker.py           # Splits text into overlapping chunks for embedding
│   ├── addons/                  # Pluggable addon system
│   │   ├── base.py              # Base Addon class — interface for all addons
│   │   ├── dead_drop/           # LORACLE DEAD DROP — encrypted async messaging
│   │   │   ├── addon.py         # Command handlers, lifecycle hooks
│   │   │   ├── store.py         # SQLite message queue (pending/delivered/expired)
│   │   │   ├── crypto.py        # Fernet encryption (AES-128-CBC + HMAC)
│   │   │   └── dashboard.py     # 2-tab dashboard (LIVE/CONFIG) HTML/JS + API routes
│   │   ├── triage/              # LORACLE TRIAGE — offline medical reference
│   │   │   ├── addon.py         # Medical query handler with separate RAG instance
│   │   │   ├── prompts.py       # TCCC-optimized system prompts
│   │   │   └── dashboard.py     # High-contrast medical reference UI
│   │   └── brief/               # LORACLE BRIEF — AI-generated SITREPs
│   │       ├── addon.py         # Traffic observer, SITREP scheduler
│   │       ├── aggregator.py    # SQLite traffic event collector
│   │       ├── generator.py     # LLM-powered SITREP generation
│   │       ├── exporter.py      # Text + PDF export
│   │       └── dashboard.py     # SITREP display, history, export controls
│   └── tests/                   # Unit tests
│       ├── test_protocol.py     # Protocol chunking/reassembly tests
│       └── test_ollama_client.py
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
