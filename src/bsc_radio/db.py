from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migration (
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS source (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
  station TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  config_json TEXT NOT NULL DEFAULT '{}', last_fetch_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS source_fetch (
  id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES source(id), started_at TEXT NOT NULL,
  finished_at TEXT, status_code INTEGER, item_count INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS item (
  id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES source(id), url TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL, author TEXT, published_at TEXT, collected_at TEXT NOT NULL,
  content_hash TEXT NOT NULL, raw_text TEXT, status TEXT NOT NULL DEFAULT 'new', metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_item_status_date ON item(status, published_at DESC);
CREATE TABLE IF NOT EXISTS fact (
  id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES item(id), statement TEXT NOT NULL,
  fact_type TEXT NOT NULL, reliability REAL NOT NULL DEFAULT 0.5, quote TEXT,
  source_url TEXT NOT NULL, source_name TEXT NOT NULL, source_date TEXT,
  embedding_json TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS event (
  id INTEGER PRIMARY KEY, canonical_name TEXT NOT NULL UNIQUE, slug TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'normal', starts_at TEXT, ends_at TEXT,
  aliases_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS topic (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL, station TEXT NOT NULL,
  event_id INTEGER REFERENCES event(id), status TEXT NOT NULL DEFAULT 'open',
  embedding_json TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topic_fact (
  topic_id INTEGER NOT NULL REFERENCES topic(id), fact_id INTEGER NOT NULL REFERENCES fact(id),
  PRIMARY KEY(topic_id, fact_id)
);
CREATE TABLE IF NOT EXISTS episode (
  id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL, format TEXT NOT NULL,
  station TEXT NOT NULL, event_id INTEGER REFERENCES event(id), status TEXT NOT NULL DEFAULT 'draft',
  summary TEXT NOT NULL DEFAULT '', script_json TEXT NOT NULL DEFAULT '[]',
  prompt_hash TEXT, rejection_reason TEXT, published_at TEXT, hidden INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_episode_status_format ON episode(status, format, published_at DESC);
CREATE TABLE IF NOT EXISTS sentence (
  id INTEGER PRIMARY KEY, episode_id INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
  position INTEGER NOT NULL, character TEXT NOT NULL, text TEXT NOT NULL,
  delivery_json TEXT NOT NULL DEFAULT '[]', fact_ids_json TEXT NOT NULL DEFAULT '[]',
  verdict TEXT, verifier_note TEXT,
  UNIQUE(episode_id, position)
);
CREATE TABLE IF NOT EXISTS audio (
  id INTEGER PRIMARY KEY, episode_id INTEGER NOT NULL REFERENCES episode(id), path TEXT NOT NULL,
  duration_seconds REAL NOT NULL, bitrate INTEGER NOT NULL DEFAULT 128, lufs REAL,
  checksum TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS airing (
  id INTEGER PRIMARY KEY, episode_id INTEGER REFERENCES episode(id), audio_id INTEGER REFERENCES audio(id),
  started_at TEXT NOT NULL, ended_at TEXT, listener_peak INTEGER, reason TEXT
);
CREATE TABLE IF NOT EXISTS cost (
  id INTEGER PRIMARY KEY, provider TEXT NOT NULL, operation TEXT NOT NULL, model TEXT NOT NULL,
  units_in INTEGER NOT NULL DEFAULT 0, units_out INTEGER NOT NULL DEFAULT 0,
  cost_micro_eur INTEGER NOT NULL DEFAULT 0, request_id TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS user (
  id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
  email_confirmed INTEGER NOT NULL DEFAULT 0, plan TEXT NOT NULL DEFAULT 'free',
  evening_email INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS session (
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
  expires_at TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS auth_token (
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT
);
CREATE TABLE IF NOT EXISTS event_vote (
  event_id INTEGER NOT NULL REFERENCES event(id), user_id INTEGER NOT NULL REFERENCES user(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(event_id, user_id)
);
CREATE TABLE IF NOT EXISTS event_suggestion (
  id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES user(id), name TEXT NOT NULL, url TEXT,
  status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES user(id), episode_id INTEGER REFERENCES episode(id),
  rating INTEGER, message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS incident (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, severity TEXT NOT NULL DEFAULT 'error', message TEXT NOT NULL,
  context_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'open',
  last_alert_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS task (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
  available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, leased_until TEXT, error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS lexicon_entry (
  term TEXT PRIMARY KEY COLLATE NOCASE, pronunciation TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS domain_watch (
  domain TEXT PRIMARY KEY, last_status TEXT, checked_at TEXT, changed_at TEXT
);
CREATE TABLE IF NOT EXISTS pipeline_run (
  id INTEGER PRIMARY KEY, command TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running',
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT,
  heartbeat_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, pid INTEGER,
  detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def migrate(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(pipeline_run)")
            }
            if "heartbeat_at" not in columns:
                connection.execute("ALTER TABLE pipeline_run ADD COLUMN heartbeat_at TEXT")
                connection.execute(
                    "UPDATE pipeline_run SET heartbeat_at=COALESCE(finished_at,started_at)"
                )
            if "pid" not in columns:
                connection.execute("ALTER TABLE pipeline_run ADD COLUMN pid INTEGER")
            sentence_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(sentence)")
            }
            if "delivery_json" not in sentence_columns:
                connection.execute(
                    "ALTER TABLE sentence ADD COLUMN delivery_json TEXT NOT NULL DEFAULT '[]'"
                )
            connection.execute("INSERT OR IGNORE INTO schema_migration(version) VALUES (1)")
            connection.execute("INSERT OR IGNORE INTO schema_migration(version) VALUES (2)")

    @contextlib.contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute(sql, params).fetchall())

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.connect() as connection:
            cursor = connection.execute(sql, params)
            return int(cursor.lastrowid)
