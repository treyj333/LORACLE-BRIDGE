"""LORACLE BRIDGE — Offline AI Over Mesh Radio.

Connects to one or more radios (Meshtastic and/or MeshCore) via the
RadioBackend abstraction layer, routes incoming messages through a local
LLM (Ollama), and sends responses back over the correct radio.

Usage:
    python standalone_bridge.py
    python standalone_bridge.py --ble                              # Bluetooth LE
    python standalone_bridge.py --model mistral --serial /dev/cu.usbserial-0001
    python standalone_bridge.py --tcp 192.168.1.100:4403
    python standalone_bridge.py --protocol meshcore --serial /dev/cu.usbserial-0002
    python standalone_bridge.py --second-radio meshcore:serial:/dev/ttyUSB1
    python standalone_bridge.py --list-models
"""

import argparse
import glob
import hashlib
import json
import logging
import os
import queue
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import meshtastic
import meshtastic.serial_interface
import meshtastic.tcp_interface
from meshtastic import BROADCAST_ADDR
from pubsub import pub

try:
    from meshtastic.version import get_active_version as _get_meshtastic_version
except ImportError:
    _get_meshtastic_version = lambda: "unknown"

try:
    import meshtastic.ble_interface
    BLE_AVAILABLE = True
except ImportError:
    BLE_AVAILABLE = False

from ollama_client import OllamaClient, auto_select_model, MODEL_PROFILES, get_system_ram_gb
from protocol import chunk_message
from dashboard import (
    start_dashboard, record_message, update_state, set_bridge,
    register_addon_tab, register_addon_api_route, _emit_sse,
)
from addons import load_addons
from coverage_logger import CoverageLogger
from greeter import GreeterService, DEFAULT_GREETING
from radio.events import Protocol, Transport, UnifiedMessage, UnifiedNode
from radio.manager import RadioManager
from radio.meshtastic_backend import MeshtasticBackend
from bridge import DEFAULT_CONFIG as BRIDGE_DEFAULT_CONFIG, Relay, build_policy
from bridge.rate_limit import RelayRateLimiter
from db.bridge_events import BridgeEventStore
from routing import Tier, parse_prefix, route, USAGE_HINT, ModelNotConfiguredError
from routing.tiers import load_tiers, save_tiers, ROUTING_AUTO_KEY, ROUTING_SHOW_TAG_KEY
from db import init_db, migrate_from_json, ContactStore, MessageStore, SettingsStore

# ── Paths ──────────────────────────────────────────────────────────────────

_MESH_LLM_DIR = os.path.join(os.path.expanduser("~"), ".mesh-llm")
SETTINGS_PATH = os.path.join(_MESH_LLM_DIR, "settings.json")
DB_PATH = os.path.join(_MESH_LLM_DIR, "loracle.db")


def load_settings() -> dict:
    """Load persisted settings. Tries SQLite first, falls back to JSON."""
    defaults = {"ai_replies": True, "second_radio": None}
    # Try JSON fallback (pre-migration)
    try:
        with open(SETTINGS_PATH) as f:
            saved = json.load(f)
        defaults.update(saved)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return defaults


def save_settings(settings: dict):
    """Persist settings to ~/.mesh-llm/settings.json."""
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("standalone")

# How often to rescan interface.nodes for newly-synced positions, in seconds.
# Meshtastic-python populates interface.nodes asynchronously after connect, so
# a single one-shot load right after _connect_radio() catches almost nothing.
NODEDB_REFRESH_INTERVAL_S = 30

# Per-channel cooldown for public-channel AI replies. Prevents a chain of
# trigger-word messages from saturating the channel with bot responses.
PUBLIC_CHANNEL_COOLDOWN_SECS = 8

# Suppress noisy meshtastic protobuf DEBUG spam, but keep warnings visible
logging.getLogger("meshtastic.mesh_interface").setLevel(logging.WARNING)
logging.getLogger("meshtastic.stream_interface").setLevel(logging.WARNING)

# Max bytes for a single LoRa text message
MAX_LORA_TEXT = 233

# Deduplication cache: (node_id, content_hash) -> timestamp
_dedup_cache = {}  # type: Dict[Tuple[str, str], float]
DEDUP_TTL = 300  # 5 minutes
CONTEXT_TTL = 3600  # 1 hour — auto-clear conversation context after inactivity
RATE_LIMIT_SECS = 5  # Min seconds between messages from same node
MAX_TRACKED_NODES = 2000  # Cap on _node_positions/_node_meta to prevent memory leak
OVERFLOW_TTL = 3600  # 1 hour — auto-clear unclaimed !more pages


def get_dedup_cache_size() -> int:
    """Return the current size of the deduplication cache (public API for dashboard)."""
    return len(_dedup_cache)


# Trigger words — public channel messages must contain one of these to get a response
_AI_TRIGGERS = {"agent", "ai", "oracle", "loracle", "bridge", "help", "hey"}


def _is_addressed_to_ai(text: str) -> bool:
    """Check if a public channel message is directed at the AI."""
    if text.startswith("!"):
        return True  # Commands are always for us
    prefix = text[:50].lower()
    return any(trigger in prefix for trigger in _AI_TRIGGERS)


def auto_detect_serial_port() -> Optional[str]:
    """Find a Meshtastic device serial port on macOS/Linux."""
    candidates = (
        glob.glob("/dev/cu.usbserial-*")
        + glob.glob("/dev/cu.SLAB_USBtoUART*")
        + glob.glob("/dev/cu.wchusbserial*")
        + glob.glob("/dev/ttyUSB*")
        + glob.glob("/dev/ttyACM*")
    )
    return candidates[0] if candidates else None


# Maps Protocol enum short values ("mt", "mc") to DB-column strings
# ("meshtastic", "meshcore"). Keeps the DB schema stable while the radio
# abstraction uses the short form in unified IDs.
_PROTOCOL_SHORT_TO_DB = {"mt": "meshtastic", "mc": "meshcore"}


def _parse_second_radio(spec: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse a --second-radio spec like 'meshcore:serial:/dev/ttyUSB1'
    or 'meshcore:tcp:192.168.1.1:4000' into a config dict.

    Returns None on empty/invalid input.
    """
    if not spec:
        return None
    # Only split the first two colons; transport params may contain colons (tcp host:port)
    parts = spec.split(":", 2)
    if len(parts) < 3:
        return None
    protocol, transport, params = parts[0].lower(), parts[1].lower(), parts[2]
    if protocol not in ("meshcore",):
        return None  # meshtastic secondary not supported yet (primary radio is meshtastic)
    cfg: Dict[str, Any] = {"protocol": protocol, "transport": transport}
    if transport == "serial":
        if not params:
            return None
        cfg["serial_port"] = params
        return cfg
    if transport == "tcp":
        if ":" in params:
            host, port_str = params.rsplit(":", 1)
            try:
                cfg["tcp_port"] = int(port_str)
            except ValueError:
                return None
            cfg["tcp_host"] = host
        else:
            cfg["tcp_host"] = params
            cfg["tcp_port"] = 4000  # meshcore default
        return cfg
    if transport == "ble":
        cfg["ble_address"] = params or None
        return cfg
    return None


class StandaloneBridge:
    """LORACLE BRIDGE — LoRa + Oracle mesh AI bridge."""

    def __init__(
        self,
        connection_type: str = "serial",
        serial_port: Optional[str] = None,
        tcp_host: str = "192.168.1.1",
        tcp_port: int = 4403,
        ble_address: Optional[str] = None,
        ollama_url: str = "http://localhost:11434",
        model: str = "gemma3:4b",
        max_response_length: int = 200,
        system_prompt: Optional[str] = None,
        compression_enabled: bool = True,
        rag_enabled: bool = True,
        rag_dir: Optional[str] = None,
        dashboard_port: int = 8000,
        addon_config: Optional[Dict[str, dict]] = None,
        auto_greet: bool = True,
        greet_message: Optional[str] = None,
        public_talk: bool = True,
        protocol: str = "auto",
        ai_replies_enabled: bool = True,
        second_radio: Optional[str] = None,
    ):
        self.connection_type = connection_type
        self.serial_port = serial_port
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.ble_address = ble_address
        self.dashboard_port = dashboard_port
        self.compression_enabled = compression_enabled
        self.interface = None  # kept for backward compat (dashboard/addon access)
        self._running = False
        self._connection_paused = False
        self._reconnect_delay = 1
        self._ble_scanning = False
        self._connecting = False
        self._request_queue: queue.Queue = queue.Queue()
        self._start_time = time.time()
        self._ai_replies_enabled = ai_replies_enabled
        self._second_radio_config = _parse_second_radio(second_radio)
        if second_radio and self._second_radio_config is None:
            logger.warning(
                f"Ignoring invalid --second-radio spec: {second_radio!r} "
                "(expected 'meshcore:serial:/dev/…', 'meshcore:tcp:host[:port]', or 'meshcore:ble:[addr]')"
            )

        # ── SQLite persistence ────────────────────────────────────────────
        self._db = init_db(DB_PATH)
        migrate_from_json(self._db, SETTINGS_PATH)
        self._contact_store = ContactStore(self._db)
        self._message_store = MessageStore(self._db)
        self._settings_store = SettingsStore(self._db)
        # Run retention pruning at startup
        self._message_store.prune()

        # ── RadioManager (multi-protocol abstraction) ─────────────────────
        self._radio_manager = RadioManager()
        self._primary_backend = MeshtasticBackend(
            connection_type=connection_type,
            serial_port=serial_port,
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            ble_address=ble_address,
        )
        self._message_count = 0
        self._node_count = 0
        self._known_nodes = set()  # type: Set[str]
        self._node_last_active: Dict[str, float] = {}  # node_id -> last message timestamp
        self._overflow: Dict[str, tuple] = {}  # node_id -> (remaining_text, timestamp)
        self._node_positions: Dict[str, dict] = {}  # node_id -> {lat, lon, alt, last_update}
        self._node_meta: Dict[str, dict] = {}  # node_id -> {hops, hops_updated}
        self._device_metrics: Dict[str, dict] = {}  # node_id -> {battery, voltage, ...}
        self._traceroute_results: Dict[str, dict] = {}  # dest_id -> {route, timestamp}
        self._last_nodedb_refresh: float = 0.0  # last time we re-scanned interface.nodes
        self._channel_last_send: Dict[int, float] = {}  # public-channel reply cooldown
        self.public_talk = public_talk  # respond on public channels when addressed

        # ── Bridge relay (LORACLE v2 Phase 2 + Phase 5) ─────────────────
        # Cross-protocol relay between Meshtastic and MeshCore. The relay
        # observes every incoming message, applies policy/dedup/loop
        # guards + rate-limit + audit log, re-broadcasts to the other
        # network with a sender prefix. Config lives in SettingsStore
        # and hot-swaps via API.
        self._bridge_config = self._load_bridge_config()
        self._bridge_events: List[dict] = []  # ring buffer for BRIDGE tab
        # Phase 5: persistent audit log for every successful relay.
        self._bridge_event_store = BridgeEventStore(self._db)
        try:
            pruned = self._bridge_event_store.prune()
            if pruned:
                logger.info(f"Pruned {pruned} stale bridge_events rows")
        except Exception as e:
            logger.debug(f"bridge_events prune skipped: {e}")
        # Phase 5: per-direction sliding-window rate limiter. Defaults
        # (30 events / 60s) are defense-tech-mesh-friendly; operators
        # can tune via bridge_config keys rate_limit_max / rate_limit_window_s.
        rl_max = int(self._bridge_config.get("rate_limit_max", 30) or 30)
        rl_window = float(self._bridge_config.get("rate_limit_window_s", 60.0) or 60.0)
        self._bridge_rate_limiter = RelayRateLimiter(
            max_events=max(1, rl_max), window_seconds=max(1.0, rl_window)
        )
        self._relay = Relay(
            send_fn=self._bridge_send,
            policy=build_policy(self._bridge_config),
            identity_fn=self._bridge_sender_display,
            on_relay=self._on_bridge_relay,
            rate_limiter=self._bridge_rate_limiter,
        )

        # Coverage logger — append-only JSONL of (ts, node, lat, lon, rssi, snr) samples
        coverage_path = os.path.join(
            os.path.expanduser("~"), ".mesh-llm", "coverage.jsonl"
        )
        self.coverage = CoverageLogger(coverage_path)

        # Greeter — proactively DM new nodes a brief welcome
        greeter_path = os.path.join(
            os.path.expanduser("~"), ".mesh-llm", "greeted_nodes.json"
        )
        self.greeter = GreeterService(
            path=greeter_path,
            message=greet_message or DEFAULT_GREETING,
            enabled=auto_greet,
        )

        # RAG
        self.rag_enabled = rag_enabled
        self.rag_engine = None
        self._rag_disabled_nodes: set = set()  # Nodes that opted out of RAG

        if rag_enabled:
            try:
                from rag import RAGEngine
                self.rag_engine = RAGEngine(
                    ollama_base_url=ollama_url,
                    data_dir=rag_dir,
                )
                stats = self.rag_engine.get_stats()
                logger.info(
                    f"RAG enabled: {stats['total_docs']} docs, "
                    f"{stats['total_chunks']} chunks"
                )
            except ImportError as e:
                logger.warning(f"RAG dependencies not available: {e}")
                logger.warning("RAG disabled. Install deps: pip install numpy pymupdf beautifulsoup4")
                self.rag_enabled = False
            except Exception as e:
                logger.warning(f"RAG initialization failed: {e}")
                self.rag_enabled = False

        self.ollama = OllamaClient(
            base_url=ollama_url,
            model=model,
            max_response_length=max_response_length,
            system_prompt=system_prompt,
        )

        # Command registry: {cmd_string: (handler, help_text)}
        self._command_registry: Dict[str, Tuple[callable, str]] = {}
        self._init_commands()

        # Addons
        self._addons = []
        if addon_config is not None:
            self._addons = load_addons(self, addon_config)
            for addon in self._addons:
                # Register addon commands
                for cmd, (handler, help_text) in addon.get_commands().items():
                    self._command_registry[cmd] = (handler, help_text)
                # Register addon dashboard tabs and API routes
                tab = addon.get_dashboard_tab()
                if tab:
                    register_addon_tab(tab)
                for method, path, handler in addon.get_api_routes():
                    register_addon_api_route(method, path, handler)

    def start(self):
        """Start the bridge."""
        logger.info("Starting Standalone LORACLE BRIDGE")

        # Start web dashboard
        start_dashboard(self.dashboard_port)
        set_bridge(self)
        update_state(
            connection_type=self.connection_type,
            model=self.ollama.model,
            ollama_url=self.ollama.base_url,
            uptime_start=self._start_time,
            rag_enabled=self.rag_enabled,
        )

        # Verify Ollama is available
        if not self.ollama.is_available():
            logger.error(
                f"Cannot connect to Ollama at {self.ollama.base_url}. "
                "Make sure Ollama is running (ollama serve)."
            )
            sys.exit(1)

        logger.info(f"Ollama connected: {self.ollama.base_url} (model: {self.ollama.model})")

        # Register message handlers (kept for backward compat — the
        # MeshtasticBackend also subscribes via start_listening, but
        # the bridge still needs its own _on_receive for application-
        # level filtering that was previously interleaved with packet
        # parsing.  The backend now converts packets to UnifiedMessage;
        # application filtering happens in _processing_loop.)
        pub.subscribe(self._on_receive, "meshtastic.receive.text")
        pub.subscribe(self._on_position, "meshtastic.receive.position")
        pub.subscribe(self._on_telemetry, "meshtastic.receive.telemetry")
        pub.subscribe(self._on_traceroute, "meshtastic.receive.traceroute")
        # NodeInfo broadcasts — fires when a node announces its identity. Without this,
        # a node that only broadcasts NodeInfo + telemetry (never text / position) would
        # stay invisible until the 30s nodeDB rescan catches it.
        try:
            pub.subscribe(self._on_user, "meshtastic.receive.user")
        except Exception as e:
            logger.debug(f"user topic subscribe failed: {e}")

        self._running = True

        # Start background threads
        threading.Thread(target=self._processing_loop, daemon=True).start()
        threading.Thread(target=self._dedup_cleanup_loop, daemon=True).start()
        threading.Thread(target=self._context_cleanup_loop, daemon=True).start()

        # Connect to radio in background — the old proven path
        # (RadioManager/MeshtasticBackend are kept as library code for
        # future multi-radio support but NOT connected at runtime to
        # avoid fighting with the working _radio_connection_loop)
        threading.Thread(target=self._radio_connection_loop, daemon=True).start()
        # Node sync loop
        threading.Thread(target=self._node_sync_loop, daemon=True).start()

        # Secondary radio (MeshCore via RadioManager) — optional, only if
        # --second-radio was passed and the spec parsed cleanly.
        if self._second_radio_config:
            threading.Thread(target=self._connect_secondary_radio, daemon=True).start()
            threading.Thread(target=self._secondary_radio_ingest_loop, daemon=True).start()

        # Start addons
        for addon in self._addons:
            try:
                addon.on_start()
            except Exception as e:
                logger.warning(f"Addon {addon.name} on_start error: {e}")

        logger.info("Bridge ready — dashboard at http://localhost:%d", self.dashboard_port)

        # Main loop
        while self._running:
            try:
                time.sleep(1)
            except KeyboardInterrupt:
                logger.info("Shutting down...")
                self._running = False
                break

        self._cleanup()

    def _check_firmware(self):
        """Check firmware/library version compatibility and log it."""
        lib_ver = _get_meshtastic_version()
        fw_ver = "unknown"
        hw_model = "unknown"
        try:
            if self.interface and hasattr(self.interface, "metadata") and self.interface.metadata:
                fw_ver = getattr(self.interface.metadata, "firmware_version", "unknown")
                hw_model_val = getattr(self.interface.metadata, "hw_model", None)
                if hw_model_val is not None:
                    hw_model = str(hw_model_val)
        except (AttributeError, TypeError) as e:
            logger.debug(f"Could not read firmware metadata: {e}")

        logger.info(f"Meshtastic library: {lib_ver} | Firmware: {fw_ver} | HW: {hw_model}")

        # Store for dashboard
        update_state(
            firmware_version=fw_ver,
            library_version=lib_ver,
            hw_model=hw_model,
        )

        # Warn on major version mismatch
        try:
            lib_major = lib_ver.split(".")[0]
            fw_major = fw_ver.split(".")[0]
            if lib_major != fw_major and fw_ver != "unknown":
                logger.warning(
                    f"Version mismatch: library v{lib_ver} vs firmware v{fw_ver}. "
                    "This may cause protobuf parsing errors. "
                    "Consider updating your radio firmware or Python library to match."
                )
        except (AttributeError, TypeError, ValueError) as e:
            logger.debug(f"Version check failed: {e}")

    def _radio_connection_loop(self):
        """Background loop: connect to radio, reconnect if it drops."""
        while self._running:
            if self._connection_paused:
                time.sleep(2)
                continue
            if self._is_interface_alive():
                # Periodically rescan the nodeDB for newly-synced positions.
                # The meshtastic library streams the nodeDB asynchronously after
                # connect, so positions can keep showing up for minutes.
                if time.time() - self._last_nodedb_refresh >= NODEDB_REFRESH_INTERVAL_S:
                    self._load_nodedb_positions()
                # Drain at most one queued greeting per pump tick (rate-limited
                # internally to ~1 send per send_interval_s)
                try:
                    self.greeter.pump(self.interface)
                except Exception as e:
                    logger.debug(f"Greeter pump error: {e}")
                time.sleep(5)
                continue
            # Skip if another thread is already connecting (e.g. dashboard switch)
            if self._connecting:
                time.sleep(2)
                continue
            # Interface is dead or not connected — try to connect
            self._connect_radio()
            if self.interface:
                self._check_firmware()
                # Tell the greeter who we are so it never DMs ourselves
                try:
                    self_id = self._get_self_node_id()
                    if self_id:
                        self.greeter.set_self_id(self_id)
                except Exception as e:
                    logger.debug(f"Could not derive self node id: {e}")
                self._load_nodedb_positions()
                # BLE needs more time to stabilize after connecting
                if self.connection_type == "ble":
                    time.sleep(10)
            else:
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    def _connect_radio(self):
        """Attempt to connect to the Meshtastic device once.

        Sets self.interface on success, leaves it None on failure.
        Never calls sys.exit — caller handles retries.
        """
        if self._connecting:
            return
        self._connecting = True
        try:
            if self.connection_type == "ble":
                if not BLE_AVAILABLE:
                    logger.error(
                        "BLE not available. Requires Python 3.11+ with bleak installed.\n"
                        "Fix: brew install python@3.12 && rm -rf venv && ./mesh-llm.sh --ble"
                    )
                    return
                addr_msg = f": {self.ble_address}" if self.ble_address else " (scanning...)"
                logger.info(f"Connecting via BLE{addr_msg}")
                self.interface = meshtastic.ble_interface.BLEInterface(
                    address=self.ble_address if self.ble_address else None
                )
            elif self.connection_type == "tcp":
                logger.info(f"Connecting via TCP: {self.tcp_host}:{self.tcp_port}")
                self.interface = meshtastic.tcp_interface.TCPInterface(
                    hostname=self.tcp_host,
                    portNumber=self.tcp_port,
                )
            else:
                port = self.serial_port or auto_detect_serial_port()
                if not port:
                    logger.warning(
                        "No Meshtastic device found. Waiting for USB connection... "
                        "(retry in %ds)", self._reconnect_delay
                    )
                    update_state(connected=False)
                    return
                logger.info(f"Connecting via serial: {port}")
                self.interface = meshtastic.serial_interface.SerialInterface(
                    devPath=port
                )

            self._reconnect_delay = 1
            logger.info("Connected to Meshtastic device")
            if self.connection_type == "ble":
                conn_addr = self.ble_address or "scan"
            elif self.connection_type == "tcp":
                conn_addr = f"{self.tcp_host}:{self.tcp_port}"
            else:
                conn_addr = self.serial_port or "auto"
            update_state(connected=True, connection_address=conn_addr or "auto")

        except Exception as e:
            logger.error(
                f"Connection failed: {e}. Retrying in {self._reconnect_delay}s..."
            )
            self.interface = None
            update_state(connected=False)
        finally:
            self._connecting = False

    # ─── BLE scanning & connection management ──────────────────────────────────

    def scan_ble_devices(self, timeout: float = 10.0) -> List[dict]:
        """Scan for nearby Meshtastic BLE devices.

        Runs the scan in a **subprocess** to protect the bridge from macOS
        CoreBluetooth SIGABRT crashes (which happen if Python's Info.plist
        lacks NSBluetoothAlwaysUsageDescription).  The subprocess uses the
        meshtastic library's BLEInterface.scan() which filters by the
        Meshtastic service UUID.
        """
        if not BLE_AVAILABLE:
            return []
        if self._ble_scanning:
            return []

        self._ble_scanning = True
        try:
            import subprocess
            # Run scan in isolated subprocess — if it crashes, bridge survives
            scan_script = (
                "import json, sys\n"
                "try:\n"
                "    import meshtastic.ble_interface\n"
                "    devices = meshtastic.ble_interface.BLEInterface.scan()\n"
                "    result = []\n"
                "    for d in devices:\n"
                "        result.append({\n"
                "            'name': d.name or 'Meshtastic Device',\n"
                "            'address': str(d.address),\n"
                "            'rssi': d.rssi if hasattr(d, 'rssi') and d.rssi else -100,\n"
                "        })\n"
                "    result.sort(key=lambda x: x['rssi'], reverse=True)\n"
                "    print(json.dumps(result))\n"
                "except Exception as e:\n"
                "    print(json.dumps({'error': str(e)}))\n"
            )
            # Use the same Python that's running the bridge
            venv_python = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "venv", "bin", "python"
            )
            python_cmd = venv_python if os.path.exists(venv_python) else sys.executable

            proc = subprocess.run(
                [python_cmd, "-c", scan_script],
                capture_output=True, text=True,
                timeout=int(timeout) + 10,
            )

            if proc.returncode != 0:
                stderr = proc.stderr.strip()
                if "NSBluetoothAlwaysUsageDescription" in stderr or proc.returncode == -6:
                    logger.error(
                        "BLE scan crashed — Python lacks Bluetooth permission on macOS. "
                        "Run: sudo /usr/libexec/PlistBuddy -c "
                        "\"Add :NSBluetoothAlwaysUsageDescription string "
                        "'Needs Bluetooth for Meshtastic.'\" "
                        "\"$(python3 -c \"import sys,os; print(os.path.join("
                        "sys.base_prefix,'Resources/Python.app/Contents/Info.plist'))\")\" "
                        "&& then restart the bridge."
                    )
                    return [{"error": "bluetooth_permission",
                             "message": "Python needs Bluetooth permission on macOS. "
                                        "Restart the bridge with ./mesh-llm.sh to auto-fix, "
                                        "or see the Debug log for manual instructions."}]
                logger.error(f"BLE scan subprocess failed (exit {proc.returncode}): {stderr}")
                return [{"error": "scan_failed", "message": stderr or "BLE scan crashed"}]

            output = proc.stdout.strip()
            if not output:
                return []

            data = json.loads(output)
            if isinstance(data, dict) and "error" in data:
                logger.error(f"BLE scan error: {data['error']}")
                return [{"error": "scan_error", "message": data["error"]}]
            return data

        except subprocess.TimeoutExpired:
            logger.error("BLE scan timed out")
            return [{"error": "timeout", "message": "BLE scan timed out"}]
        except Exception as e:
            logger.error(f"BLE scan failed: {e}")
            return [{"error": "scan_failed", "message": str(e)}]
        finally:
            self._ble_scanning = False

    def disconnect_radio(self):
        """Disconnect current radio and pause auto-reconnect."""
        self._connection_paused = True
        if self.interface:
            try:
                self.interface.close()
            except Exception as e:
                logger.warning(f"Error closing interface: {e}")
            self.interface = None
        update_state(connected=False, connection_address="")
        logger.info("Radio disconnected (auto-reconnect paused)")

    def switch_connection(self, connection_type: str, address: str = None,
                          host: str = None, port: int = None) -> dict:
        """Switch to a different connection type/device.

        Returns {ok: bool, error: str?}
        """
        # Close existing interface
        if self.interface:
            try:
                self.interface.close()
            except Exception as e:
                logger.debug(f"Error closing interface during switch: {e}")
            self.interface = None

        # Also update the primary backend if it exists
        if hasattr(self, '_primary_backend'):
            try:
                self._primary_backend.switch_connection(
                    connection_type,
                    address=address, host=host, port=port,
                )
            except Exception as e:
                logger.debug(f"Backend switch error: {e}")

        # Update connection params
        self.connection_type = connection_type
        if connection_type == "ble":
            self.ble_address = address
            if address:
                self._save_last_ble_device(address)
        elif connection_type == "tcp":
            if host:
                self.tcp_host = host
            if port:
                self.tcp_port = port
        elif connection_type == "serial":
            self.serial_port = address  # None means auto-detect

        # Unpause and reset reconnect delay
        self._connection_paused = False
        self._reconnect_delay = 1
        update_state(connection_type=connection_type)

        # Attempt immediate connection
        self._connect_radio()
        if self.interface:
            self._check_firmware()
            addr = address or host or self.serial_port or "auto"
            update_state(connection_address=addr)
            return {"ok": True}
        else:
            return {"ok": False, "error": "Connection started — will retry in background"}

    @staticmethod
    def _ble_config_path() -> str:
        config_dir = os.path.expanduser("~/.mesh-llm")
        os.makedirs(config_dir, exist_ok=True)
        return os.path.join(config_dir, "ble_last_device.json")

    def _save_last_ble_device(self, address: str, name: str = ""):
        """Persist last-connected BLE device for quick reconnect."""
        try:
            with open(self._ble_config_path(), "w") as f:
                json.dump({"address": address, "name": name}, f)
        except Exception as e:
            logger.warning(f"Failed to save BLE device: {e}")

    @staticmethod
    def load_last_ble_device() -> Optional[dict]:
        """Load last-connected BLE device info."""
        path = StandaloneBridge._ble_config_path()
        try:
            if os.path.exists(path):
                with open(path) as f:
                    return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.debug(f"Could not load BLE config: {e}")
        return None

    def _persist_incoming(
        self,
        protocol: str,
        sender: str,
        text: str,
        channel: int,
        is_dm: bool,
        rssi: Optional[int] = None,
        snr: Optional[float] = None,
        hops: Optional[int] = None,
    ) -> None:
        """Persist an incoming text message to SQLite. Protocol-agnostic.

        Called by the Meshtastic pubsub path (_on_receive) and the
        secondary-radio ingest loop (_secondary_radio_ingest_loop). Both
        pass a protocol string ("meshtastic" or "meshcore") that ends up
        on the contacts and messages rows.
        """
        try:
            self._contact_store.upsert(
                contact_id=sender, protocol=protocol,
                backend_id=sender, short_name=sender[-6:] if len(sender) > 6 else sender,
                rssi=rssi, snr=snr, hops=hops,
            )
            if is_dm:
                self._message_store.insert(
                    contact_id=sender, direction="in", author="human",
                    text=text, protocol=protocol,
                    hops_traveled=hops, rx_rssi=rssi, rx_snr=snr,
                )
                self._contact_store.increment_unread(sender)
            else:
                chan_id = f"{protocol}:channel:{channel}"
                chan_name = f"Channel {channel}"
                self._contact_store.upsert(
                    contact_id=chan_id, protocol=protocol,
                    backend_id=chan_id, short_name=chan_name,
                    long_name=chan_name, is_channel=True,
                    channel_idx=channel, channel_name=chan_name,
                )
                self._message_store.insert(
                    contact_id=chan_id, direction="in", author="human",
                    text=f"{sender}: {text}", protocol=protocol,
                    hops_traveled=hops, rx_rssi=rssi, rx_snr=snr,
                    is_channel_msg=True,
                )
                self._contact_store.increment_unread(chan_id)
        except Exception as e:
            logger.debug(f"DB persist error: {e}")

    # ── Bridge relay helpers (LORACLE v2 Phase 2) ────────────────────────

    def _load_bridge_config(self) -> Dict[str, Any]:
        """Read bridge config from SettingsStore, falling back to defaults.

        Returns a fresh copy of DEFAULT_CONFIG if the key is unset. Shape
        is documented in bridge/config.py.
        """
        try:
            cfg = self._settings_store.get("bridge_config", None)
            if isinstance(cfg, dict):
                return cfg
        except Exception as e:
            logger.debug(f"Could not load bridge_config: {e}")
        return dict(BRIDGE_DEFAULT_CONFIG)

    def _save_bridge_config(self, cfg: Dict[str, Any]) -> None:
        """Persist bridge config and hot-swap the relay's policy.

        Accepts an already-validated dict (validation lives in the API
        endpoint). Raises if the settings write fails.
        """
        self._settings_store.set("bridge_config", cfg)
        self._bridge_config = cfg
        self._relay.set_policy(build_policy(cfg))
        logger.info(f"Bridge config updated: enabled={cfg.get('enabled')} "
                    f"rules={len(cfg.get('rules', []))}")

    def _bridge_send(self, dest_protocol: str, text: str, channel: int) -> None:
        """Protocol-agnostic broadcast used as the relay's send_fn.

        Meshtastic goes through the live ``self.interface`` (the proven
        pubsub path). MeshCore goes through RadioManager. Raises on any
        delivery failure so the relay records a drop instead of a
        successful delivery.
        """
        if dest_protocol == "meshtastic":
            if not self.interface or not self._is_interface_alive():
                raise ConnectionError("primary meshtastic interface not alive")
            self.interface.sendText(
                text,
                destinationId=BROADCAST_ADDR,
                channelIndex=channel,
                wantAck=False,
            )
        elif dest_protocol == "meshcore":
            # "mc:" with empty native_id tells RadioManager to pick the
            # MeshCore backend and broadcast on the given channel.
            self._radio_manager.send("mc:", text, channel=channel, is_dm=False)
        else:
            raise ValueError(f"unknown bridge destination protocol: {dest_protocol!r}")

    def _bridge_sender_display(self, source_protocol: str, sender: str) -> str:
        """Pick the most human-readable name for a bridged sender.

        Preference: custom_name → long_name → short_name → last-6 of the
        native id. Falls back gracefully if the contact isn't in the DB
        yet (first-ever message from that node).
        """
        try:
            contact = self._contact_store.get(sender)
            if contact:
                for key in ("custom_name", "long_name", "short_name"):
                    val = contact.get(key)
                    if val:
                        return str(val)
        except Exception as e:
            logger.debug(f"sender-display lookup error: {e}")
        return sender[-6:] if len(sender) > 6 else sender

    def _on_bridge_relay(self, event: Dict[str, Any]) -> None:
        """Handle a successful relay: ring buffer + SSE + audit log + addons.

        Four things happen here:
          1. Bounded in-memory buffer for the BRIDGE tab's fast path.
          2. SSE push so clients don't have to poll.
          3. Persistent audit log in the bridge_events SQLite table.
          4. Each addon's on_bridged_message() hook is fired — lets
             Sentinel/Triage/Brief observe cross-protocol traffic.

        Exceptions in any path are logged and swallowed so one broken
        consumer can't break the relay loop.
        """
        event["timestamp"] = time.time()
        self._bridge_events.append(event)
        if len(self._bridge_events) > 200:
            # Trim to the last 200 — bounded buffer for dashboard polling.
            del self._bridge_events[:-200]
        try:
            _emit_sse("bridge.relay", {"event": event})
        except Exception as e:
            logger.debug(f"[bridge] SSE emit error: {e}")
        # Persist to audit log (Phase 5).
        try:
            self._bridge_event_store.insert(
                source_protocol=event.get("source", ""),
                dest_protocol=event.get("dest", ""),
                channel=int(event.get("channel", 0)),
                sender=event.get("sender", ""),
                sender_display=event.get("sender_display"),
                text=event.get("text", ""),
                outcome="relayed",
                timestamp=event["timestamp"],
            )
        except Exception as e:
            logger.debug(f"[bridge] audit-log insert error: {e}")
        # Fire addon hook (Phase 5).
        for addon in self._addons:
            try:
                addon.on_bridged_message(event)
            except Exception as e:
                logger.warning(
                    f"Addon {addon.name} on_bridged_message error: {e}"
                )

    def _on_receive(self, packet, interface):
        """Handle incoming text message from the mesh."""
        try:
            sender = packet.get("fromId", "unknown")
            to_id = packet.get("toId", "")
            text = packet.get("decoded", {}).get("text", "")
            channel = packet.get("channel", 0)
            protocol = "meshtastic"

            # Record hop metadata for every packet, even if we ignore the text
            self._record_packet_meta(sender, packet)

            if not text or not text.strip():
                return

            # ── DM vs broadcast detection ──────────────────────────────────
            # We compare the packet's `toId` against our own `!hex` node id.
            # If `_get_self_node_id()` returns None (interface still warming
            # up), fall back to legacy "treat everything as a DM" so we never
            # silently drop a message during the first few seconds after a
            # reconnect. Real DMs to us → always reply privately.
            self_id = self._get_self_node_id()
            if self_id and to_id:
                is_dm = (to_id.lower() == self_id.lower())
            else:
                is_dm = True  # legacy fallback

            # Public channel: only reply when explicitly addressed (or to !cmds).
            # This keeps the bridge silent on casual chat. Trigger word logic
            # lives in the module-level _is_addressed_to_ai() helper.
            if not is_dm:
                if not self.public_talk:
                    return  # public-channel talk hard-disabled via CLI
                if not _is_addressed_to_ai(text):
                    # Still let addon observers see public traffic (Brief etc.)
                    for addon in self._addons:
                        try:
                            addon.on_message(sender, text.strip(), packet)
                        except Exception as e:
                            logger.debug(f"Addon {addon.name} observer error: {e}")
                    logger.debug(
                        f"Public ch{channel} from {sender} not addressed to AI, ignoring"
                    )
                    return
                # Per-channel cooldown — stops a chain of trigger-word messages
                # from saturating the public channel with bot replies.
                last = self._channel_last_send.get(channel, 0)
                if time.time() - last < PUBLIC_CHANNEL_COOLDOWN_SECS:
                    logger.info(
                        f"Public ch{channel} cooldown active "
                        f"({int(time.time() - last)}s < {PUBLIC_CHANNEL_COOLDOWN_SECS}s), "
                        f"dropping reply to {sender}"
                    )
                    return
                self._channel_last_send[channel] = time.time()
                logger.info(f"Public ch{channel}: addressed by {sender}")

            # Deduplication
            content_hash = hashlib.md5(text.encode()).hexdigest()[:8]
            cache_key = (sender, content_hash)
            now = time.time()

            if cache_key in _dedup_cache and (now - _dedup_cache[cache_key]) < DEDUP_TTL:
                logger.debug(f"Duplicate from {sender}, ignoring")
                return

            _dedup_cache[cache_key] = now

            # Rate limiting — prevent spam while inference is running
            if sender in self._node_last_active:
                elapsed_since_last = now - self._node_last_active[sender]
                if elapsed_since_last < RATE_LIMIT_SECS:
                    logger.info(
                        f"Rate-limited {sender} "
                        f"({elapsed_since_last:.0f}s < {RATE_LIMIT_SECS}s cooldown)"
                    )
                    return

            # Track nodes
            self._known_nodes.add(sender)
            self._node_count = len(self._known_nodes)
            try:
                self.greeter.maybe_greet(sender)
            except Exception as e:
                logger.debug(f"Greeter maybe_greet error: {e}")

            ch_label = "DM" if is_dm else f"ch{channel}"
            logger.info(f"Received from {sender} ({ch_label}): {text[:100]}")
            record_message("in", sender, text.strip())

            # Extract packet metadata for persistence + coverage logging
            rssi_val = packet.get("rxRssi")
            snr_val = packet.get("rxSnr")
            hop_start = packet.get("hopStart")
            hop_limit = packet.get("hopLimit")
            hops = None
            if hop_start is not None and hop_limit is not None:
                h = int(hop_start) - int(hop_limit)
                if 0 <= h <= 30:
                    hops = h

            # Persist to SQLite via shared helper (also used by secondary-radio ingest)
            self._persist_incoming(
                protocol, sender, text.strip(), channel, is_dm,
                rssi=rssi_val, snr=snr_val, hops=hops,
            )

            # Bridge relay observation — may re-broadcast to MeshCore.
            # Policy/dedup/loop-guard decisions all live inside the Relay.
            try:
                self._relay.observe(protocol, sender, text.strip(), channel, is_dm)
            except Exception as e:
                logger.warning(f"[bridge] relay.observe(meshtastic) error: {e}")

            # Coverage sample — log signal strength against the sender's last
            # known position, if we have one and the packet carries RSSI/SNR.
            rssi = packet.get("rxRssi")
            snr = packet.get("rxSnr")
            if (rssi is not None or snr is not None):
                pos = self._node_positions.get(sender)
                if pos and pos.get("lat") is not None and pos.get("lon") is not None:
                    self.coverage.record(
                        sender, pos["lat"], pos["lon"], rssi=rssi, snr=snr
                    )

            # Notify addon observers
            for addon in self._addons:
                try:
                    addon.on_message(sender, text.strip(), packet)
                except Exception as e:
                    logger.warning(f"Addon {addon.name} on_message error: {e}")

            self._request_queue.put(("meshtastic", sender, text.strip(), channel, is_dm))

        except Exception as e:
            logger.error(f"Error processing incoming message: {e}")

    def _record_packet_meta(self, sender: str, packet: dict) -> None:
        """Extract per-node metadata (hop count, etc.) from any incoming packet.

        Meshtastic packets carry `hopStart` (original hop limit set by the
        sender) and `hopLimit` (remaining hops when we received it). Actual
        hops = hopStart - hopLimit. Not every packet has both fields, so we
        leave existing data alone when either is missing.
        """
        if not sender:
            return
        try:
            hop_start = packet.get("hopStart")
            hop_limit = packet.get("hopLimit")
            if hop_start is None or hop_limit is None:
                return
            hops = int(hop_start) - int(hop_limit)
            if hops < 0 or hops > 30:
                return  # garbage, ignore
            entry = self._node_meta.setdefault(sender, {})
            entry["hops"] = hops
            entry["hops_updated"] = time.time()
        except (TypeError, ValueError):
            return

    @staticmethod
    def _extract_position(pos: dict) -> Optional[Tuple[float, float, float]]:
        """Pull (lat, lon, alt) from a meshtastic position dict.

        Handles both forms the meshtastic-python lib uses depending on version
        and code path:
          - degrees floats:  {"latitude": 34.05, "longitude": -118.24}
          - 1e7 integers:    {"latitudeI": 340500000, "longitudeI": -1182400000}

        Returns None if either coord is missing or both are zero (null island
        sentinel many radios send before they get a real fix).
        """
        if not pos:
            return None
        lat = pos.get("latitude")
        if lat is None:
            lat = pos.get("latitudeI")
        lon = pos.get("longitude")
        if lon is None:
            lon = pos.get("longitudeI")
        if lat is None or lon is None:
            return None
        # latitudeI/longitudeI are 1e-7 degrees
        if isinstance(lat, int) and abs(lat) > 900:
            lat = lat / 1e7
        if isinstance(lon, int) and abs(lon) > 1800:
            lon = lon / 1e7
        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            return None
        if lat == 0 and lon == 0:
            return None
        alt = pos.get("altitude", 0) or 0
        return lat, lon, alt

    def _store_node_position(self, node_id: str, lat: float, lon: float, alt: float,
                              ts: float = None):
        """Store a node position, evicting the oldest entry if at capacity."""
        self._node_positions[node_id] = {
            "lat": lat, "lon": lon, "alt": alt, "last_update": ts if ts is not None else time.time(),
        }
        if len(self._node_positions) > MAX_TRACKED_NODES:
            oldest = min(
                self._node_positions,
                key=lambda k: self._node_positions[k].get("last_update", 0),
            )
            del self._node_positions[oldest]
            self._node_meta.pop(oldest, None)

    def _on_position(self, packet, interface):
        """Handle incoming position packet from the mesh."""
        try:
            sender = packet.get("fromId", "")
            if not sender:
                return
            self._known_nodes.add(sender)  # ensure node is visible to frontend
            self._record_packet_meta(sender, packet)
            parsed = self._extract_position(packet.get("decoded", {}).get("position", {}))
            if parsed is None:
                return
            lat, lon, alt = parsed
            is_new = sender not in self._node_positions
            self._store_node_position(sender, lat, lon, alt)
            if is_new:
                logger.info(f"Position update (new): {sender} -> {lat:.5f}, {lon:.5f}")
                try:
                    self.greeter.maybe_greet(sender)
                except Exception as e:
                    logger.debug(f"Greeter maybe_greet error: {e}")
            else:
                logger.debug(f"Position update: {sender} -> {lat:.5f}, {lon:.5f}")

            # Coverage sample — pull RSSI/SNR from the same packet if present
            rssi = packet.get("rxRssi")
            snr = packet.get("rxSnr")
            if rssi is not None or snr is not None:
                self.coverage.record(sender, lat, lon, rssi=rssi, snr=snr)
        except Exception as e:
            logger.debug(f"Error processing position: {e}")

    def _on_telemetry(self, packet, interface):
        """Handle incoming telemetry packet — extract device metrics."""
        try:
            sender = packet.get("fromId", "")
            if not sender:
                return
            self._known_nodes.add(sender)  # ensure node is visible to frontend
            telemetry = packet.get("decoded", {}).get("telemetry", {})
            dm = telemetry.get("deviceMetrics", {})
            if dm:
                entry = self._device_metrics.setdefault(sender, {})
                if "batteryLevel" in dm:
                    entry["battery"] = dm["batteryLevel"]
                if "voltage" in dm:
                    entry["voltage"] = round(dm["voltage"], 2)
                if "channelUtilization" in dm:
                    entry["ch_util"] = round(dm["channelUtilization"], 1)
                if "airUtilTx" in dm:
                    entry["air_util"] = round(dm["airUtilTx"], 1)
                entry["updated"] = time.time()
            env = telemetry.get("environmentMetrics", {})
            if env:
                entry = self._device_metrics.setdefault(sender, {})
                if "temperature" in env:
                    entry["temperature"] = round(env["temperature"], 1)
                if "relativeHumidity" in env:
                    entry["humidity"] = round(env["relativeHumidity"], 1)
                entry["updated"] = time.time()
        except Exception as e:
            logger.debug(f"Error processing telemetry: {e}")

    def _on_user(self, packet, interface):
        """Handle NodeInfo (user) broadcast — captures identity of newly-announcing nodes.

        Without this subscription, a brand-new node that sends only NodeInfo + telemetry
        (the typical first packets a radio broadcasts on boot) would stay invisible to the
        UI until the 30-second nodeDB rescan caught it.
        """
        try:
            sender = packet.get("fromId", "")
            if not sender:
                return
            self._known_nodes.add(sender)
            self._record_packet_meta(sender, packet)
            user = packet.get("decoded", {}).get("user", {})
            short = user.get("shortName") or user.get("short_name")
            long_name = user.get("longName") or user.get("long_name")
            hw_model = user.get("hwModel") or user.get("hw_model")
            meta = self._node_meta.setdefault(sender, {})
            if short:
                meta["short_name"] = short
            if long_name:
                meta["long_name"] = long_name
            if hw_model:
                meta["hw_model"] = str(hw_model)
                # Also cache into device_metrics so the HW color toggle picks it up immediately
                self._device_metrics.setdefault(sender, {})["hw_model"] = str(hw_model)
            logger.info(f"NodeInfo from {sender}: {short or '?'} / {long_name or '?'}")
        except Exception as e:
            logger.debug(f"Error processing user packet: {e}")

    def _on_traceroute(self, packet, interface):
        """Handle traceroute response — store the route."""
        try:
            sender = packet.get("fromId", "")
            if sender:
                self._known_nodes.add(sender)  # ensure node is visible to frontend
            decoded = packet.get("decoded", {})
            route_back = decoded.get("traceroute", {}).get("route", [])
            route_towards = decoded.get("traceroute", {}).get("routeBack", [])
            hops = []
            for node_num in route_back:
                hops.append(f"!{node_num:08x}" if isinstance(node_num, int) else str(node_num))
            self._traceroute_results[sender] = {
                "route": hops,
                "dest": sender,
                "timestamp": time.time(),
            }
            logger.info(f"Traceroute to {sender}: {' → '.join(hops) or 'direct'}")
        except Exception as e:
            logger.debug(f"Error processing traceroute: {e}")

    def _load_nodedb_positions(self):
        """Re-scan the meshtastic nodeDB and merge any positions into _node_positions.

        Safe to call repeatedly. The meshtastic library populates interface.nodes
        asynchronously after connect, so we run this both at connect time AND
        periodically from the reconnect loop (see NODEDB_REFRESH_INTERVAL_S).
        """
        if not self.interface:
            return
        try:
            nodes = getattr(self.interface, "nodes", None)
            if not nodes:
                return
            protocol = "meshtastic"
            # First-ever startup safety net: silently mark every node already
            # in the radio's nodeDB as "known" so we don't blast the entire
            # mesh on initial deployment. Idempotent — only does work once.
            try:
                self.greeter.seed_from_nodedb(list(nodes.keys()))
            except Exception as e:
                logger.debug(f"Greeter seed error: {e}")

            new_count = 0
            for node_id, info in nodes.items():
                key = str(node_id)
                # Greet any node we discover from the nodeDB after the seed
                try:
                    self.greeter.maybe_greet(key)
                except Exception as e:
                    logger.debug(f"Greeter maybe_greet error: {e}")

                # Auto-populate contact in DB so it appears in messenger sidebar
                try:
                    user_info = info.get("user", {})
                    long_name = user_info.get("longName")
                    short_name = user_info.get("shortName") or (key[-6:] if len(key) > 6 else key)
                    hops = None
                    meta = self._node_meta.get(key)
                    if meta and "hops" in meta:
                        hops = meta["hops"]
                    self._contact_store.upsert(
                        contact_id=key, protocol=protocol,
                        backend_id=key, short_name=short_name,
                        long_name=long_name, hops=hops,
                    )
                except Exception as e:
                    logger.debug(f"Contact upsert from nodeDB error: {e}")

                self._known_nodes.add(key)

                # Extract device metrics from nodeDB
                dm = info.get("deviceMetrics", {})
                if dm:
                    entry = self._device_metrics.setdefault(key, {})
                    if "batteryLevel" in dm:
                        entry["battery"] = dm["batteryLevel"]
                    if "voltage" in dm:
                        entry["voltage"] = round(dm["voltage"], 2)
                    if not entry.get("updated"):
                        entry["updated"] = time.time()
                hw = info.get("user", {}).get("hwModel")
                if hw:
                    self._device_metrics.setdefault(key, {})["hw_model"] = hw

                parsed = self._extract_position(info.get("position", {}))
                if parsed is None:
                    continue
                lat, lon, alt = parsed
                is_new = key not in self._node_positions
                pos_ts = info.get("position", {}).get("time", time.time())
                self._store_node_position(key, lat, lon, alt, ts=pos_ts)
                if is_new:
                    new_count += 1
                    logger.info(f"Position from nodeDB (new): {key} -> {lat:.5f}, {lon:.5f}")
            if new_count:
                logger.info(
                    f"NodeDB scan: +{new_count} new positions "
                    f"(total tracked: {len(self._node_positions)}, nodeDB size: {len(nodes)})"
                )
            self._last_nodedb_refresh = time.time()
        except Exception as e:
            logger.debug(f"Could not read nodeDB positions: {e}")

    def _connect_secondary_radio(self):
        """Construct and register a secondary MeshCore backend via _radio_manager.

        Runs in a background thread (see start()). MeshCoreBackend.connect()
        can block up to 30s, so keep this off the main thread. On failure,
        logs an error and returns — the rest of the bridge keeps running
        on the primary Meshtastic radio.
        """
        cfg = self._second_radio_config
        if not cfg:
            return
        if cfg.get("protocol") != "meshcore":
            logger.warning(
                f"Unsupported secondary-radio protocol: {cfg.get('protocol')!r}"
            )
            return
        try:
            from radio.meshcore_backend import MeshCoreBackend
        except ImportError:
            logger.warning(
                "meshcore library not installed — skipping secondary radio. "
                "Install with: pip install meshcore>=2.2.1"
            )
            return
        try:
            backend = MeshCoreBackend(
                connection_type=cfg["transport"],
                serial_port=cfg.get("serial_port"),
                tcp_host=cfg.get("tcp_host", "192.168.1.1"),
                tcp_port=cfg.get("tcp_port", 4000),
                ble_address=cfg.get("ble_address"),
            )
            self._radio_manager.add_backend(backend)
            logger.info(
                f"Secondary radio connected: meshcore via {cfg['transport']}"
            )
        except Exception as e:
            logger.error(f"Failed to connect secondary meshcore radio: {e}")

    def _secondary_radio_ingest_loop(self):
        """Drain _radio_manager's message queue and feed into _request_queue.

        Runs forever in a background thread (see start()). Skips meshtastic
        messages — those flow through the proven pubsub path in _on_receive
        — so this loop in practice only processes meshcore traffic. Kept
        generic for when the primary path is eventually unified.
        """
        while self._running:
            msg = self._radio_manager.get_message(timeout=1.0)
            if msg is None:
                continue
            try:
                db_protocol = _PROTOCOL_SHORT_TO_DB.get(msg.protocol.value)
                if db_protocol is None:
                    logger.warning(
                        f"Unknown protocol from radio_manager: {msg.protocol!r}"
                    )
                    continue
                if db_protocol == "meshtastic":
                    # Handled by pubsub in _on_receive; skip to avoid double-persist
                    continue

                sender = msg.node.backend_native_id
                text = (msg.text or "").strip()
                channel = msg.channel if msg.channel is not None else 0
                is_dm = msg.is_dm

                if not text:
                    continue

                now = time.time()
                last = self._node_last_active.get(sender)
                if last is not None and (now - last) < RATE_LIMIT_SECS:
                    logger.info(
                        f"Rate-limited {sender}/{db_protocol} "
                        f"({now - last:.0f}s < {RATE_LIMIT_SECS}s cooldown)"
                    )
                    continue

                self._known_nodes.add(sender)
                self._node_count = len(self._known_nodes)
                ch_label = "DM" if is_dm else f"ch{channel}"
                logger.info(
                    f"[{db_protocol}] Received from {sender} ({ch_label}): {text[:100]}"
                )
                record_message("in", sender, text)

                self._persist_incoming(
                    db_protocol, sender, text, channel, is_dm,
                    rssi=msg.rssi, snr=msg.snr, hops=msg.hops_traveled,
                )

                # Bridge relay observation — may re-broadcast to Meshtastic.
                try:
                    self._relay.observe(db_protocol, sender, text, channel, is_dm)
                except Exception as e:
                    logger.warning(f"[bridge] relay.observe({db_protocol}) error: {e}")

                for addon in self._addons:
                    try:
                        addon.on_message(sender, text, msg.raw_packet or {})
                    except Exception as e:
                        logger.warning(
                            f"Addon {addon.name} on_message error: {e}"
                        )

                self._request_queue.put((db_protocol, sender, text, channel, is_dm))
            except Exception as e:
                logger.error(f"Error in secondary radio ingest: {e}")

    def _get_self_node_id(self) -> Optional[str]:
        """Best-effort derive the local node id in '!hex' form. Returns None
        if the meshtastic-python interface hasn't surfaced it yet.
        """
        if not self.interface:
            return None
        try:
            my_info = getattr(self.interface, "myInfo", None)
            num = None
            if my_info is not None:
                num = getattr(my_info, "my_node_num", None) or getattr(my_info, "myNodeNum", None)
            if num is None:
                local_node = getattr(self.interface, "localNode", None)
                if local_node is not None:
                    num = getattr(local_node, "nodeNum", None)
            if num is None:
                return None
            return f"!{int(num):08x}"
        except (AttributeError, TypeError, ValueError) as e:
            logger.debug(f"Could not derive self node id: {e}")
            return None

    def _processing_loop(self):
        """Process queued messages through Ollama and send responses."""
        while self._running:
            try:
                protocol, node_id, text, channel, is_dm = self._request_queue.get(timeout=1)
            except queue.Empty:
                continue

            try:
                self._message_count += 1
                self._node_last_active[node_id] = time.time()

                # Resolve per-contact AI state
                effective_ai = self._ai_replies_enabled
                try:
                    effective_ai = self._contact_store.get_effective_ai(
                        node_id, self._ai_replies_enabled
                    )
                    # For channel messages, check channel's AI state
                    if not is_dm:
                        chan_id = f"{protocol}:channel:{channel}"
                        effective_ai = self._contact_store.get_effective_ai(
                            chan_id, self._ai_replies_enabled
                        )
                except Exception:
                    pass

                # Check for commands (always process, regardless of AI toggle)
                response = self._handle_command(node_id, text)
                elapsed = 0
                chosen_tier = Tier.STANDARD  # default for commands

                if response is None:
                    # Check AI toggle — if off, log but don't reply
                    if not effective_ai:
                        logger.info(f"AI disabled for {node_id}, skipping reply")
                        continue

                    # ── Model routing ─────────────────────────────────────
                    # Parse !tiny/!std/!big prefix
                    override_tier, routed_text = parse_prefix(text)
                    if override_tier is not None and not routed_text:
                        # Prefix only, no query → usage hint
                        response = USAGE_HINT
                        chosen_tier = override_tier
                        elapsed = 0
                    else:
                        query_text = routed_text if override_tier else text
                        chosen_tier = Tier.STANDARD
                        chosen_model = self.ollama.model

                        try:
                            tiers = load_tiers(self._settings_store)
                            auto_routing = self._settings_store.get(ROUTING_AUTO_KEY, True)
                            installed = self.ollama.list_models()
                            chosen_tier, chosen_model = route(
                                query_text, tiers, installed,
                                override=override_tier,
                                auto_routing=bool(auto_routing),
                            )
                        except ModelNotConfiguredError as e:
                            response = str(e)
                            self._send_response(node_id, response, channel=0, is_dm=True, protocol=protocol)
                            record_message("out", node_id, response)
                            continue
                        except Exception as e:
                            logger.warning(f"Routing error, using default model: {e}")

                        logger.info(f"Processing query from {node_id} [{chosen_tier.value.upper()}] → {chosen_model}")

                        # Send acknowledgment as DM to sender
                        self._send_raw(node_id, "Thinking...", channel=0, is_dm=True, protocol=protocol)

                        # RAG: tier-aware context size
                        context_messages = None
                        max_rag_chunks = 3  # default
                        if chosen_tier == Tier.TINY:
                            max_rag_chunks = 0  # skip RAG for tiny
                        elif chosen_tier == Tier.BIG:
                            max_rag_chunks = 6  # more context for big
                        if (
                            max_rag_chunks > 0
                            and self.rag_enabled
                            and self.rag_engine
                            and node_id not in self._rag_disabled_nodes
                        ):
                            try:
                                context_messages = self.rag_engine.build_context_messages(
                                    query_text, top_k=max_rag_chunks
                                )
                                if context_messages:
                                    logger.info(
                                        f"RAG: injecting {len(context_messages)} context chunk(s)"
                                    )
                            except Exception as e:
                                logger.warning(f"RAG search failed: {e}")

                        start = time.time()
                        response = self.ollama.chat(
                            node_id, query_text,
                            context_messages=context_messages,
                            model_override=chosen_model,
                        )
                        elapsed = time.time() - start
                        logger.info(f"Ollama [{chosen_tier.value.upper()}] responded in {elapsed:.1f}s ({len(response)} chars)")

                # Determine where to store + send the reply
                reply_contact_id = node_id  # default: sender's DM thread
                originating_channel = None
                reply_is_dm = True  # always DM the reply

                if not is_dm:
                    # Channel message → AI reply goes to sender as DM
                    originating_channel = f"{protocol}:channel:{channel}"
                    logger.info(f"Channel AI redirect: replying to {node_id} as DM (from ch{channel})")

                # Send response as DM to sender (never to channel)
                tier_tag = chosen_tier.value if elapsed > 0 else None
                record_message("out", node_id, response, chunks=1, llm_time=elapsed,
                               tier=tier_tag)
                try:
                    author = "ai" if elapsed > 0 else "human"
                    self._message_store.insert(
                        contact_id=reply_contact_id, direction="out", author=author,
                        text=response, protocol=protocol,
                        originating_channel_id=originating_channel,
                    )
                except Exception as e:
                    logger.debug(f"DB persist (out) error: {e}")
                update_state(
                    message_count=self._message_count,
                    node_count=self._node_count,
                    known_nodes=list(self._known_nodes),
                )
                self._send_response(node_id, response, channel=0, is_dm=reply_is_dm, protocol=protocol)

            except Exception as e:
                logger.error(f"Error processing message from {node_id}: {e}")
                self._send_response(node_id, f"Error: {e}", channel=0, is_dm=True, protocol=protocol)

    def _init_commands(self):
        """Register built-in commands into the command registry."""
        self._command_registry["!help"] = (self._cmd_help, "this message")
        self._command_registry["!status"] = (self._cmd_status, "bridge info")
        self._command_registry["!model"] = (self._cmd_model, "<name> - switch model")
        self._command_registry["!models"] = (self._cmd_models, "list models")
        self._command_registry["!clear"] = (self._cmd_clear, "reset conversation")
        self._command_registry["!ping"] = (self._cmd_ping, "connectivity test")
        self._command_registry["!more"] = (self._cmd_more, "next page of long response")
        if self.rag_enabled:
            self._command_registry["!rag"] = (self._cmd_rag, "on/off - toggle knowledge base")
            self._command_registry["!docs"] = (self._cmd_docs, "list documents")

    def _cmd_help(self, node_id: str, arg: str) -> str:
        parts = []
        for cmd, (_handler, help_text) in sorted(self._command_registry.items()):
            parts.append(f"{cmd} - {help_text}")
        return "Commands: " + ", ".join(parts)

    def _cmd_status(self, node_id: str, arg: str) -> str:
        uptime = int(time.time() - self._start_time)
        h, rem = divmod(uptime, 3600)
        m, s = divmod(rem, 60)
        status = (
            f"Model: {self.ollama.model}, "
            f"Uptime: {h}h{m}m{s}s, "
            f"Nodes: {self._node_count}, "
            f"Messages: {self._message_count}"
        )
        if self.rag_enabled and self.rag_engine:
            stats = self.rag_engine.get_stats()
            rag_active = "on" if node_id not in self._rag_disabled_nodes else "off"
            status += (
                f", RAG: {rag_active} "
                f"({stats['total_docs']} docs, {stats['total_chunks']} chunks)"
            )
        # Show active addons
        if self._addons:
            addon_names = [a.display_name for a in self._addons]
            status += f", Addons: {', '.join(addon_names)}"
        return status

    def _cmd_model(self, node_id: str, arg: str) -> str:
        if not arg:
            return f"Current model: {self.ollama.model}. Usage: !model <name>"
        if self.ollama.set_model(arg):
            return f"Switched to model: {self.ollama.model}"
        models = self.ollama.list_models()
        return f"Model '{arg}' not found. Available: {', '.join(models[:5])}"

    def _cmd_models(self, node_id: str, arg: str) -> str:
        models = self.ollama.list_models()
        if models:
            return f"Models: {', '.join(models)}"
        return "No models installed."

    def _cmd_clear(self, node_id: str, arg: str) -> str:
        self.ollama.clear_history(node_id)
        return "Conversation history cleared."

    def _cmd_ping(self, node_id: str, arg: str) -> str:
        return "pong"

    def _cmd_more(self, node_id: str, arg: str) -> str:
        entry = self._overflow.get(node_id)
        if not entry:
            return "No more content."
        remaining, _ts = entry
        del self._overflow[node_id]
        return remaining

    def _cmd_rag(self, node_id: str, arg: str) -> str:
        if not self.rag_enabled:
            return "RAG not enabled. Start bridge with --rag flag."
        if arg.lower() == "on":
            self._rag_disabled_nodes.discard(node_id)
            return "Knowledge base enabled for your node."
        elif arg.lower() == "off":
            self._rag_disabled_nodes.add(node_id)
            return "Knowledge base disabled for your node."
        else:
            active = "on" if node_id not in self._rag_disabled_nodes else "off"
            stats = self.rag_engine.get_stats()
            return (
                f"RAG: {active} for your node. "
                f"{stats['total_docs']} docs, {stats['total_chunks']} chunks. "
                f"Usage: !rag on/off"
            )

    def _cmd_docs(self, node_id: str, arg: str) -> str:
        if not self.rag_enabled or not self.rag_engine:
            return "RAG not enabled. Start bridge with --rag flag."
        docs = self.rag_engine.list_documents()
        if not docs:
            return "No documents in knowledge base."
        lines = [f"{d['filename']} ({d['chunk_count']} chunks)" for d in docs[:10]]
        result = "Docs: " + ", ".join(lines)
        if len(docs) > 10:
            result += f" ...and {len(docs) - 10} more"
        return result

    def _handle_command(self, node_id: str, text: str) -> Optional[str]:
        """Handle ! commands via registry lookup. Returns response or None."""
        if not text.startswith("!"):
            return None

        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        entry = self._command_registry.get(cmd)
        if entry:
            handler, _help = entry
            return handler(node_id, arg)

        return None  # Unknown ! prefix — treat as regular message

    def _is_interface_alive(self) -> bool:
        """Quick check if the radio interface is still usable."""
        try:
            if self.interface is None:
                return False
            # For serial: check stream is not None (used by _writeBytes)
            if hasattr(self.interface, "stream") and self.interface.stream is None:
                return False
            # For TCP: check socket is not None (used by _writeBytes)
            if hasattr(self.interface, "socket") and self.interface.socket is None:
                return False
            # For BLE: check the client is still connected
            if BLE_AVAILABLE and isinstance(self.interface, meshtastic.ble_interface.BLEInterface):
                if hasattr(self.interface, "client") and self.interface.client is None:
                    return False
            # Verify localNode is accessible (used by _sendPacket for hop_limit)
            _ = self.interface.localNode
            return True
        except (AttributeError, OSError) as e:
            logger.debug(f"Interface health check failed: {e}")
            return False

    def _reconnect_radio(self):
        """Close the current interface and reconnect."""
        logger.warning("Reconnecting to radio...")
        update_state(connected=False)
        if self.interface:
            try:
                self.interface.close()
            except Exception as e:
                logger.debug(f"Error closing interface during reconnect: {e}")
            self.interface = None
        self._connect_radio()

    def _send_raw(
        self,
        node_id: str,
        text: str,
        channel: int = 0,
        is_dm: bool = True,
        protocol: str = "meshtastic",
    ):
        """Send a short status message (no pager, no overflow).

        Used for 'Thinking...' and other ephemeral status indicators.
        Replies on the same channel the original message came from.
        Includes a post-TX settle delay so the radio returns to RX mode.

        When ``protocol == "meshcore"``, routes via the RadioManager using
        the unified ``mc:`` prefix instead of the Meshtastic interface.
        """
        if protocol == "meshcore":
            try:
                self._radio_manager.send(
                    f"mc:{node_id}", text, channel=channel, is_dm=is_dm,
                )
                ch_label = "DM" if is_dm else f"ch{channel}"
                logger.info(f"[meshcore] Status to {node_id} ({ch_label}): {text}")
            except Exception as e:
                logger.warning(
                    f"Failed to send meshcore status to {node_id}: {e}"
                )
            return
        if not self.interface or not self._is_interface_alive():
            logger.warning(
                f"Status drop to {node_id}: interface not alive ({text[:40]!r})"
            )
            return
        try:
            dest = node_id if is_dm else BROADCAST_ADDR
            want_ack = os.environ.get("DEBUG_WANT_ACK") == "1"
            self.interface.sendText(
                text, destinationId=dest, channelIndex=channel, wantAck=want_ack,
            )
            ch_label = "DM" if is_dm else f"ch{channel}"
            logger.info(f"Status to {node_id} ({ch_label}): {text}")
            time.sleep(3)  # Post-TX settle — let radio return to RX
        except Exception as e:
            logger.warning(f"Failed to send status to {node_id}: {e}")

    def _send_response(
        self,
        node_id: str,
        content: str,
        channel: int = 0,
        is_dm: bool = True,
        protocol: str = "meshtastic",
    ):
        """Send a single plain-text response over the mesh.

        The Meshtastic app groups messages by sender and only shows the latest,
        so we ALWAYS send exactly one message. If the response is too long, we
        truncate at a sentence boundary and store the overflow for the !more
        command (interactive pager).

        Replies on the same channel the original message came from:
        DMs get a DM back, public channel messages get a public reply.

        When ``protocol == "meshcore"``, routes via the RadioManager using
        the unified ``mc:`` prefix. Phase 1 behavior: meshcore sends use a
        simple truncation at MAX_LORA_TEXT — no paging / overflow /
        !more support yet (deferred to Phase 2 of LORACLE v2).
        """
        if protocol == "meshcore":
            content_bytes = content.encode("utf-8")
            if len(content_bytes) > MAX_LORA_TEXT:
                truncated = content
                while (
                    len(truncated.encode("utf-8")) > MAX_LORA_TEXT
                    and len(truncated) > 1
                ):
                    truncated = truncated[:-1]
                message = truncated
                logger.info(
                    f"[meshcore] Response truncated to {len(message)} chars "
                    "(Phase 1: no paging)"
                )
            else:
                message = content
            try:
                self._radio_manager.send(
                    f"mc:{node_id}", message, channel=channel, is_dm=is_dm,
                )
                ch_label = "DM" if is_dm else f"ch{channel}"
                logger.info(
                    f"[meshcore] Sent to {node_id} ({ch_label}): "
                    f"{len(message.encode('utf-8'))} bytes"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to send meshcore response to {node_id}: {e}"
                )
            return
        if not self.interface:
            logger.error("No radio interface — cannot send")
            return

        MORE_HINT = "... (!more)"
        READY_HINT = " [End]"
        content_bytes = content.encode("utf-8")

        if len(content_bytes) <= MAX_LORA_TEXT:
            # Fits in one message — clear any previous overflow
            self._overflow.pop(node_id, None)
            # Append ready hint if it still fits
            with_hint = content + READY_HINT
            if len(with_hint.encode("utf-8")) <= MAX_LORA_TEXT:
                message = with_hint
            else:
                message = content
        else:
            # Too long — truncate at sentence boundary, store overflow
            budget = MAX_LORA_TEXT - len(MORE_HINT.encode("utf-8"))

            # Find the cut point (don't break mid-character)
            truncated = content
            while len(truncated.encode("utf-8")) > budget and len(truncated) > 1:
                truncated = truncated[: len(truncated) - 1]

            # Try to break at last sentence boundary (keep >40% of content)
            min_keep = len(truncated) * 2 // 5
            best_break = -1
            for sep in [". ", "! ", "? "]:
                idx = truncated.rfind(sep)
                if idx > min_keep:
                    best_break = max(best_break, idx + len(sep))
            if best_break > 0:
                truncated = truncated[:best_break]

            message = truncated.rstrip() + MORE_HINT
            remaining = content[len(truncated):].lstrip()
            self._overflow[node_id] = (remaining, time.time())
            logger.info(
                f"Response paged: sending {len(message)} bytes, "
                f"{len(remaining)} chars remaining for !more"
            )

        logger.info(f"Sending to {node_id}: {len(message)} bytes")

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            if not self._is_interface_alive():
                logger.warning(
                    f"Interface not alive before send (attempt {attempt}/{max_retries})"
                )
                if attempt < max_retries:
                    self._reconnect_radio()
                    continue
                else:
                    logger.error("Interface dead after all retries, giving up")
                    return

            try:
                dest = node_id if is_dm else BROADCAST_ADDR
                want_ack = os.environ.get("DEBUG_WANT_ACK") == "1"
                result = self.interface.sendText(
                    message,
                    destinationId=dest,
                    channelIndex=channel,
                    wantAck=want_ack,
                )
                pkt_id = getattr(result, "id", None)
                ch_label = "DM" if is_dm else f"ch{channel}"
                ack_label = " wantAck=1" if want_ack else ""
                logger.info(
                    f"Sent to {node_id} ({ch_label}, pkt_id={pkt_id}, attempt={attempt}){ack_label}"
                )
                time.sleep(3)  # Post-TX settle — let radio return to RX
                return  # Success

            except Exception as e:
                logger.error(
                    f"Failed to send to {node_id} "
                    f"(attempt {attempt}/{max_retries}): {e}"
                )
                if attempt < max_retries:
                    if attempt == max_retries - 1:
                        self._reconnect_radio()
                    else:
                        time.sleep(1)

        logger.error(f"All send retries exhausted for {node_id}")

    def _dedup_cleanup_loop(self):
        """Clean up expired dedup cache entries, stale overflow, and prune messages."""
        last_prune = time.time()
        while self._running:
            time.sleep(60)
            now = time.time()
            expired = [k for k, v in _dedup_cache.items() if now - v > DEDUP_TTL]
            for k in expired:
                del _dedup_cache[k]
            # Evict stale !more overflow entries
            stale_overflow = [
                nid for nid, (_, ts) in self._overflow.items()
                if now - ts > OVERFLOW_TTL
            ]
            for nid in stale_overflow:
                del self._overflow[nid]
            if stale_overflow:
                logger.debug(f"Evicted {len(stale_overflow)} stale overflow entries")
            # Prune message DB every hour
            if now - last_prune > 3600:
                last_prune = now
                try:
                    self._message_store.prune()
                except Exception as e:
                    logger.debug(f"Message prune error: {e}")

    def _context_cleanup_loop(self):
        """Auto-clear conversation context for nodes idle longer than CONTEXT_TTL."""
        while self._running:
            time.sleep(120)  # Check every 2 minutes
            now = time.time()
            stale = [
                nid for nid, ts in self._node_last_active.items()
                if now - ts > CONTEXT_TTL
            ]
            for nid in stale:
                self.ollama.clear_history(nid)
                del self._node_last_active[nid]
                if nid in self._rag_disabled_nodes:
                    self._rag_disabled_nodes.discard(nid)
                logger.info(f"Auto-cleared context for {nid} (idle >{CONTEXT_TTL//60}m)")

    def _node_sync_loop(self):
        """Periodically merge node positions from all backends."""
        while self._running:
            time.sleep(NODEDB_REFRESH_INTERVAL_S)
            try:
                positions = self._radio_manager.get_all_node_positions()
                for uid, pos in positions.items():
                    if uid not in self._node_positions:
                        self._node_positions[uid] = pos
                    else:
                        # Update if newer
                        existing_ts = self._node_positions[uid].get("last_update", 0)
                        new_ts = pos.get("last_update", 0)
                        if new_ts > existing_ts:
                            self._node_positions[uid] = pos
                meta = self._radio_manager.get_all_node_meta()
                for uid, m in meta.items():
                    self._node_meta[uid] = m
            except Exception as e:
                logger.debug(f"Node sync error: {e}")

    def _cleanup(self):
        """Clean up resources."""
        # Stop addons
        for addon in self._addons:
            try:
                addon.on_stop()
            except Exception as e:
                logger.warning(f"Addon {addon.name} on_stop error: {e}")

        # Stop all radio backends
        try:
            self._radio_manager.stop_all()
        except Exception as e:
            logger.debug(f"Error stopping radio backends: {e}")

        if self.interface:
            try:
                self.interface.close()
            except Exception as e:
                logger.debug(f"Error closing interface during shutdown: {e}")
        logger.info("Bridge shut down")


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Standalone LORACLE BRIDGE — chat with a local AI over mesh radio"
    )

    conn = parser.add_mutually_exclusive_group()
    conn.add_argument(
        "--ble",
        metavar="ADDRESS",
        nargs="?",
        const="",
        help="Connect via Bluetooth LE (optionally specify device address)",
    )
    conn.add_argument(
        "--serial",
        metavar="PORT",
        help="Serial port for Meshtastic device (default: auto-detect)",
    )
    conn.add_argument(
        "--tcp",
        metavar="HOST:PORT",
        help="TCP address for Meshtastic device (e.g., 192.168.1.100:4403)",
    )

    parser.add_argument(
        "--model",
        default=None,
        help="Ollama model to use (default: auto-select based on RAM)",
    )
    parser.add_argument(
        "--ollama-url",
        default="http://localhost:11434",
        help="Ollama API URL (default: http://localhost:11434)",
    )
    parser.add_argument(
        "--max-length",
        type=int,
        default=200,
        help="Max response characters (default: 200)",
    )
    parser.add_argument(
        "--system-prompt",
        help="Custom system prompt for the LLM",
    )
    parser.add_argument(
        "--no-compression",
        action="store_true",
        help="Disable zlib compression for chunks",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="List available Ollama models and exit",
    )
    parser.add_argument(
        "--rag",
        action="store_true",
        default=True,
        help="Enable RAG knowledge base (default: on)",
    )
    parser.add_argument(
        "--no-rag",
        action="store_true",
        help="Disable RAG knowledge base",
    )
    parser.add_argument(
        "--rag-dir",
        default=None,
        help="RAG data directory (default: ~/.mesh-llm/rag)",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8000,
        help="Web dashboard port (default: 8000)",
    )

    # Addon flags
    parser.add_argument(
        "--enable-dead-drop",
        action="store_true",
        default=True,
        help="Enable Dead Drop encrypted async messaging",
    )
    parser.add_argument(
        "--enable-triage",
        action="store_true",
        default=True,
        help="Enable Triage offline medical reference",
    )
    parser.add_argument(
        "--triage-dir",
        default=None,
        help="Triage medical knowledge base directory (default: ~/.mesh-llm/triage)",
    )
    parser.add_argument(
        "--enable-brief",
        action="store_true",
        default=True,
        help="Enable Brief AI-generated situation reports",
    )
    parser.add_argument(
        "--brief-interval",
        type=int,
        default=60,
        help="Brief SITREP generation interval in minutes (default: 60)",
    )
    parser.add_argument(
        "--enable-navigation",
        action="store_true",
        default=True,
        help="Enable Navigation bearing/distance helper",
    )
    parser.add_argument(
        "--enable-all-addons",
        action="store_true",
        default=True,
        help="Enable all available addons",
    )
    parser.add_argument(
        "--auto-greet",
        dest="auto_greet",
        action="store_true",
        default=True,
        help="Auto-DM new mesh nodes a brief welcome (default: on)",
    )
    parser.add_argument(
        "--no-auto-greet",
        dest="auto_greet",
        action="store_false",
        help="Disable the auto-greet welcome DM",
    )
    parser.add_argument(
        "--greet-message",
        default=None,
        help="Override the auto-greet message text (default: built-in)",
    )
    parser.add_argument(
        "--public-talk",
        dest="public_talk",
        action="store_true",
        default=True,
        help="Respond to public-channel messages that address LORACLE by name (default: on)",
    )
    parser.add_argument(
        "--no-public-talk",
        dest="public_talk",
        action="store_false",
        help="Disable public-channel replies — DM only",
    )

    # ── Protocol / multi-radio flags ─────────────────────────────────────
    parser.add_argument(
        "--protocol",
        choices=["auto", "meshtastic", "meshcore"],
        default="auto",
        help="Primary radio protocol (default: auto-detect)",
    )
    parser.add_argument(
        "--second-radio",
        metavar="PROTOCOL:TRANSPORT:PARAMS",
        default=None,
        help=(
            "Connect a second radio. Format: protocol:transport:params. "
            "Examples: meshcore:serial:/dev/ttyUSB1, meshtastic:tcp:192.168.1.50:4403"
        ),
    )
    parser.add_argument(
        "--ai-replies",
        choices=["on", "off"],
        default=None,
        help="Global AI auto-reply toggle (default: on). When off, logs messages but stays quiet.",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    # List models mode
    if args.list_models:
        client = OllamaClient(base_url=args.ollama_url)
        if not client.is_available():
            print(f"Cannot connect to Ollama at {args.ollama_url}")
            sys.exit(1)
        models = client.list_models()
        ram_gb = get_system_ram_gb()
        if models:
            print("Installed models:")
            for m in models:
                print(f"  {m}")
        else:
            print("No models installed.")
        print(f"\nRecommended models (system has {ram_gb}GB RAM):")
        for p in MODEL_PROFILES:
            fits = "OK" if ram_gb >= p["min_ram_gb"] else f"needs {p['min_ram_gb']}GB+"
            installed = "installed" if any(
                m == p["name"] or m.startswith(f"{p['name'].split(':')[0]}:")
                for m in models
            ) else "not installed"
            print(f"  {p['name']:16s}  [{p['tier']:6s}]  {fits:12s}  {installed}")
            print(f"    {p['description']}")
        sys.exit(0)

    # Determine connection type
    connection_type = "serial"
    serial_port = None
    tcp_host = "192.168.1.1"
    tcp_port = 4403
    ble_address = None

    if args.ble is not None:
        connection_type = "ble"
        ble_address = args.ble if args.ble else None
    elif args.tcp:
        connection_type = "tcp"
        if ":" in args.tcp:
            tcp_host, tcp_port_str = args.tcp.rsplit(":", 1)
            tcp_port = int(tcp_port_str)
        else:
            tcp_host = args.tcp
    elif args.serial:
        serial_port = args.serial

    # Auto-select model based on RAM (unless user explicitly chose one)
    selected_model, profile_prompt, needs_pull = auto_select_model(
        ollama_url=args.ollama_url,
        user_model=args.model,
    )

    # If a better model needs pulling, start with whatever's installed NOW
    # and pull the upgrade in the background (so dashboard comes up immediately)
    upgrade_model = None
    upgrade_prompt = None
    if needs_pull:
        upgrade_model = selected_model
        upgrade_prompt = profile_prompt
        # Fall back to best installed model for immediate startup
        selected_model = "gemma3:4b"
        profile_prompt = None
        logger.info(
            f"Starting with {selected_model} while pulling {upgrade_model} in background..."
        )

    # Use profile prompt unless user provided a custom --system-prompt
    effective_prompt = args.system_prompt or profile_prompt

    # Build addon config from CLI flags
    addon_config = {}
    if args.enable_all_addons or args.enable_dead_drop:
        addon_config["dead_drop"] = {}
    if args.enable_all_addons or args.enable_triage:
        addon_config["triage"] = {
            "data_dir": args.triage_dir,
            "ollama_url": args.ollama_url,
        }
    if args.enable_all_addons or args.enable_brief:
        addon_config["brief"] = {
            "interval_minutes": args.brief_interval,
        }
    if args.enable_all_addons or args.enable_navigation:
        addon_config["navigation"] = {}

    # AI replies toggle (CLI > settings file > default)
    settings = load_settings()
    ai_replies = settings.get("ai_replies", True)
    if args.ai_replies is not None:
        ai_replies = args.ai_replies == "on"

    bridge = StandaloneBridge(
        connection_type=connection_type,
        serial_port=serial_port,
        tcp_host=tcp_host,
        tcp_port=tcp_port,
        ble_address=ble_address,
        ollama_url=args.ollama_url,
        model=selected_model,
        max_response_length=args.max_length,
        system_prompt=effective_prompt,
        compression_enabled=not args.no_compression,
        rag_enabled=not args.no_rag,
        rag_dir=args.rag_dir,
        dashboard_port=args.dashboard_port,
        addon_config=addon_config if addon_config else None,
        auto_greet=args.auto_greet,
        greet_message=args.greet_message,
        public_talk=args.public_talk,
        protocol=args.protocol,
        ai_replies_enabled=ai_replies,
        second_radio=args.second_radio,
    )

    # Pull the better model in background after bridge starts
    if upgrade_model:
        def _background_pull():
            logger.info(f"Background: pulling {upgrade_model}...")
            if bridge.ollama.pull_model(upgrade_model):
                old = bridge.ollama.model
                bridge.ollama.set_model(upgrade_model)
                # Apply the tuned prompt if user didn't provide a custom one
                if not args.system_prompt and upgrade_prompt:
                    bridge.ollama.system_prompt = upgrade_prompt
                logger.info(
                    f"Model upgraded: {old} -> {bridge.ollama.model} "
                    "(better model for your RAM)"
                )
                update_state(model=bridge.ollama.model)
            else:
                logger.warning(
                    f"Failed to pull {upgrade_model}. Continuing with {bridge.ollama.model}"
                )

        threading.Thread(target=_background_pull, daemon=True).start()

    bridge.start()


if __name__ == "__main__":
    main()
