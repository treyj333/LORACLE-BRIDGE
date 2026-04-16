"""Protocol auto-detection for serial/TCP/BLE connections.

Given transport parameters, probes the device to determine if it speaks
Meshtastic or MeshCore.  Falls back to None if detection is ambiguous.
"""

import asyncio
import logging
from typing import Optional

from radio.events import Protocol, Transport

logger = logging.getLogger("radio.detector")


class DetectionFailed(Exception):
    """Raised when protocol auto-detection cannot determine the protocol."""


def detect_protocol(
    transport: Transport,
    params: dict,
    timeout: float = 5.0,
) -> Optional[Protocol]:
    """Probe a connection to determine the protocol.

    Args:
        transport: SERIAL, TCP, or BLE.
        params: Transport-specific parameters (port, host, address, etc.).
        timeout: Seconds to wait for each probe.

    Returns:
        Protocol.MESHTASTIC or Protocol.MESHCORE, or None if detection fails.
    """
    if transport == Transport.SERIAL:
        return _detect_serial(params.get("port"), timeout)
    elif transport == Transport.TCP:
        return _detect_tcp(params.get("host"), params.get("port"), timeout)
    elif transport == Transport.BLE:
        return _detect_ble(params.get("address"), timeout)
    return None


def _detect_serial(port: Optional[str], timeout: float) -> Optional[Protocol]:
    """Try Meshtastic first (more common), then MeshCore."""
    if not port:
        return None

    # Try Meshtastic
    try:
        import meshtastic.serial_interface
        iface = meshtastic.serial_interface.SerialInterface(devPath=port)
        # If we get here without exception, it's Meshtastic
        iface.close()
        logger.info(f"Detected Meshtastic on {port}")
        return Protocol.MESHTASTIC
    except Exception:
        pass

    # Try MeshCore
    try:
        from meshcore import MeshCore

        async def _probe():
            mc = await asyncio.wait_for(
                MeshCore.create_serial(port, 115200),
                timeout=timeout,
            )
            await mc.disconnect()
            return True

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(_probe())
            if result:
                logger.info(f"Detected MeshCore on {port}")
                return Protocol.MESHCORE
        finally:
            loop.close()
    except Exception:
        pass

    logger.warning(f"Could not detect protocol on {port}")
    return None


def _detect_tcp(host: Optional[str], port: Optional[int], timeout: float) -> Optional[Protocol]:
    """Probe TCP connection for protocol."""
    if not host:
        return None

    # Meshtastic default port is 4403, MeshCore is 4000
    # Use port hint if available
    if port == 4403:
        return Protocol.MESHTASTIC
    if port == 4000:
        return Protocol.MESHCORE

    # Try Meshtastic
    try:
        import meshtastic.tcp_interface
        iface = meshtastic.tcp_interface.TCPInterface(
            hostname=host, portNumber=port or 4403
        )
        iface.close()
        return Protocol.MESHTASTIC
    except Exception:
        pass

    # Try MeshCore
    try:
        from meshcore import MeshCore

        async def _probe():
            mc = await asyncio.wait_for(
                MeshCore.create_tcp(host, port or 4000),
                timeout=timeout,
            )
            await mc.disconnect()
            return True

        loop = asyncio.new_event_loop()
        try:
            if loop.run_until_complete(_probe()):
                return Protocol.MESHCORE
        finally:
            loop.close()
    except Exception:
        pass

    return None


def _detect_ble(address: Optional[str], timeout: float) -> Optional[Protocol]:
    """Probe BLE connection for protocol.

    Meshtastic and MeshCore use different GATT service UUIDs.
    """
    # Without address, we can't probe
    if not address:
        return None

    # Try Meshtastic first (more common for BLE)
    try:
        import meshtastic.ble_interface
        iface = meshtastic.ble_interface.BLEInterface(address=address)
        iface.close()
        return Protocol.MESHTASTIC
    except Exception:
        pass

    # Try MeshCore
    try:
        from meshcore import MeshCore

        async def _probe():
            mc = await asyncio.wait_for(
                MeshCore.create_ble(address),
                timeout=timeout,
            )
            await mc.disconnect()
            return True

        loop = asyncio.new_event_loop()
        try:
            if loop.run_until_complete(_probe()):
                return Protocol.MESHCORE
        finally:
            loop.close()
    except Exception:
        pass

    return None
