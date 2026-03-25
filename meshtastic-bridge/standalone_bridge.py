"""Standalone LORACLE.

Connects directly to a Meshtastic radio and Ollama — no N.O.M.A.D. stack required.
Receives text messages over the mesh, processes them through a local LLM,
and sends chunked responses back over LoRa.

Usage:
    python standalone_bridge.py
    python standalone_bridge.py --ble                              # Bluetooth LE
    python standalone_bridge.py --model mistral --serial /dev/cu.usbserial-0001
    python standalone_bridge.py --tcp 192.168.1.100:4403
    python standalone_bridge.py --list-models
"""

import argparse
import glob
import hashlib
import logging
import queue
import sys
import threading
import time
from typing import Dict, Optional, Set, Tuple

import meshtastic
import meshtastic.serial_interface
import meshtastic.tcp_interface
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

from ollama_client import OllamaClient
from protocol import chunk_message
from dashboard import start_dashboard, record_message, update_state, set_bridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("standalone")

# Suppress noisy meshtastic protobuf parsing errors (firmware/library mismatch)
logging.getLogger("meshtastic.mesh_interface").setLevel(logging.CRITICAL)
logging.getLogger("meshtastic.stream_interface").setLevel(logging.CRITICAL)

# Max bytes for a single LoRa text message
MAX_LORA_TEXT = 233

# Deduplication cache: (node_id, content_hash) -> timestamp
_dedup_cache = {}  # type: Dict[Tuple[str, str], float]
DEDUP_TTL = 300  # 5 minutes


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


class StandaloneBridge:
    """LORACLE — LoRa + Oracle mesh AI bridge."""

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
        inter_chunk_delay: float = 3.0,
        rag_enabled: bool = True,
        rag_dir: Optional[str] = None,
        dashboard_port: int = 8000,
    ):
        self.connection_type = connection_type
        self.serial_port = serial_port
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.ble_address = ble_address
        self.dashboard_port = dashboard_port
        self.compression_enabled = compression_enabled
        self.inter_chunk_delay = inter_chunk_delay
        self.interface = None
        self._running = False
        self._reconnect_delay = 1
        self._request_queue: queue.Queue = queue.Queue()
        self._start_time = time.time()
        self._message_count = 0
        self._node_count = 0
        self._known_nodes = set()  # type: Set[str]

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

    def start(self):
        """Start the bridge."""
        logger.info("Starting Standalone LORACLE")

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

        # Connect to radio
        self._connect_radio()
        update_state(connected=True)

        # Firmware / library version check
        self._check_firmware()

        # Register message handler
        pub.subscribe(self._on_receive, "meshtastic.receive.text")

        self._running = True

        # Start background threads
        threading.Thread(target=self._processing_loop, daemon=True).start()
        threading.Thread(target=self._dedup_cleanup_loop, daemon=True).start()

        logger.info("Bridge ready — waiting for mesh messages")

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
        except Exception:
            pass

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
        except Exception:
            pass

    def _connect_radio(self):
        """Connect to the Meshtastic device with retry."""
        while self._running or self._reconnect_delay == 1:
            try:
                if self.connection_type == "ble":
                    if not BLE_AVAILABLE:
                        logger.error(
                            "BLE not available. Requires Python 3.11+ with bleak installed.\n"
                            "Fix: brew install python@3.12 && rm -rf venv && ./mesh-llm.sh --ble"
                        )
                        sys.exit(1)
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
                        logger.error(
                            "No Meshtastic device found. Connect a radio via USB "
                            "or specify --serial <port>, --tcp <host:port>, or --ble"
                        )
                        sys.exit(1)
                    logger.info(f"Connecting via serial: {port}")
                    self.interface = meshtastic.serial_interface.SerialInterface(
                        devPath=port
                    )

                self._reconnect_delay = 1
                logger.info("Connected to Meshtastic device")
                update_state(connected=True)
                return

            except Exception as e:
                logger.error(
                    f"Connection failed: {e}. Retrying in {self._reconnect_delay}s..."
                )
                update_state(connected=False)
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    def _on_receive(self, packet, interface):
        """Handle incoming text message from the mesh."""
        try:
            sender = packet.get("fromId", "unknown")
            text = packet.get("decoded", {}).get("text", "")

            if not text or not text.strip():
                return

            # Deduplication
            content_hash = hashlib.md5(text.encode()).hexdigest()[:8]
            cache_key = (sender, content_hash)
            now = time.time()

            if cache_key in _dedup_cache and (now - _dedup_cache[cache_key]) < DEDUP_TTL:
                logger.debug(f"Duplicate from {sender}, ignoring")
                return

            _dedup_cache[cache_key] = now

            # Track nodes
            self._known_nodes.add(sender)
            self._node_count = len(self._known_nodes)

            logger.info(f"Received from {sender}: {text[:100]}")
            record_message("in", sender, text.strip())
            self._request_queue.put((sender, text.strip()))

        except Exception as e:
            logger.error(f"Error processing incoming message: {e}")

    def _processing_loop(self):
        """Process queued messages through Ollama and send responses."""
        while self._running:
            try:
                node_id, text = self._request_queue.get(timeout=1)
            except queue.Empty:
                continue

            try:
                self._message_count += 1

                # Check for commands
                response = self._handle_command(node_id, text)
                elapsed = 0

                if response is None:
                    # Regular message — send to Ollama
                    logger.info(f"Processing query from {node_id}...")

                    # RAG: search for relevant context
                    context_messages = None
                    if (
                        self.rag_enabled
                        and self.rag_engine
                        and node_id not in self._rag_disabled_nodes
                    ):
                        try:
                            context_messages = self.rag_engine.build_context_messages(text)
                            if context_messages:
                                logger.info(
                                    f"RAG: injecting {len(context_messages)} context chunk(s)"
                                )
                        except Exception as e:
                            logger.warning(f"RAG search failed: {e}")

                    start = time.time()
                    response = self.ollama.chat(
                        node_id, text, context_messages=context_messages
                    )
                    elapsed = time.time() - start
                    logger.info(f"Ollama responded in {elapsed:.1f}s ({len(response)} chars)")

                # Send response
                content_bytes = response.encode("utf-8")
                num_parts = max(1, (len(content_bytes) + MAX_LORA_TEXT - 1) // MAX_LORA_TEXT)
                record_message("out", node_id, response, chunks=num_parts, llm_time=elapsed)
                update_state(
                    message_count=self._message_count,
                    node_count=self._node_count,
                    known_nodes=list(self._known_nodes),
                )
                self._send_response(node_id, response)

            except Exception as e:
                logger.error(f"Error processing message from {node_id}: {e}")
                self._send_response(node_id, f"Error: {e}")

    def _handle_command(self, node_id: str, text: str) -> Optional[str]:
        """Handle ! commands. Returns response string or None for regular messages."""
        if not text.startswith("!"):
            return None

        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "!help":
            help_text = (
                "Commands: "
                "!help - this message, "
                "!status - bridge info, "
                "!model <name> - switch model, "
                "!models - list models, "
                "!clear - reset conversation, "
                "!ping - connectivity test"
            )
            if self.rag_enabled:
                help_text += (
                    ", !rag on/off - toggle knowledge base, "
                    "!docs - list documents"
                )
            return help_text

        elif cmd == "!status":
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
            return status

        elif cmd == "!model":
            if not arg:
                return f"Current model: {self.ollama.model}. Usage: !model <name>"
            if self.ollama.set_model(arg):
                return f"Switched to model: {self.ollama.model}"
            else:
                models = self.ollama.list_models()
                return f"Model '{arg}' not found. Available: {', '.join(models[:5])}"

        elif cmd == "!models":
            models = self.ollama.list_models()
            if models:
                return f"Models: {', '.join(models)}"
            return "No models installed."

        elif cmd == "!clear":
            self.ollama.clear_history(node_id)
            return "Conversation history cleared."

        elif cmd == "!ping":
            return "pong"

        elif cmd == "!rag":
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

        elif cmd == "!docs":
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

        else:
            return None  # Unknown ! prefix — treat as regular message

    def _send_response(self, node_id: str, content: str):
        """Send a plain-text response over the mesh.

        Uses sendText() so responses are readable on any standard Meshtastic
        device or app. Long responses are split into numbered parts.
        """
        if not self.interface:
            logger.error("No radio interface — cannot send")
            return

        # Split into LoRa-sized plain-text parts
        content_bytes = content.encode("utf-8")
        if len(content_bytes) <= MAX_LORA_TEXT:
            parts = [content]
        else:
            # Split into numbered parts that fit in MAX_LORA_TEXT bytes
            parts = []
            remaining = content
            while remaining:
                # Estimate how many parts we'll need (for the [X/N] prefix)
                est_total = max(1, (len(remaining.encode("utf-8")) + MAX_LORA_TEXT - 1) // (MAX_LORA_TEXT - 8))
                prefix_len = len(f"[{len(parts)+1}/{est_total}] ".encode("utf-8"))
                budget = MAX_LORA_TEXT - prefix_len

                # Find the split point (don't break mid-character)
                chunk_text = remaining
                while len(chunk_text.encode("utf-8")) > budget and len(chunk_text) > 1:
                    chunk_text = chunk_text[:len(chunk_text) - 1]

                parts.append(chunk_text)
                remaining = remaining[len(chunk_text):]

            # Add part numbers
            total = len(parts)
            if total > 1:
                parts = [f"[{i+1}/{total}] {p}" for i, p in enumerate(parts)]

        logger.info(f"Sending to {node_id}: {len(content)} chars, {len(parts)} part(s)")

        for i, part in enumerate(parts):
            try:
                self.interface.sendText(
                    part,
                    destinationId=node_id,
                )
                logger.debug(f"Sent part {i + 1}/{len(parts)} to {node_id}")

                if i < len(parts) - 1:
                    time.sleep(self.inter_chunk_delay)

            except Exception as e:
                logger.error(f"Failed to send part {i + 1}/{len(parts)}: {e}")
                return

        logger.info(f"Response sent to {node_id}")

    def _dedup_cleanup_loop(self):
        """Clean up expired deduplication cache entries."""
        while self._running:
            time.sleep(60)
            now = time.time()
            expired = [k for k, v in _dedup_cache.items() if now - v > DEDUP_TTL]
            for k in expired:
                del _dedup_cache[k]

    def _cleanup(self):
        """Clean up resources."""
        if self.interface:
            try:
                self.interface.close()
            except Exception:
                pass
        logger.info("Bridge shut down")


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Standalone LORACLE — chat with a local AI over mesh radio"
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
        default="gemma3:4b",
        help="Ollama model to use (default: gemma3:4b)",
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
        "--chunk-delay",
        type=float,
        default=3.0,
        help="Seconds between chunks (default: 3.0)",
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
        if models:
            print("Installed models:")
            for m in models:
                print(f"  {m}")
        else:
            print("No models installed. Run: ollama pull gemma3:4b")
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

    bridge = StandaloneBridge(
        connection_type=connection_type,
        serial_port=serial_port,
        tcp_host=tcp_host,
        tcp_port=tcp_port,
        ble_address=ble_address,
        ollama_url=args.ollama_url,
        model=args.model,
        max_response_length=args.max_length,
        system_prompt=args.system_prompt,
        compression_enabled=not args.no_compression,
        inter_chunk_delay=args.chunk_delay,
        rag_enabled=not args.no_rag,
        rag_dir=args.rag_dir,
        dashboard_port=args.dashboard_port,
    )

    bridge.start()


if __name__ == "__main__":
    main()
