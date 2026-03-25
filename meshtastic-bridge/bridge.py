"""Meshtastic ↔ N.O.M.A.D. LLM Bridge

Connects to a Meshtastic radio device (serial or TCP) and bridges text
messages to the N.O.M.A.D. offline LLM platform. LLM responses are chunked
to fit within LoRa's 228-byte message limit and sent back over the mesh.
"""

import time
import hashlib
import logging
import threading
from collections import defaultdict
from typing import Dict, Set, Tuple

import meshtastic
import meshtastic.serial_interface
import meshtastic.tcp_interface
from pubsub import pub

from config import BridgeConfig
from protocol import chunk_message
from nomad_client import NomadClient
from health import start_health_server, update_status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bridge")

# Deduplication cache: (node_id, content_hash) -> timestamp
_dedup_cache = {}  # type: Dict[Tuple[str, str], float]
DEDUP_TTL = 300  # 5 minutes

# Track nodes with pending responses
_active_nodes = set()  # type: Set[str]
_active_nodes_lock = threading.Lock()


class MeshtasticBridge:
    """Main bridge between Meshtastic mesh network and N.O.M.A.D."""

    def __init__(self):
        self.config = BridgeConfig()
        self.client = NomadClient(self.config.nomad_api_url)
        self.interface = None
        self._running = False
        self._reconnect_delay = 1

    def start(self):
        """Start the bridge."""
        logger.info("Starting Meshtastic ↔ N.O.M.A.D. bridge")

        # Start health server
        start_health_server()

        # Initial config fetch
        self.config.refresh_from_api()

        # Connect to radio
        self._connect_radio()

        # Register message handler
        pub.subscribe(self._on_receive, "meshtastic.receive.text")

        self._running = True

        # Start background threads
        threading.Thread(target=self._config_refresh_loop, daemon=True).start()
        threading.Thread(target=self._outgoing_poll_loop, daemon=True).start()
        threading.Thread(target=self._dedup_cleanup_loop, daemon=True).start()

        logger.info("Bridge started successfully")

        # Main loop - keep alive and handle reconnection
        while self._running:
            try:
                time.sleep(1)
            except KeyboardInterrupt:
                logger.info("Shutting down bridge...")
                self._running = False
                break

        self._cleanup()

    def _connect_radio(self):
        """Connect to the Meshtastic device."""
        while self._running or self._reconnect_delay == 1:  # Allow first attempt before _running is set
            try:
                if self.config.connection_type == "tcp":
                    logger.info(
                        f"Connecting to Meshtastic via TCP: {self.config.tcp_host}:{self.config.tcp_port}"
                    )
                    self.interface = meshtastic.tcp_interface.TCPInterface(
                        hostname=self.config.tcp_host,
                        portNumber=self.config.tcp_port,
                    )
                else:
                    logger.info(
                        f"Connecting to Meshtastic via serial: {self.config.serial_port}"
                    )
                    self.interface = meshtastic.serial_interface.SerialInterface(
                        devPath=self.config.serial_port
                    )

                self._reconnect_delay = 1
                update_status(radio_connected=True)
                logger.info("Connected to Meshtastic device")
                return

            except Exception as e:
                update_status(radio_connected=False)
                logger.error(
                    f"Failed to connect to Meshtastic device: {e}. "
                    f"Retrying in {self._reconnect_delay}s..."
                )
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
                logger.debug(f"Duplicate message from {sender}, ignoring")
                return

            _dedup_cache[cache_key] = now

            # Extract node info
            node_info = {}
            if self.interface and hasattr(self.interface, "nodes"):
                node = self.interface.nodes.get(sender, {})
                user = node.get("user", {})
                node_info["long_name"] = user.get("longName")
                node_info["short_name"] = user.get("shortName")

            logger.info(f"Received from {sender}: {text[:100]}")

            # Forward to N.O.M.A.D.
            result = self.client.send_incoming(
                node_id=sender,
                content=text,
                long_name=node_info.get("long_name"),
                short_name=node_info.get("short_name"),
            )

            if result:
                with _active_nodes_lock:
                    _active_nodes.add(sender)

        except Exception as e:
            logger.error(f"Error processing incoming message: {e}")

    def _outgoing_poll_loop(self):
        """Poll N.O.M.A.D. for completed LLM responses to send back over mesh."""
        while self._running:
            try:
                with _active_nodes_lock:
                    nodes = list(_active_nodes)

                update_status(active_nodes=len(nodes))

                for node_id in nodes:
                    messages = self.client.get_outgoing(node_id)

                    for msg in messages:
                        self._send_response(node_id, msg)

                # Check N.O.M.A.D. health and flush offline queue
                nomad_ok = self.client.is_healthy()
                update_status(nomad_reachable=nomad_ok)
                if nomad_ok:
                    self.client.flush_offline_queue()

            except Exception as e:
                logger.error(f"Error in outgoing poll loop: {e}")

            time.sleep(self.config.poll_interval)

    def _send_response(self, node_id: str, message: dict):
        """Chunk and send an LLM response over the mesh."""
        msg_id = message["id"]
        content = message["content"]

        logger.info(
            f"Sending response to {node_id} ({len(content)} chars): {content[:80]}..."
        )

        chunks = chunk_message(content, self.config.compression_enabled)

        # Notify N.O.M.A.D. we're sending
        self.client.mark_sending(msg_id, len(chunks))

        for i, chunk in enumerate(chunks):
            try:
                if self.interface:
                    self.interface.sendData(
                        chunk,
                        destinationId=node_id,
                        portNum=meshtastic.portnums_pb2.TEXT_MESSAGE_APP,
                    )
                    logger.debug(
                        f"Sent chunk {i + 1}/{len(chunks)} to {node_id}"
                    )

                # Delay between chunks to avoid flooding
                if i < len(chunks) - 1:
                    time.sleep(self.config.inter_chunk_delay)

            except Exception as e:
                logger.error(
                    f"Failed to send chunk {i + 1}/{len(chunks)} to {node_id}: {e}"
                )
                return

        # Mark as fully sent
        self.client.mark_sent(msg_id)
        logger.info(f"Response sent to {node_id} ({len(chunks)} chunks)")

    def _config_refresh_loop(self):
        """Periodically refresh configuration from N.O.M.A.D. API."""
        while self._running:
            time.sleep(self.config.config_refresh_interval)
            self.config.refresh_from_api()

    def _dedup_cleanup_loop(self):
        """Clean up expired deduplication cache entries."""
        while self._running:
            time.sleep(60)
            now = time.time()
            expired = [k for k, v in _dedup_cache.items() if now - v > DEDUP_TTL]
            for k in expired:
                del _dedup_cache[k]

    def _cleanup(self):
        """Clean up resources on shutdown."""
        if self.interface:
            try:
                self.interface.close()
            except Exception:
                pass
        logger.info("Bridge shut down")


if __name__ == "__main__":
    bridge = MeshtasticBridge()
    bridge.start()
