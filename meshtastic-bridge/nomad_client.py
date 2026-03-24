"""HTTP client for communicating with the N.O.M.A.D. API."""

import logging
import requests
from collections import deque

logger = logging.getLogger(__name__)

MAX_LOCAL_QUEUE = 50


class NomadClient:
    """Client for N.O.M.A.D.'s Meshtastic API endpoints."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self._offline_queue: deque = deque(maxlen=MAX_LOCAL_QUEUE)

    def send_incoming(
        self, node_id: str, content: str, long_name: str = None, short_name: str = None
    ) -> dict | None:
        """Send an incoming mesh message to N.O.M.A.D."""
        payload = {"nodeId": node_id, "content": content}
        if long_name:
            payload["longName"] = long_name
        if short_name:
            payload["shortName"] = short_name

        try:
            resp = self.session.post(
                f"{self.base_url}/api/meshtastic/incoming",
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to send incoming message to N.O.M.A.D.: {e}")
            self._offline_queue.append(payload)
            return None

    def get_outgoing(self, node_id: str) -> list[dict]:
        """Poll for outgoing messages ready to send to a mesh node."""
        try:
            resp = self.session.get(
                f"{self.base_url}/api/meshtastic/outgoing/{node_id}",
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug(f"Failed to get outgoing messages for {node_id}: {e}")
            return []

    def mark_sending(self, message_id: int, chunk_count: int) -> bool:
        """Notify N.O.M.A.D. that we're starting to send a message."""
        try:
            resp = self.session.post(
                f"{self.base_url}/api/meshtastic/outgoing/{message_id}/sending",
                json={"chunkCount": chunk_count},
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning(f"Failed to mark message {message_id} as sending: {e}")
            return False

    def mark_sent(self, message_id: int) -> bool:
        """Confirm a message was fully sent over the mesh."""
        try:
            resp = self.session.post(
                f"{self.base_url}/api/meshtastic/outgoing/{message_id}/sent",
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning(f"Failed to mark message {message_id} as sent: {e}")
            return False

    def is_healthy(self) -> bool:
        """Check if N.O.M.A.D. API is reachable."""
        try:
            resp = self.session.get(
                f"{self.base_url}/api/health", timeout=5
            )
            return resp.status_code == 200
        except Exception:
            return False

    def flush_offline_queue(self):
        """Retry sending queued messages that failed when API was unreachable."""
        while self._offline_queue:
            payload = self._offline_queue[0]
            try:
                resp = self.session.post(
                    f"{self.base_url}/api/meshtastic/incoming",
                    json=payload,
                    timeout=30,
                )
                resp.raise_for_status()
                self._offline_queue.popleft()
                logger.info(f"Flushed queued message for node {payload.get('nodeId')}")
            except Exception:
                break
