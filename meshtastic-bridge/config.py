"""Configuration loader for the Meshtastic bridge."""

import os
import logging
import requests

logger = logging.getLogger(__name__)

# Defaults
DEFAULT_NOMAD_API_URL = "http://nomad_admin:8080"
DEFAULT_CONNECTION_TYPE = "serial"
DEFAULT_SERIAL_PORT = "/dev/ttyUSB0"
DEFAULT_TCP_HOST = "192.168.1.1"
DEFAULT_TCP_PORT = 4403
DEFAULT_POLL_INTERVAL = 3
DEFAULT_CONFIG_REFRESH_INTERVAL = 30
DEFAULT_INTER_CHUNK_DELAY = 2.0
DEFAULT_MAX_RESPONSE_LENGTH = 500
DEFAULT_COMPRESSION_ENABLED = True


class BridgeConfig:
    """Manages configuration from environment variables and N.O.M.A.D. API."""

    def __init__(self):
        self.nomad_api_url = os.environ.get("NOMAD_API_URL", DEFAULT_NOMAD_API_URL)
        self.connection_type = os.environ.get("CONNECTION_TYPE", DEFAULT_CONNECTION_TYPE)
        self.serial_port = os.environ.get("SERIAL_PORT", DEFAULT_SERIAL_PORT)
        self.tcp_host = os.environ.get("TCP_HOST", DEFAULT_TCP_HOST)
        self.tcp_port = int(os.environ.get("TCP_PORT", DEFAULT_TCP_PORT))
        self.poll_interval = DEFAULT_POLL_INTERVAL
        self.config_refresh_interval = DEFAULT_CONFIG_REFRESH_INTERVAL
        self.inter_chunk_delay = DEFAULT_INTER_CHUNK_DELAY
        self.max_response_length = DEFAULT_MAX_RESPONSE_LENGTH
        self.compression_enabled = DEFAULT_COMPRESSION_ENABLED
        self.enabled = True

    def refresh_from_api(self):
        """Fetch latest configuration from N.O.M.A.D. API."""
        try:
            resp = requests.get(
                f"{self.nomad_api_url}/api/meshtastic/config", timeout=10
            )
            resp.raise_for_status()
            data = resp.json()

            self.enabled = data.get("enabled", True)
            self.connection_type = data.get("connectionType", self.connection_type)
            self.serial_port = data.get("serialPort", self.serial_port)
            self.tcp_host = data.get("tcpHost", self.tcp_host)
            self.tcp_port = int(data.get("tcpPort", self.tcp_port))
            self.inter_chunk_delay = data.get("interChunkDelay", 2000) / 1000.0
            self.max_response_length = data.get(
                "maxResponseLength", DEFAULT_MAX_RESPONSE_LENGTH
            )
            self.compression_enabled = data.get("compressionEnabled", True)

            logger.debug("Configuration refreshed from N.O.M.A.D. API")
        except Exception as e:
            logger.warning(f"Failed to refresh config from API: {e}")
