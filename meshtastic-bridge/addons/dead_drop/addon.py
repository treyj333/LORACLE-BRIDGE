"""LORACLE DEAD DROP addon — encrypted async message store over mesh.

Commands:
    !drop-key <passphrase>  — Register your encryption key
    !drop <node> <message>  — Leave encrypted message for a node
    !pickup                 — Retrieve your pending messages
    !pending                — Check how many messages are waiting
"""

import logging
import time
from typing import Dict, Optional, Tuple

from addons.base import Addon
from addons.dead_drop.crypto import derive_key, encrypt, decrypt
from addons.dead_drop.store import DeadDropStore

logger = logging.getLogger(__name__)

ADDON_CLASS = None  # Set at bottom of file


class DeadDropAddon(Addon):
    name = "dead_drop"
    display_name = "Dead Drop"

    def __init__(self, bridge, config: dict = None):
        super().__init__(bridge, config)
        self.store = DeadDropStore(
            ttl_hours=config.get("message_ttl_hours", 72) if config else 72,
        )

    def get_commands(self) -> Dict[str, Tuple[callable, str]]:
        return {
            "!drop-key": (self._cmd_drop_key, "<passphrase> - register encryption key"),
            "!drop": (self._cmd_drop, "<node> <message> - leave encrypted message"),
            "!pickup": (self._cmd_pickup, "retrieve your pending messages"),
            "!pending": (self._cmd_pending, "check waiting message count"),
        }

    def get_dashboard_tab(self) -> Optional[dict]:
        from addons.dead_drop.dashboard import get_tab_config
        return get_tab_config()

    def get_api_routes(self):
        from addons.dead_drop.dashboard import get_api_routes
        return get_api_routes(self)

    def _cmd_drop_key(self, node_id: str, arg: str) -> str:
        """Register an encryption key from a passphrase."""
        if not arg:
            has_key = self.store.has_key(node_id)
            status = "registered" if has_key else "not set"
            return f"Your Dead Drop key: {status}. Usage: !drop-key <passphrase>"

        key = derive_key(arg)
        self.store.register_key(node_id, key)
        return "Encryption key registered. Others can now !drop messages for you."

    def _cmd_drop(self, node_id: str, arg: str) -> str:
        """Leave an encrypted message for another node."""
        parts = arg.split(None, 1)
        if len(parts) < 2:
            return "Usage: !drop <node_id> <message>"

        recipient = parts[0]
        message = parts[1]

        # Normalize recipient ID (add ! prefix if missing)
        if not recipient.startswith("!"):
            recipient = "!" + recipient

        # Check recipient has a key
        recipient_key = self.store.get_key(recipient)
        if not recipient_key:
            return (
                f"Node {recipient} has no Dead Drop key. "
                "They need to run !drop-key <passphrase> first."
            )

        # Encrypt and store
        encrypted = encrypt(message, recipient_key)
        msg_id = self.store.store_message(node_id, recipient, encrypted)
        pending = self.store.pending_count(recipient)

        return f"Message dropped for {recipient} (id: {msg_id}). They have {pending} pending."

    def _cmd_pickup(self, node_id: str, arg: str) -> str:
        """Retrieve pending messages for the sender."""
        my_key = self.store.get_key(node_id)
        if not my_key:
            return "No Dead Drop key set. Run !drop-key <passphrase> first."

        messages = self.store.pickup_messages(node_id)
        if not messages:
            return "No pending messages."

        results = []
        for msg in messages:
            try:
                plaintext = decrypt(msg["encrypted_payload"], my_key)
                age_mins = int((time.time() - msg["created_at"]) / 60)
                results.append(f"From {msg['sender_node']} ({age_mins}m ago): {plaintext}")
            except Exception:
                results.append(f"From {msg['sender_node']}: [decryption failed]")

        header = f"{len(messages)} message(s):\n"
        return header + "\n".join(results)

    def _cmd_pending(self, node_id: str, arg: str) -> str:
        """Check how many messages are waiting."""
        count = self.store.pending_count(node_id)
        if count == 0:
            return "No pending Dead Drop messages."
        return f"You have {count} pending Dead Drop message(s). Run !pickup to retrieve."

    def on_start(self):
        logger.info("Dead Drop addon started")

    def on_stop(self):
        logger.info("Dead Drop addon stopped")


ADDON_CLASS = DeadDropAddon
