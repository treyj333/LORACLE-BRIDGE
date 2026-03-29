"""LORACLE BRIDGE — Offline AI Over Mesh Radio.

Connects directly to a Meshtastic radio and Ollama.
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
    register_addon_tab, register_addon_api_route,
)
from addons import load_addons

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("standalone")

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
    ):
        self.connection_type = connection_type
        self.serial_port = serial_port
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.ble_address = ble_address
        self.dashboard_port = dashboard_port
        self.compression_enabled = compression_enabled
        self.interface = None
        self._running = False
        self._reconnect_delay = 1
        self._request_queue: queue.Queue = queue.Queue()
        self._start_time = time.time()
        self._message_count = 0
        self._node_count = 0
        self._known_nodes = set()  # type: Set[str]
        self._node_last_active: Dict[str, float] = {}  # node_id -> last message timestamp
        self._overflow: Dict[str, str] = {}  # node_id -> remaining text for !more pager

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

        # Register message handler
        pub.subscribe(self._on_receive, "meshtastic.receive.text")

        self._running = True

        # Start background threads
        threading.Thread(target=self._processing_loop, daemon=True).start()
        threading.Thread(target=self._dedup_cleanup_loop, daemon=True).start()
        threading.Thread(target=self._context_cleanup_loop, daemon=True).start()

        # Connect to radio in background — bridge runs even without radio
        threading.Thread(target=self._radio_connection_loop, daemon=True).start()

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

    def _radio_connection_loop(self):
        """Background loop: connect to radio, reconnect if it drops."""
        while self._running:
            if self._is_interface_alive():
                time.sleep(5)
                continue
            # Interface is dead or not connected — try to connect
            self._connect_radio()
            if self.interface:
                self._check_firmware()
            else:
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    def _connect_radio(self):
        """Attempt to connect to the Meshtastic device once.

        Sets self.interface on success, leaves it None on failure.
        Never calls sys.exit — caller handles retries.
        """
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
            update_state(connected=True)

        except Exception as e:
            logger.error(
                f"Connection failed: {e}. Retrying in {self._reconnect_delay}s..."
            )
            self.interface = None
            update_state(connected=False)

    def _on_receive(self, packet, interface):
        """Handle incoming text message from the mesh."""
        try:
            sender = packet.get("fromId", "unknown")
            to_id = packet.get("toId", "")
            text = packet.get("decoded", {}).get("text", "")
            channel = packet.get("channel", 0)

            if not text or not text.strip():
                return

            # Always respond via DM back to the sender.
            # The Meshtastic toId format (e.g. '!02e5f1e0') vs node num (int)
            # makes reliable DM detection fragile, so we treat every incoming
            # text message as directed at us and always reply privately.
            is_dm = True

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

            ch_label = "DM" if is_dm else f"ch{channel}"
            logger.info(f"Received from {sender} ({ch_label}): {text[:100]}")
            record_message("in", sender, text.strip())

            # Notify addon observers
            for addon in self._addons:
                try:
                    addon.on_message(sender, text.strip(), packet)
                except Exception as e:
                    logger.warning(f"Addon {addon.name} on_message error: {e}")

            self._request_queue.put((sender, text.strip(), channel, is_dm))

        except Exception as e:
            logger.error(f"Error processing incoming message: {e}")

    def _processing_loop(self):
        """Process queued messages through Ollama and send responses."""
        while self._running:
            try:
                node_id, text, channel, is_dm = self._request_queue.get(timeout=1)
            except queue.Empty:
                continue

            try:
                self._message_count += 1
                self._node_last_active[node_id] = time.time()

                # Check for commands
                response = self._handle_command(node_id, text)
                elapsed = 0

                if response is None:
                    # Regular message — send to Ollama
                    logger.info(f"Processing query from {node_id}...")

                    # Send immediate acknowledgment so user knows not to resend
                    self._send_raw(node_id, "Thinking...", channel=channel, is_dm=is_dm)

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
                record_message("out", node_id, response, chunks=1, llm_time=elapsed)
                update_state(
                    message_count=self._message_count,
                    node_count=self._node_count,
                    known_nodes=list(self._known_nodes),
                )
                self._send_response(node_id, response, channel=channel, is_dm=is_dm)

            except Exception as e:
                logger.error(f"Error processing message from {node_id}: {e}")
                self._send_response(node_id, f"Error: {e}", channel=channel, is_dm=is_dm)

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
        remaining = self._overflow.get(node_id, "")
        if not remaining:
            return "No more content."
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
            # Verify localNode is accessible (used by _sendPacket for hop_limit)
            _ = self.interface.localNode
            return True
        except Exception:
            return False

    def _reconnect_radio(self):
        """Close the current interface and reconnect."""
        logger.warning("Reconnecting to radio...")
        update_state(connected=False)
        if self.interface:
            try:
                self.interface.close()
            except Exception:
                pass
            self.interface = None
        self._connect_radio()

    def _send_raw(self, node_id: str, text: str, channel: int = 0, is_dm: bool = True):
        """Send a short status message (no pager, no overflow).

        Used for 'Thinking...' and other ephemeral status indicators.
        Replies on the same channel the original message came from.
        Includes a post-TX settle delay so the radio returns to RX mode.
        """
        if not self.interface or not self._is_interface_alive():
            return
        try:
            dest = node_id if is_dm else BROADCAST_ADDR
            self.interface.sendText(
                text, destinationId=dest, channelIndex=channel, wantAck=False,
            )
            ch_label = "DM" if is_dm else f"ch{channel}"
            logger.info(f"Status to {node_id} ({ch_label}): {text}")
            time.sleep(3)  # Post-TX settle — let radio return to RX
        except Exception as e:
            logger.warning(f"Failed to send status to {node_id}: {e}")

    def _send_response(self, node_id: str, content: str, channel: int = 0, is_dm: bool = True):
        """Send a single plain-text response over the mesh.

        The Meshtastic app groups messages by sender and only shows the latest,
        so we ALWAYS send exactly one message. If the response is too long, we
        truncate at a sentence boundary and store the overflow for the !more
        command (interactive pager).

        Replies on the same channel the original message came from:
        DMs get a DM back, public channel messages get a public reply.
        """
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
            self._overflow[node_id] = remaining
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
                result = self.interface.sendText(
                    message,
                    destinationId=dest,
                    channelIndex=channel,
                    wantAck=False,
                )
                pkt_id = getattr(result, "id", None)
                ch_label = "DM" if is_dm else f"ch{channel}"
                logger.info(
                    f"Sent to {node_id} ({ch_label}, pkt_id={pkt_id}, attempt={attempt})"
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
        """Clean up expired deduplication cache entries."""
        while self._running:
            time.sleep(60)
            now = time.time()
            expired = [k for k, v in _dedup_cache.items() if now - v > DEDUP_TTL]
            for k in expired:
                del _dedup_cache[k]

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

    def _cleanup(self):
        """Clean up resources."""
        # Stop addons
        for addon in self._addons:
            try:
                addon.on_stop()
            except Exception as e:
                logger.warning(f"Addon {addon.name} on_stop error: {e}")

        if self.interface:
            try:
                self.interface.close()
            except Exception:
                pass
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
        default=False,
        help="Enable Dead Drop encrypted async messaging",
    )
    parser.add_argument(
        "--enable-triage",
        action="store_true",
        default=False,
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
        default=False,
        help="Enable Brief AI-generated situation reports",
    )
    parser.add_argument(
        "--brief-interval",
        type=int,
        default=60,
        help="Brief SITREP generation interval in minutes (default: 60)",
    )
    parser.add_argument(
        "--enable-all-addons",
        action="store_true",
        default=False,
        help="Enable all available addons",
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
