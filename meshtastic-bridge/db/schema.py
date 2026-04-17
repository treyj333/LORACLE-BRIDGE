"""SQLite schema and initialization for LORACLE Bridge."""

import json
import logging
import os
import sqlite3
import time

logger = logging.getLogger("db.schema")

SCHEMA_VERSION = 1

_TABLES = """
CREATE TABLE IF NOT EXISTS contacts (
  id TEXT PRIMARY KEY,
  protocol TEXT NOT NULL,
  backend_id TEXT NOT NULL,
  short_name TEXT NOT NULL,
  long_name TEXT,
  last_rssi INTEGER,
  last_snr REAL,
  last_hops INTEGER,
  first_seen REAL NOT NULL,
  last_heard REAL,
  has_position INTEGER DEFAULT 0,
  ai_enabled INTEGER,
  is_channel INTEGER DEFAULT 0,
  channel_idx INTEGER,
  channel_name TEXT,
  unread_count INTEGER DEFAULT 0,
  last_read_at REAL,
  is_favorite INTEGER NOT NULL DEFAULT 0,
  custom_name TEXT,
  created_at REAL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_contacts_last_heard ON contacts(last_heard DESC);
CREATE INDEX IF NOT EXISTS idx_contacts_protocol ON contacts(protocol);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  contact_id TEXT NOT NULL,
  direction TEXT NOT NULL,
  author TEXT NOT NULL,
  text TEXT NOT NULL,
  timestamp REAL NOT NULL,
  protocol TEXT NOT NULL,
  hops_traveled INTEGER,
  rx_rssi INTEGER,
  rx_snr REAL,
  is_channel_msg INTEGER DEFAULT 0,
  originating_channel_id TEXT,
  delivery_status TEXT DEFAULT 'sent',
  FOREIGN KEY (contact_id) REFERENCES contacts(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_contact_ts ON messages(contact_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp DESC);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at REAL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS installed_packs (
  pack_id TEXT PRIMARY KEY,
  version TEXT NOT NULL,
  installed_at REAL DEFAULT (strftime('%s','now')),
  install_path TEXT NOT NULL,
  doc_count_success INTEGER NOT NULL,
  doc_count_failed INTEGER NOT NULL,
  total_bytes INTEGER,
  manifest_snapshot TEXT
);

CREATE TABLE IF NOT EXISTS pack_documents (
  pack_id TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  local_path TEXT NOT NULL,
  sha256 TEXT,
  bytes INTEGER,
  fetched_at REAL,
  ingested_at REAL,
  chunk_count INTEGER,
  PRIMARY KEY (pack_id, doc_id),
  FOREIGN KEY (pack_id) REFERENCES installed_packs(pack_id)
);

CREATE TABLE IF NOT EXISTS bridge_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp REAL NOT NULL,
  source_protocol TEXT NOT NULL,
  dest_protocol TEXT NOT NULL,
  channel INTEGER NOT NULL,
  sender TEXT NOT NULL,
  sender_display TEXT,
  text TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('relayed', 'blocked', 'rate_limited', 'deduped', 'loop_guard')),
  force_relay INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_bridge_events_ts ON bridge_events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_bridge_events_outcome ON bridge_events(outcome, timestamp DESC);
"""


def init_db(path: str) -> sqlite3.Connection:
    """Create or open the database and ensure schema exists."""
    if path != ":memory:":
        os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(_TABLES)
    _ensure_contact_columns(db)
    db.commit()
    logger.info(f"Database ready: {path}")
    return db


def _ensure_contact_columns(db: sqlite3.Connection) -> None:
    """Idempotently add columns that were introduced after the initial schema."""
    cols = {row["name"] for row in db.execute("PRAGMA table_info(contacts)").fetchall()}
    if "is_favorite" not in cols:
        db.execute("ALTER TABLE contacts ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0")
        logger.info("Migration: added contacts.is_favorite")
    if "custom_name" not in cols:
        db.execute("ALTER TABLE contacts ADD COLUMN custom_name TEXT")
        logger.info("Migration: added contacts.custom_name")


def migrate_from_json(db: sqlite3.Connection, json_path: str) -> None:
    """One-time migration of settings.json into the settings table."""
    if not os.path.exists(json_path):
        return
    try:
        with open(json_path) as f:
            data = json.load(f)
        migrated = 0
        for key, value in data.items():
            db.execute(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value), time.time()),
            )
            migrated += 1
        db.commit()
        # Rename to .bak
        bak_path = json_path + ".bak"
        os.rename(json_path, bak_path)
        logger.info(f"Migrated {migrated} settings from {json_path} → SQLite (backup: {bak_path})")
    except Exception as e:
        logger.warning(f"Settings migration failed (preserved original): {e}")
