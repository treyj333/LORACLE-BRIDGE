#!/usr/bin/env bash
#
# mesh-llm.sh — One-command LORACLE BRIDGE
#
# Connects a Meshtastic radio to a local Ollama LLM.
# Connects a Meshtastic radio to a local Ollama LLM.
# Automatically installs all prerequisites on first run.
#
# Usage:
#   ./mesh-llm.sh                                    # Auto-detect radio, default model
#   ./mesh-llm.sh --ble                              # Connect via Bluetooth LE
#   ./mesh-llm.sh --model mistral                    # Use a specific model
#   ./mesh-llm.sh --serial /dev/cu.usbserial-0001    # Specify serial port
#   ./mesh-llm.sh --tcp 192.168.1.100:4403           # Use TCP connection
#   ./mesh-llm.sh --list-models                      # List available Ollama models
#
set -eo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
BRIDGE_DIR="$PROJECT_ROOT/meshtastic-bridge"

# ─── Parse arguments ─────────────────────────────────────────────────────────

OLLAMA_URL="http://localhost:11434"
MODEL=""
BRIDGE_ARGS=()
LIST_MODELS=false
RAG_ENABLED=true
INGEST_PATH=""
BLE_MODE=false
DASHBOARD_PORT=8000

for arg in "$@"; do
  case "$arg" in
    --list-models) LIST_MODELS=true ;;
    --rag) RAG_ENABLED=true ;;
    --no-rag) RAG_ENABLED=false ;;
    --ble) BLE_MODE=true ;;
    --help|-h)
      echo "LORACLE BRIDGE — Chat with a local AI over mesh radio"
      echo ""
      echo "Usage: ./mesh-llm.sh [OPTIONS]"
      echo ""
      echo "Connection (default: USB serial, auto-detected):"
      echo "  --serial <port>         Serial port for radio (default: auto-detect USB)"
      echo "  --tcp <host:port>       TCP address for radio (e.g. 192.168.1.1:4403)"
      echo "  --ble [address]         Connect via Bluetooth LE (scan if no address given)"
      echo ""
      echo "Options:"
      echo "  --model <name>          Ollama model (default: auto-select based on RAM)"
      echo "  --ollama-url <url>      Ollama URL (default: http://localhost:11434)"
      echo "  --max-length <int>      Max response chars (default: 200)"
      echo "  --system-prompt <text>  Custom system prompt"
      echo "  --no-compression        Disable chunk compression"
      echo "  --chunk-delay <secs>    Delay between chunks (default: 2.0)"
      echo "  --dashboard-port <int>  Web dashboard port (default: 8000)"
      echo "  --list-models           List available Ollama models and exit"
      echo "  --no-rag                Disable knowledge base (RAG is on by default)"
      echo "  --rag-dir <path>        RAG data directory (default: ~/.mesh-llm/rag)"
      echo "  --help                  Show this help"
      echo ""
      echo "Document Management:"
      echo "  ./mesh-llm.sh --ingest <file>    Ingest a PDF, ZIM, or text file"
      echo "  ./mesh-llm.sh --docs             List ingested documents"
      echo "  ./mesh-llm.sh --docs-stats       Show knowledge base stats"
      echo ""
      echo "Everything is installed automatically on first run (Homebrew, Python, Ollama)."
      exit 0
      ;;
    --ingest) INGEST_PATH="__next__" ;;
    --docs) INGEST_PATH="__list__" ;;
    --docs-stats) INGEST_PATH="__stats__" ;;
  esac
  # Capture the path after --ingest
  if [ "$INGEST_PATH" = "__next__" ] && [ "$arg" != "--ingest" ]; then
    INGEST_PATH="$arg"
  fi
done

# Forward all args to standalone_bridge.py
BRIDGE_ARGS=("$@")

# Track what we started for cleanup
STARTED_OLLAMA=false
declare -a PIDS

cleanup() {
  echo ""
  echo "Shutting down..."
  if [ ${#PIDS[@]} -gt 0 ]; then
    for pid in "${PIDS[@]}"; do
      kill "$pid" 2>/dev/null || true
    done
  fi
  if [ "$STARTED_OLLAMA" = true ]; then
    echo "Stopping Ollama (we started it)..."
    pkill -f "ollama serve" 2>/dev/null || true
  fi
  echo "Done."
  exit 0
}

trap cleanup EXIT INT TERM

# ─── Banner ──────────────────────────────────────────────────────────────────

echo ""
echo "=========================================="
echo "  LORACLE BRIDGE"
echo "=========================================="
echo ""

# ─── 0. Ensure Homebrew is available (macOS) ─────────────────────────────────

if [[ "$(uname)" == "Darwin" ]] && ! command -v brew &>/dev/null; then
  echo "Installing Homebrew (required for dependencies)..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # Add Homebrew to PATH for this session
  if [ -x "/opt/homebrew/bin/brew" ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [ -x "/usr/local/bin/brew" ]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
  echo "OK Homebrew installed"
fi

# ─── 1. Check/Install Python ────────────────────────────────────────────────

find_python() {
  local py="$1"
  if command -v "$py" &>/dev/null; then
    local ver
    ver=$("$py" --version 2>&1 | cut -d' ' -f2)
    local minor
    minor=$(echo "$ver" | cut -d. -f2)
    echo "$py:$ver:$minor"
  fi
}

detect_best_python() {
  PYTHON_CANDIDATES=()
  # Check versioned Homebrew Python paths (brew install python@3.12 uses these)
  for ver in 3.13 3.12 3.11; do
    if [ -x "/opt/homebrew/bin/python$ver" ]; then
      PYTHON_CANDIDATES+=("/opt/homebrew/bin/python$ver")
    elif [ -x "/usr/local/bin/python$ver" ]; then
      PYTHON_CANDIDATES+=("/usr/local/bin/python$ver")
    fi
  done
  # Also check unversioned paths
  if [ -x "/opt/homebrew/bin/python3" ]; then
    PYTHON_CANDIDATES+=("/opt/homebrew/bin/python3")
  elif [ -x "/usr/local/bin/python3" ]; then
    PYTHON_CANDIDATES+=("/usr/local/bin/python3")
  fi
  PYTHON_CANDIDATES+=("python3")

  BEST_PYTHON=""
  BEST_VER=""
  BEST_MINOR=0

  for candidate in "${PYTHON_CANDIDATES[@]}"; do
    result=$(find_python "$candidate" 2>/dev/null || true)
    if [ -n "$result" ]; then
      py_path=$(echo "$result" | cut -d: -f1)
      py_ver=$(echo "$result" | cut -d: -f2)
      py_minor=$(echo "$result" | cut -d: -f3)
      if [ "$py_minor" -gt "$BEST_MINOR" ] 2>/dev/null; then
        BEST_PYTHON="$py_path"
        BEST_VER="$py_ver"
        BEST_MINOR="$py_minor"
      fi
    fi
  done
}

detect_best_python

# Auto-install Python 3.12 if none found or too old for BLE
if [ -z "$BEST_PYTHON" ]; then
  echo "Python 3 not found. Installing Python 3.12..."
  if command -v brew &>/dev/null; then
    brew install python@3.12
  else
    echo "Cannot auto-install Python (no Homebrew on Linux)."
    echo "Install manually: sudo apt install python3 python3-venv"
    exit 1
  fi
  detect_best_python
fi

# BLE requires Python 3.11+ (for pyobjc/bleak to build on macOS)
if [ "$BLE_MODE" = true ] && [ "$BEST_MINOR" -lt 11 ]; then
  echo "BLE mode requires Python 3.11+ (found $BEST_VER). Installing Python 3.12..."
  if command -v brew &>/dev/null; then
    brew install python@3.12
  else
    echo "Cannot auto-install Python 3.12 (no Homebrew)."
    exit 1
  fi
  detect_best_python

  if [ "$BEST_MINOR" -lt 11 ]; then
    echo "Python 3.11+ still not available after install. Check your PATH."
    exit 1
  fi
fi

PYTHON="$BEST_PYTHON"
echo "OK Python $BEST_VER ($PYTHON)"

# ─── 2. Check/Install Ollama ────────────────────────────────────────────────

if ! command -v ollama &>/dev/null; then
  echo "Ollama not found. Installing..."
  if command -v brew &>/dev/null; then
    brew install ollama
  elif [[ "$(uname)" == "Linux" ]]; then
    curl -fsSL https://ollama.ai/install.sh | sh
  else
    echo "Cannot auto-install Ollama."
    echo "Visit: https://ollama.ai"
    exit 1
  fi
fi
echo "OK Ollama installed"

# ─── 3. Start Ollama if not running ──────────────────────────────────────────

if curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  echo "OK Ollama running"
else
  echo "Starting Ollama..."
  ollama serve >/dev/null 2>&1 &
  PIDS+=($!)
  STARTED_OLLAMA=true

  # Wait for it to be ready
  for i in $(seq 1 15); do
    if curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
      break
    fi
    if [ "$i" = "15" ]; then
      echo "Ollama failed to start after 15 seconds."
      exit 1
    fi
    sleep 1
  done
  echo "OK Ollama started"
fi

# ─── 4. Check models ─────────────────────────────────────────────────────────

MODEL_COUNT=$(curl -sf "$OLLAMA_URL/api/tags" 2>/dev/null | "$PYTHON" -c "
import sys, json
try:
    data = json.load(sys.stdin)
    models = data.get('models', [])
    print(len(models))
except:
    print('0')
" 2>/dev/null || echo "0")

if [ "$LIST_MODELS" = true ]; then
  # Just list and exit — delegate to Python script
  cd "$BRIDGE_DIR"

  if [ ! -d "venv" ]; then
    echo "Setting up Python environment..."
    "$PYTHON" -m venv venv
    venv/bin/pip install -r requirements.txt -q 2>&1 | tail -1
  fi

  venv/bin/python standalone_bridge.py --list-models --ollama-url "$OLLAMA_URL"
  exit 0
fi

if [ "$MODEL_COUNT" = "0" ]; then
  echo "No models installed. Detecting best model for your system..."
  TOTAL_RAM_GB=$("$PYTHON" -c "
import os, platform, subprocess
try:
    if platform.system() == 'Darwin':
        raw = subprocess.check_output(['sysctl', '-n', 'hw.memsize']).strip()
        print(int(raw) // (1024**3))
    else:
        pages = os.sysconf('SC_PHYS_PAGES')
        page_size = os.sysconf('SC_PAGE_SIZE')
        print((pages * page_size) // (1024**3))
except:
    print(8)
" 2>/dev/null || echo "8")

  if [ "$TOTAL_RAM_GB" -ge 16 ]; then
    PULL_MODEL="phi4:14b"
  elif [ "$TOTAL_RAM_GB" -ge 8 ]; then
    PULL_MODEL="qwen3:8b"
  else
    PULL_MODEL="gemma3:4b"
  fi
  echo "System has ${TOTAL_RAM_GB}GB RAM — pulling $PULL_MODEL..."
  echo "(This may take a few minutes on first run)"
  ollama pull "$PULL_MODEL"
  echo "OK Model downloaded: $PULL_MODEL"
else
  echo "OK $MODEL_COUNT model(s) available"
fi

# ─── 5. Setup Python environment ─────────────────────────────────────────────

cd "$BRIDGE_DIR"

# Check if venv exists and was built with the right Python version
NEED_VENV=false
if [ ! -d "venv" ]; then
  NEED_VENV=true
elif [ -f "venv/bin/python" ]; then
  VENV_VER=$(venv/bin/python --version 2>&1 | cut -d' ' -f2 | cut -d. -f1-2)
  WANT_VER=$(echo "$BEST_VER" | cut -d. -f1-2)
  if [ "$VENV_VER" != "$WANT_VER" ]; then
    echo "Recreating venv (was Python $VENV_VER, now $WANT_VER)..."
    rm -rf venv
    NEED_VENV=true
  fi
fi

if [ "$NEED_VENV" = true ]; then
  echo "Setting up Python environment..."
  "$PYTHON" -m venv venv
  venv/bin/pip install -r requirements.txt -q 2>&1 | tail -1
  # Store requirements hash for auto-update detection
  md5sum requirements.txt 2>/dev/null | cut -d' ' -f1 > venv/.req_hash 2>/dev/null || \
    md5 -q requirements.txt > venv/.req_hash 2>/dev/null || true
  echo "OK Dependencies installed"
else
  # Check if requirements.txt changed since last install
  CURRENT_HASH=$(md5sum requirements.txt 2>/dev/null | cut -d' ' -f1 || md5 -q requirements.txt 2>/dev/null || echo "")
  STORED_HASH=""
  if [ -f "venv/.req_hash" ]; then
    STORED_HASH=$(cat venv/.req_hash 2>/dev/null || echo "")
  fi

  if [ -n "$CURRENT_HASH" ] && [ "$CURRENT_HASH" != "$STORED_HASH" ]; then
    echo "Dependencies changed, updating..."
    venv/bin/pip install -r requirements.txt -q 2>&1 | tail -1
    echo "$CURRENT_HASH" > venv/.req_hash
    echo "OK Dependencies updated"
  else
    echo "OK Python environment ready"
  fi
fi

# Verify BLE deps if needed
if [ "$BLE_MODE" = true ]; then
  if ! venv/bin/python -c "import bleak" 2>/dev/null; then
    echo "Installing BLE dependencies..."
    venv/bin/pip install bleak -q 2>&1 | tail -1
    if ! venv/bin/python -c "import bleak" 2>/dev/null; then
      echo "BLE dependency (bleak) failed to install."
      echo "This requires Python 3.11+. Current: $(venv/bin/python --version 2>&1)"
      exit 1
    fi
  fi
  echo "OK BLE support ready"
fi

# ─── 6. RAG: pull embedding model if needed ──────────────────────────────────

if [ "$RAG_ENABLED" = true ] || [ -n "$INGEST_PATH" ]; then
  HAS_EMBED=$(curl -sf "$OLLAMA_URL/api/tags" 2>/dev/null | "$PYTHON" -c "
import sys, json
try:
    data = json.load(sys.stdin)
    models = [m['name'] for m in data.get('models', [])]
    print('yes' if any('nomic-embed-text' in m for m in models) else 'no')
except:
    print('no')
" 2>/dev/null || echo "no")

  if [ "$HAS_EMBED" = "no" ]; then
    echo "Pulling embedding model (nomic-embed-text) for RAG..."
    ollama pull nomic-embed-text
    echo "OK Embedding model ready"
  else
    echo "OK Embedding model available"
  fi
fi

# ─── 7. Handle document management commands ──────────────────────────────────

if [ "$INGEST_PATH" = "__list__" ]; then
  venv/bin/python manage_docs.py list
  exit 0
elif [ "$INGEST_PATH" = "__stats__" ]; then
  venv/bin/python manage_docs.py stats
  exit 0
elif [ -n "$INGEST_PATH" ] && [ "$INGEST_PATH" != "__next__" ]; then
  venv/bin/python manage_docs.py ingest "$INGEST_PATH"
  exit 0
fi

# ─── 8. Auto-ingest CONTEXT FILES when RAG is enabled ────────────────────────

CONTEXT_DIR="$PROJECT_ROOT/CONTEXT FILES"
if [ "$RAG_ENABLED" = true ] && [ -d "$CONTEXT_DIR" ]; then
  FILE_COUNT=$(find "$CONTEXT_DIR" -maxdepth 1 -type f \( -name "*.pdf" -o -name "*.zim" -o -name "*.txt" -o -name "*.md" \) | wc -l | tr -d ' ')
  if [ "$FILE_COUNT" -gt 0 ]; then
    echo "Auto-ingesting $FILE_COUNT file(s) from CONTEXT FILES/ (skipping already ingested)..."
    venv/bin/python manage_docs.py ingest "$CONTEXT_DIR" --skip-existing
    echo ""
  fi
fi

# ─── 9. Launch bridge ────────────────────────────────────────────────────────

echo ""
echo "=========================================="
echo "  Bridge starting..."
# Determine connection display
CONN_DISPLAY="USB Serial (auto-detect)"
for a in "${BRIDGE_ARGS[@]}"; do
  case "$a" in
    --ble) CONN_DISPLAY="Bluetooth LE" ;;
    --tcp) CONN_DISPLAY="TCP" ;;
    --serial) CONN_DISPLAY="USB Serial" ;;
  esac
done
echo "  Connection: $CONN_DISPLAY"
if [ "$RAG_ENABLED" = true ]; then
  echo "  RAG: enabled"
else
  echo "  RAG: disabled"
fi
echo "  Dashboard: http://localhost:$DASHBOARD_PORT"
echo "  Press Ctrl+C to stop"
echo "=========================================="
echo ""

# Open dashboard in default browser after a short delay (non-blocking)
(sleep 3 && (open "http://localhost:$DASHBOARD_PORT" 2>/dev/null || xdg-open "http://localhost:$DASHBOARD_PORT" 2>/dev/null)) &

venv/bin/python standalone_bridge.py "${BRIDGE_ARGS[@]}"
