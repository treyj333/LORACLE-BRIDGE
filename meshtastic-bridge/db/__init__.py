"""SQLite persistence layer for LORACLE Bridge."""

from db.schema import init_db, migrate_from_json
from db.contacts import ContactStore
from db.messages import MessageStore
from db.settings import SettingsStore

__all__ = [
    "init_db",
    "migrate_from_json",
    "ContactStore",
    "MessageStore",
    "SettingsStore",
]
