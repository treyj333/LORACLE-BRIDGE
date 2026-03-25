<div align="center">
<img src="https://raw.githubusercontent.com/Crosstalk-Solutions/project-nomad/refs/heads/main/admin/public/project_nomad_logo.png" width="200" height="200"/>

# Project N.O.M.A.D. + Meshtastic
### Offline AI Over Mesh Radio

**Chat with a local LLM over LoRa — no internet required**

[![Website](https://img.shields.io/badge/Website-projectnomad.us-blue)](https://www.projectnomad.us)
[![Discord](https://img.shields.io/badge/Discord-Join%20Community-5865F2)](https://discord.com/invite/crosstalksolutions)

</div>

---

## What Is This?

This project turns a **Meshtastic LoRa radio** into an AI-powered chatbot. Anyone on your mesh network can send a text message to your radio, and it automatically responds with answers from a local LLM (large language model) running on your computer via **Ollama**.

**No internet. No cloud. No API keys.** Everything runs locally on your machine.

```
Someone on the mesh types a question
  → Their radio sends it over LoRa
  → Your radio receives it via USB
  → This bridge forwards it to Ollama (AI running on your Mac)
  → AI generates a response
  → Bridge sends the response back over the mesh
  → They get an answer
```

You can also load documents (PDFs, text files, survival manuals, field guides) into a **knowledge base**, so the AI can answer questions grounded in your own reference material.

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
| **Python 3.9+** | Runs the bridge code | Auto-installed via Homebrew if missing |
| **Ollama** | Runs AI models locally on your machine — no cloud needed | Auto-installed via Homebrew (macOS) or install script (Linux) |
| **llama3.2** | The default AI model (small, fast, runs on most hardware) | Auto-pulled by Ollama on first run |

---

## Quick Start — 3 Steps

### Step 1: Clone This Repo

```bash
git clone https://github.com/YOUR_USERNAME/project-meshtastic-llm.git
cd project-meshtastic-llm
```

### Step 2: Plug In Your Radio

Connect your Meshtastic radio to your computer with a USB-C cable. That's it — the bridge auto-detects it.

### Step 3: Run It

```bash
./mesh-llm.sh
```

**That's the whole setup.** On first run, the script will:

1. Install Homebrew (if you don't have it)
2. Install Python (if you don't have it or it's too old)
3. Install Ollama (if you don't have it)
4. Start the Ollama service
5. Download the `llama3.2` AI model (if no models are installed)
6. Create a Python virtual environment and install dependencies
7. Auto-detect your radio on USB
8. Start listening for messages and responding with AI

You'll see output like this:

```
==========================================
  Meshtastic LLM Bridge
==========================================

OK Python 3.12.13
OK Ollama installed
OK Ollama running
OK 2 model(s) available
OK Python environment ready

==========================================
  Bridge starting...
  Connection: USB Serial (auto-detect)
  Dashboard: http://localhost:8000
  Press Ctrl+C to stop
==========================================
```

Now anyone on your mesh network can send a text message and get an AI response back.

Press **Ctrl+C** to stop.

---

## Connection Methods Explained

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

## Web Dashboard

The bridge includes a built-in web dashboard that starts automatically. Open it in your browser:

```
http://localhost:8000
```

**What it shows:**
- Connection status (green = connected, red = disconnected)
- Which AI model is loaded
- Bridge uptime
- Total messages sent/received
- List of known mesh nodes
- Recent message history (last 100 messages) with timestamps, node IDs, and response times

The dashboard updates every 2 seconds. To change the port:

```bash
./mesh-llm.sh --dashboard-port 9000
```

---

## Knowledge Base (RAG)

RAG (Retrieval-Augmented Generation) lets you load your own documents so the AI can answer questions using your reference material. Think field manuals, survival guides, technical docs, or any PDF/text file.

### How It Works

1. You drop files into the `CONTEXT FILES/` folder
2. The bridge breaks them into small chunks and creates searchable embeddings
3. When someone asks a question on the mesh, the bridge searches your documents for relevant info
4. That context is injected into the AI prompt so the answer is grounded in your actual documents

### Setting It Up

**Step 1:** Put your files in the `CONTEXT FILES/` folder in the project root:

```
project-meshtastic-llm/
  CONTEXT FILES/
    ranger-handbook.pdf
    survival-guide.txt
    medical-reference.pdf
```

**Step 2:** Run with `--rag` enabled:

```bash
./mesh-llm.sh --rag
```

On first run with `--rag`, the script will:
1. Pull the embedding model (`nomic-embed-text`) — this converts text into searchable vectors
2. Auto-ingest all files from `CONTEXT FILES/` (skips files already ingested)
3. Start the bridge with knowledge base search enabled

**Supported file types:** `.pdf`, `.zim`, `.txt`, `.md`

### Managing Documents

```bash
./mesh-llm.sh --docs                    # List all ingested documents
./mesh-llm.sh --docs-stats              # Show knowledge base statistics
./mesh-llm.sh --ingest path/to/file.pdf # Manually ingest a single file
```

### How Search Works Under the Hood

- Documents are split into ~5,100 character chunks with 450 character overlap
- Each chunk is embedded into a 768-dimensional vector using `nomic-embed-text`
- Queries are embedded the same way, then compared using cosine similarity
- Top 5 most relevant chunks are injected into the AI prompt (if they score above 0.3 similarity)
- Total injected context is capped at 2,000 characters to fit within LoRa bandwidth constraints

---

## Mesh Commands

Anyone on the mesh network can send these special commands (prefix with `!`):

| Command | What It Does |
|---------|-------------|
| `!help` | Shows the list of available commands |
| `!status` | Shows bridge info: which model, uptime, node count, message count, RAG stats |
| `!model <name>` | Switches the AI model (e.g., `!model mistral`) |
| `!models` | Lists all installed Ollama models |
| `!clear` | Resets conversation history for your node (starts fresh) |
| `!ping` | Simple connectivity test — confirms the bridge is alive |
| `!rag on/off` | Toggles knowledge base search on or off for your node |
| `!docs` | Lists all ingested documents in the knowledge base |

---

## All Command-Line Options

```
./mesh-llm.sh [OPTIONS]

Connection (default: USB serial, auto-detected):
  --serial <port>         Serial port for radio (default: auto-detect USB)
  --tcp <host:port>       TCP address for radio (e.g. 192.168.1.1:4403)
  --ble [address]         Connect via Bluetooth LE (scan if no address given)

Model:
  --model <name>          Ollama model to use (default: llama3.2)
  --ollama-url <url>      Ollama API URL (default: http://localhost:11434)
  --list-models           List available Ollama models and exit

Response Tuning:
  --max-length <int>      Max response characters (default: 500)
  --system-prompt <text>  Custom system prompt for the AI
  --no-compression        Disable zlib compression on chunks
  --chunk-delay <secs>    Delay between LoRa chunks (default: 2.0 seconds)

Knowledge Base:
  --rag                   Enable document-grounded responses
  --rag-dir <path>        RAG storage directory (default: ~/.mesh-llm/rag)
  --ingest <file|dir>     Ingest a file or directory into the knowledge base
  --docs                  List ingested documents
  --docs-stats            Show knowledge base statistics

Other:
  --dashboard-port <int>  Web dashboard port (default: 8000)
  --help                  Show help and exit
```

### Examples

```bash
# Basic — plug in radio, run with defaults
./mesh-llm.sh

# Use a different AI model
./mesh-llm.sh --model mistral

# Connect to radio over TCP (WiFi AP mode)
./mesh-llm.sh --tcp

# Enable knowledge base with custom model and longer responses
./mesh-llm.sh --rag --model llama3.2 --max-length 800

# Custom system prompt for a specific use case
./mesh-llm.sh --system-prompt "You are a wilderness survival expert. Be concise."

# List what models you have installed
./mesh-llm.sh --list-models
```

---

## How the Message Protocol Works

LoRa has a **228-byte message limit**. AI responses are usually much longer than that, so the bridge splits them into chunks:

| Field | Size | Purpose |
|-------|------|---------|
| Message ID | 2 bytes | Unique ID to group chunks together |
| Sequence | 1 byte | Chunk number (0, 1, 2...) |
| Total | 1 byte | Total chunks in this message |
| Flags | 1 byte | Compression enabled, final chunk, error flag |
| Reserved | 3 bytes | Future use |
| **Payload** | **220 bytes** | The actual text content |

**Compression:** Enabled by default. The bridge tries zlib compression — if it reduces the number of chunks, it uses the compressed version. Otherwise it sends uncompressed.

**Chunk delay:** 2 seconds between chunks by default. This gives the LoRa network time to transmit each one without collisions. Adjust with `--chunk-delay`.

---

## Architecture Diagram

```
┌──────────────────────────────────────────────────────────┐
│                     YOUR COMPUTER                        │
│                                                          │
│  ┌─────────────────┐     ┌──────────────────────────┐   │
│  │   Ollama         │     │   Meshtastic LLM Bridge  │   │
│  │   (local AI)     │◄───►│   standalone_bridge.py   │   │
│  │                  │     │                          │   │
│  │  llama3.2 model  │     │  ┌── Dashboard (:8000)   │   │
│  └─────────────────┘     │  ├── RAG Engine           │   │
│                           │  └── Chunk Protocol       │   │
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
project-meshtastic-llm/
│
├── mesh-llm.sh                  # THE ONE COMMAND — run this to start everything
│
├── CONTEXT FILES/               # Drop PDFs, text files here for the knowledge base
│
├── meshtastic-bridge/           # Python source code
│   ├── standalone_bridge.py     # Main bridge — radio connection, message routing, LLM calls
│   ├── ollama_client.py         # Talks to Ollama's REST API for AI responses
│   ├── protocol.py              # LoRa chunking protocol (228-byte limit handling)
│   ├── dashboard.py             # Web dashboard (Flask, auto-starts on port 8000)
│   ├── manage_docs.py           # Document management CLI (ingest, list, stats)
│   ├── config.py                # Default configuration values
│   ├── requirements.txt         # Python dependencies
│   ├── rag/                     # Knowledge base subsystem
│   │   ├── engine.py            # SQLite + NumPy vector store with cosine similarity search
│   │   ├── extractors.py        # Extracts text from PDFs, ZIM archives, and text files
│   │   └── chunker.py           # Splits text into overlapping chunks for embedding
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

### "BLE mode requires Python 3.11+"
- The script auto-installs Python 3.12 for BLE mode
- If it still fails, try: `brew install python@3.12` then re-run

### "Could not build wheels for pyobjc"
- This happens with older Python versions. The script now auto-upgrades Python when BLE is requested
- If persists: `rm -rf venv && ./mesh-llm.sh --ble` to rebuild from scratch

### Dashboard not loading
- Default: `http://localhost:8000`
- If port is in use: `./mesh-llm.sh --dashboard-port 9000`

### Responses are too slow
- Try a smaller model: `./mesh-llm.sh --model llama3.2:1b`
- Reduce max length: `./mesh-llm.sh --max-length 300`

### Chunks arriving out of order
- Increase chunk delay: `./mesh-llm.sh --chunk-delay 4.0`
- This gives the LoRa network more time between transmissions

---

## License

Project N.O.M.A.D. is licensed under the [Apache License 2.0](LICENSE).
