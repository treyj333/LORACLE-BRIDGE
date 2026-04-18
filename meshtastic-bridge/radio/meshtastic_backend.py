"""MeshtasticBackend — RadioBackend implementation for Meshtastic radios.

Wraps the ``meshtastic`` Python library's pubsub-based event model into
the RadioBackend interface.  All Meshtastic-specific code (serial/TCP/BLE
connection, packet parsing, nodeDB refresh, BLE scanning) lives here.
"""

import glob
import json
import logging
import os
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from radio.events import Protocol, Transport, UnifiedMessage, UnifiedNode
from radio.backend import RadioBackend

try:
    import meshtastic
    import meshtastic.serial_interface
    import meshtastic.tcp_interface
    MESHTASTIC_AVAILABLE = True
except ImportError:
    MESHTASTIC_AVAILABLE = False

try:
    import meshtastic.ble_interface
    BLE_AVAILABLE = True
except ImportError:
    BLE_AVAILABLE = False

logger = logging.getLogger("radio.meshtastic")

# ── Constants ────────────────────────────────────────────────────────────────

NODEDB_REFRESH_INTERVAL_S = 30
MAX_TRACKED_NODES = 2000


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


def _get_meshtastic_version() -> str:
    try:
        return meshtastic.__version__
    except AttributeError:
        return "unknown"


class MeshtasticBackend(RadioBackend):
    """RadioBackend for Meshtastic devices (serial, TCP, BLE)."""

    protocol = Protocol.MESHTASTIC

    def __init__(
        self,
        connection_type: str = "serial",
        serial_port: Optional[str] = None,
        tcp_host: str = "192.168.1.1",
        tcp_port: int = 4403,
        ble_address: Optional[str] = None,
        backend_id: Optional[str] = None,
    ):
        self.backend_id = backend_id or f"mt-{connection_type}-0"
        self.transport = Transport(connection_type)
        self._connection_type = connection_type
        self._serial_port = serial_port
        self._tcp_host = tcp_host
        self._tcp_port = tcp_port
        self._ble_address = ble_address

        self._interface = None
        self._callback: Optional[Callable] = None
        self._running = False
        self._connecting = False
        self._connection_paused = False
        self._reconnect_delay = 1.0
        self._ble_scanning = False

        self._node_positions: Dict[str, dict] = {}
        self._node_meta: Dict[str, dict] = {}
        self._last_nodedb_refresh = 0.0
        self._conn_thread: Optional[threading.Thread] = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    def connect(self) -> None:
        if not MESHTASTIC_AVAILABLE:
            raise ImportError("meshtastic library not installed")
        self._running = True
        self._conn_thread = threading.Thread(
            target=self._connection_loop, daemon=True
        )
        self._conn_thread.start()

    def disconnect(self) -> None:
        self._running = False
        self._connection_paused = True
        if self._interface:
            try:
                self._interface.close()
            except Exception as e:
                logger.warning(f"Error closing interface: {e}")
            self._interface = None
        if self._conn_thread:
            self._conn_thread.join(timeout=5)

    def is_connected(self) -> bool:
        try:
            if self._interface is None:
                return False
            if hasattr(self._interface, "stream") and self._interface.stream is None:
                return False
            if hasattr(self._interface, "socket") and self._interface.socket is None:
                return False
            if BLE_AVAILABLE and isinstance(self._interface, meshtastic.ble_interface.BLEInterface):
                if hasattr(self._interface, "client") and self._interface.client is None:
                    return False
            _ = self._interface.localNode
            return True
        except (AttributeError, OSError):
            return False

    # ── Messaging ────────────────────────────────────────────────────────

    def send_direct_message(self, to_native_id: str, text: str) -> None:
        if not self.is_connected():
            raise ConnectionError("Meshtastic radio not connected")
        self._interface.sendText(
            text, destinationId=to_native_id, wantAck=False
        )

    def send_broadcast(self, text: str, channel: int = 0) -> None:
        if not self.is_connected():
            raise ConnectionError("Meshtastic radio not connected")
        from meshtastic import BROADCAST_ADDR
        self._interface.sendText(
            text, destinationId=BROADCAST_ADDR, channelIndex=channel, wantAck=False
        )

    # ── Protocol-optional features (Meshtastic supports all three) ───────

    def send_traceroute(self, to_native_id: str, hop_limit: int = 7) -> None:
        if not self.is_connected():
            raise ConnectionError("Meshtastic radio not connected")
        self._interface.sendTraceRoute(to_native_id, hopLimit=hop_limit)

    def get_radio_config(self) -> dict:
        if not self.is_connected():
            raise ConnectionError("Meshtastic radio not connected")
        node = getattr(self._interface, "localNode", None)
        lora = getattr(getattr(node, "localConfig", None), "lora", None)
        if not lora:
            raise RuntimeError("Could not read localConfig.lora from radio")
        return {
            "region": getattr(lora, "region", 0),
            "modem_preset": getattr(lora, "modemPreset", 0),
            "tx_power": getattr(lora, "txPower", 0),
            "hop_limit": getattr(lora, "hopLimit", 3),
            "tx_enabled": getattr(lora, "txEnabled", True),
            "bandwidth": getattr(lora, "bandwidth", 0),
            "spread_factor": getattr(lora, "spreadFactor", 0),
            "coding_rate": getattr(lora, "codingRate", 0),
        }

    def set_radio_config(self, config: dict) -> None:
        if not self.is_connected():
            raise ConnectionError("Meshtastic radio not connected")
        node = getattr(self._interface, "localNode", None)
        lora = getattr(getattr(node, "localConfig", None), "lora", None)
        if not lora:
            raise RuntimeError("Could not read localConfig.lora from radio")
        changed = False
        if "hop_limit" in config:
            lora.hopLimit = max(1, min(int(config["hop_limit"]), 7))
            changed = True
        if "tx_power" in config:
            lora.txPower = max(0, min(int(config["tx_power"]), 30))
            changed = True
        if "region" in config:
            lora.region = int(config["region"])
            changed = True
        if "modem_preset" in config:
            lora.modemPreset = int(config["modem_preset"])
            changed = True
        if changed:
            node.writeConfig("lora")

    # ── Listening ────────────────────────────────────────────────────────

    def start_listening(self, callback: Callable[[UnifiedMessage], None]) -> None:
        from pubsub import pub
        self._callback = callback
        pub.subscribe(self._on_text, "meshtastic.receive.text")
        pub.subscribe(self._on_position, "meshtastic.receive.position")

    def stop_listening(self) -> None:
        from pubsub import pub
        try:
            pub.unsubscribe(self._on_text, "meshtastic.receive.text")
        except Exception:
            pass
        try:
            pub.unsubscribe(self._on_position, "meshtastic.receive.position")
        except Exception:
            pass
        self._callback = None

    # ── Node / device info ───────────────────────────────────────────────

    def get_nodes(self) -> Dict[str, UnifiedNode]:
        result = {}
        if not self._interface:
            return result
        try:
            nodes = getattr(self._interface, "nodes", None)
            if not nodes:
                return result
            for node_id_str, info in nodes.items():
                key = str(node_id_str)
                pos = self._extract_position(info.get("position", {}))
                uid = UnifiedNode.make_id(Protocol.MESHTASTIC, key)
                node = UnifiedNode(
                    id=uid,
                    protocol=Protocol.MESHTASTIC,
                    backend_native_id=key,
                    short_name=key[-6:] if len(key) > 6 else key,
                    long_name=info.get("user", {}).get("longName"),
                    has_position=pos is not None,
                    lat=pos[0] if pos else None,
                    lon=pos[1] if pos else None,
                    alt=pos[2] if pos else None,
                )
                meta = self._node_meta.get(key)
                if meta:
                    node.hops_away = meta.get("hops")
                result[key] = node
        except Exception as e:
            logger.debug(f"Error getting nodes: {e}")
        return result

    def get_self_info(self) -> dict:
        info = {
            "firmware_version": "unknown",
            "library_version": _get_meshtastic_version(),
            "hw_model": "unknown",
            "self_node_id": None,
        }
        if not self._interface:
            return info
        try:
            md = getattr(self._interface, "metadata", None)
            if md:
                info["firmware_version"] = getattr(md, "firmware_version", "unknown")
                hw = getattr(md, "hw_model", None)
                if hw is not None:
                    info["hw_model"] = str(hw)
        except Exception:
            pass
        info["self_node_id"] = self._get_self_node_id()
        return info

    def get_node_positions(self) -> Dict[str, dict]:
        return dict(self._node_positions)

    def get_node_meta(self) -> Dict[str, dict]:
        return dict(self._node_meta)

    # ── BLE scanning ─────────────────────────────────────────────────────

    def scan_ble_devices(self, timeout: float = 10.0) -> list:
        if not BLE_AVAILABLE:
            return []
        if self._ble_scanning:
            return []
        self._ble_scanning = True
        try:
            import subprocess
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
            venv_python = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "venv", "bin", "python",
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
                    return [{"error": "bluetooth_permission",
                             "message": "Python needs Bluetooth permission on macOS."}]
                return [{"error": "scan_failed", "message": stderr or "BLE scan crashed"}]
            output = proc.stdout.strip()
            if not output:
                return []
            data = json.loads(output)
            if isinstance(data, dict) and "error" in data:
                return [{"error": "scan_error", "message": data["error"]}]
            return data
        except Exception as e:
            return [{"error": "scan_failed", "message": str(e)}]
        finally:
            self._ble_scanning = False

    # ── BLE device persistence ───────────────────────────────────────────

    @staticmethod
    def _ble_config_path() -> str:
        return os.path.join(os.path.expanduser("~"), ".mesh-llm", "ble_last_device.json")

    def _save_last_ble_device(self, address: str, name: str = ""):
        try:
            path = self._ble_config_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump({"address": address, "name": name}, f)
        except Exception:
            pass

    @staticmethod
    def load_last_ble_device() -> Optional[dict]:
        try:
            path = MeshtasticBackend._ble_config_path()
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None

    # ── Connection management ────────────────────────────────────────────

    def pause_reconnect(self):
        self._connection_paused = True

    def resume_reconnect(self):
        self._connection_paused = False
        self._reconnect_delay = 1.0

    def switch_connection(self, connection_type: str, **kwargs):
        """Switch transport parameters and reconnect."""
        self.pause_reconnect()
        if self._interface:
            try:
                self._interface.close()
            except Exception:
                pass
            self._interface = None

        self._connection_type = connection_type
        self.transport = Transport(connection_type)
        if connection_type == "ble":
            self._ble_address = kwargs.get("address")
        elif connection_type == "tcp":
            self._tcp_host = kwargs.get("host", self._tcp_host)
            self._tcp_port = kwargs.get("port", self._tcp_port)
        else:
            self._serial_port = kwargs.get("address")

        self.resume_reconnect()

    # ── Internal: connection loop ────────────────────────────────────────

    def _connection_loop(self):
        """Background thread: connect, reconnect on drop, refresh nodeDB."""
        while self._running:
            if self._connection_paused:
                time.sleep(2)
                continue
            if self.is_connected():
                if time.time() - self._last_nodedb_refresh >= NODEDB_REFRESH_INTERVAL_S:
                    self._refresh_nodedb()
                time.sleep(5)
                continue
            if self._connecting:
                time.sleep(2)
                continue
            self._attempt_connect()
            if self._interface:
                self._reconnect_delay = 1.0
                if self._connection_type == "ble":
                    time.sleep(10)
            else:
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    def _attempt_connect(self):
        if self._connecting:
            return
        self._connecting = True
        try:
            if self._connection_type == "ble":
                if not BLE_AVAILABLE:
                    logger.error("BLE not available — requires Python 3.11+ with bleak")
                    return
                addr_msg = f": {self._ble_address}" if self._ble_address else " (scanning...)"
                logger.info(f"Connecting via BLE{addr_msg}")
                self._interface = meshtastic.ble_interface.BLEInterface(
                    address=self._ble_address if self._ble_address else None
                )
                if self._ble_address:
                    self._save_last_ble_device(self._ble_address)
            elif self._connection_type == "tcp":
                logger.info(f"Connecting via TCP: {self._tcp_host}:{self._tcp_port}")
                self._interface = meshtastic.tcp_interface.TCPInterface(
                    hostname=self._tcp_host,
                    portNumber=self._tcp_port,
                )
            else:
                port = self._serial_port or auto_detect_serial_port()
                if not port:
                    logger.warning("No Meshtastic device found. Waiting for USB...")
                    return
                logger.info(f"Connecting via serial: {port}")
                self._interface = meshtastic.serial_interface.SerialInterface(devPath=port)

            logger.info("Connected to Meshtastic device")
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self._interface = None
        finally:
            self._connecting = False

    def _get_self_node_id(self) -> Optional[str]:
        if not self._interface:
            return None
        try:
            my_info = getattr(self._interface, "myInfo", None)
            num = None
            if my_info is not None:
                num = getattr(my_info, "my_node_num", None) or getattr(my_info, "myNodeNum", None)
            if num is None:
                local_node = getattr(self._interface, "localNode", None)
                if local_node is not None:
                    num = getattr(local_node, "nodeNum", None)
            if num is None:
                return None
            return f"!{int(num):08x}"
        except (AttributeError, TypeError, ValueError):
            return None

    def get_connection_address(self) -> str:
        if self._connection_type == "ble":
            return self._ble_address or "scan"
        elif self._connection_type == "tcp":
            return f"{self._tcp_host}:{self._tcp_port}"
        return self._serial_port or "auto"

    # ── Internal: packet handling ────────────────────────────────────────

    def _on_text(self, packet, interface):
        """Convert Meshtastic text packet to UnifiedMessage and deliver."""
        if self._callback is None:
            return
        try:
            sender = packet.get("fromId", "unknown")
            to_id = packet.get("toId", "")
            text = packet.get("decoded", {}).get("text", "")
            channel = packet.get("channel", 0)

            self._record_packet_meta(sender, packet)

            if not text or not text.strip():
                return

            # DM vs broadcast detection
            self_id = self._get_self_node_id()
            if self_id and to_id:
                is_dm = to_id.lower() == self_id.lower()
            else:
                is_dm = True  # fallback

            rssi = packet.get("rxRssi")
            snr = packet.get("rxSnr")

            # Compute hops
            hop_start = packet.get("hopStart")
            hop_limit = packet.get("hopLimit")
            hops = None
            if hop_start is not None and hop_limit is not None:
                h = int(hop_start) - int(hop_limit)
                if 0 <= h <= 30:
                    hops = h

            short_name = sender[-6:] if len(sender) > 6 else sender
            node = UnifiedNode(
                id=UnifiedNode.make_id(Protocol.MESHTASTIC, sender),
                protocol=Protocol.MESHTASTIC,
                backend_native_id=sender,
                short_name=short_name,
                rssi=rssi,
                snr=snr,
                hops_away=hops,
                last_heard=time.time(),
            )

            msg = UnifiedMessage(
                protocol=Protocol.MESHTASTIC,
                node=node,
                text=text.strip(),
                channel=channel,
                is_dm=is_dm,
                rssi=rssi,
                snr=snr,
                timestamp=time.time(),
                hops_traveled=hops,
                raw_packet=packet,
            )
            self._callback(msg)
        except Exception as e:
            logger.error(f"Error converting Meshtastic packet: {e}")

    def _on_position(self, packet, interface):
        """Handle incoming position packet — update internal node data."""
        try:
            sender = packet.get("fromId", "")
            if not sender:
                return
            self._record_packet_meta(sender, packet)
            parsed = self._extract_position(
                packet.get("decoded", {}).get("position", {})
            )
            if parsed is None:
                return
            lat, lon, alt = parsed
            self._store_node_position(sender, lat, lon, alt)
        except Exception as e:
            logger.debug(f"Error processing position: {e}")

    def _record_packet_meta(self, sender: str, packet: dict):
        if not sender:
            return
        try:
            hop_start = packet.get("hopStart")
            hop_limit = packet.get("hopLimit")
            if hop_start is None or hop_limit is None:
                return
            hops = int(hop_start) - int(hop_limit)
            if hops < 0 or hops > 30:
                return
            entry = self._node_meta.setdefault(sender, {})
            entry["hops"] = hops
            entry["hops_updated"] = time.time()
        except (TypeError, ValueError):
            return

    @staticmethod
    def _extract_position(pos: dict) -> Optional[Tuple[float, float, float]]:
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

    def _store_node_position(self, node_id: str, lat: float, lon: float,
                              alt: float, ts: float = None):
        self._node_positions[node_id] = {
            "lat": lat, "lon": lon, "alt": alt,
            "last_update": ts if ts is not None else time.time(),
        }
        if len(self._node_positions) > MAX_TRACKED_NODES:
            oldest = min(
                self._node_positions,
                key=lambda k: self._node_positions[k].get("last_update", 0),
            )
            del self._node_positions[oldest]
            self._node_meta.pop(oldest, None)

    def _refresh_nodedb(self):
        if not self._interface:
            return
        try:
            nodes = getattr(self._interface, "nodes", None)
            if not nodes:
                return
            for node_id, info in nodes.items():
                key = str(node_id)
                parsed = self._extract_position(info.get("position", {}))
                if parsed is None:
                    continue
                lat, lon, alt = parsed
                pos_ts = info.get("position", {}).get("time", time.time())
                self._store_node_position(key, lat, lon, alt, ts=pos_ts)
            self._last_nodedb_refresh = time.time()
        except Exception as e:
            logger.debug(f"Could not refresh nodeDB: {e}")
