"""
SQLiteStore — the unified persistence backend for all memory types.

All four memory types (episodic, semantic, procedural, audit) share a
single SQLite file.  Each memory class receives an open `aiosqlite`
connection and operates on its own table(s).
"""

from contextlib import asynccontextmanager
import json
from pathlib import Path

import aiosqlite
import sqlite_vec

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS episodic_entries (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    content     TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS semantic_entries (
    id          TEXT PRIMARY KEY,
    key         TEXT UNIQUE NOT NULL,
    value       TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 1.0,
    source      TEXT,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    embedding   TEXT            -- JSON float array; NULL until first embed
);

CREATE TABLE IF NOT EXISTS skill_genomes (
    id                     TEXT PRIMARY KEY,
    name                   TEXT UNIQUE NOT NULL,
    version                INTEGER NOT NULL DEFAULT 1,
    confidence             REAL NOT NULL DEFAULT 1.0,
    usage_count            INTEGER NOT NULL DEFAULT 0,
    success_rate           REAL NOT NULL DEFAULT 1.0,
    parent_skill           TEXT,
    deprecation_threshold  REAL NOT NULL DEFAULT 0.3,
    tags                   TEXT NOT NULL DEFAULT '[]',
    body                   TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    trust_level  TEXT NOT NULL,
    success      INTEGER NOT NULL,
    duration_ms  REAL,
    error        TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relational_entries (
    id          TEXT PRIMARY KEY,
    subject     TEXT NOT NULL,
    predicate   TEXT NOT NULL,
    object      TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 1.0,
    source      TEXT NOT NULL DEFAULT 'agent',
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(subject, predicate)
);

CREATE INDEX IF NOT EXISTS idx_episodic_session   ON episodic_entries(session_id);
CREATE INDEX IF NOT EXISTS idx_episodic_created   ON episodic_entries(created_at);
CREATE INDEX IF NOT EXISTS idx_semantic_key        ON semantic_entries(key);
CREATE INDEX IF NOT EXISTS idx_audit_session       ON audit_log(session_id);
CREATE TABLE IF NOT EXISTS trigger_history (
    trigger_name  TEXT PRIMARY KEY,
    last_fire_iso TEXT NOT NULL,
    fire_count    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_relational_subject  ON relational_entries(subject);
CREATE INDEX IF NOT EXISTS idx_relational_pred     ON relational_entries(predicate);

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    model        TEXT NOT NULL,
    title        TEXT,
    started_at   TEXT NOT NULL,
    last_active  TEXT NOT NULL,
    turn_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS session_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    turn_index  INTEGER NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_log_session ON session_log(session_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_sessions_active     ON sessions(last_active DESC);

-- FTS5 Virtual Tables & Sync Triggers

CREATE VIRTUAL TABLE IF NOT EXISTS semantic_fts
USING fts5(key, value, content='semantic_entries', content_rowid='rowid');

CREATE TRIGGER IF NOT EXISTS semantic_entries_ai AFTER INSERT ON semantic_entries BEGIN
  INSERT INTO semantic_fts(rowid, key, value) VALUES (new.rowid, new.key, new.value);
END;
CREATE TRIGGER IF NOT EXISTS semantic_entries_ad AFTER DELETE ON semantic_entries BEGIN
  INSERT INTO semantic_fts(semantic_fts, rowid, key, value) VALUES ('delete', old.rowid, old.key, old.value);
END;
CREATE TRIGGER IF NOT EXISTS semantic_entries_au AFTER UPDATE ON semantic_entries BEGIN
  INSERT INTO semantic_fts(semantic_fts, rowid, key, value) VALUES ('delete', old.rowid, old.key, old.value);
  INSERT INTO semantic_fts(rowid, key, value) VALUES (new.rowid, new.key, new.value);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS skill_fts
USING fts5(name, tags, body, content='skill_genomes', content_rowid='rowid');

CREATE TRIGGER IF NOT EXISTS skill_genomes_ai AFTER INSERT ON skill_genomes BEGIN
  INSERT INTO skill_fts(rowid, name, tags, body) VALUES (new.rowid, new.name, new.tags, new.body);
END;
CREATE TRIGGER IF NOT EXISTS skill_genomes_ad AFTER DELETE ON skill_genomes BEGIN
  INSERT INTO skill_fts(skill_fts, rowid, name, tags, body) VALUES ('delete', old.rowid, old.name, old.tags, old.body);
END;
CREATE TRIGGER IF NOT EXISTS skill_genomes_au AFTER UPDATE ON skill_genomes BEGIN
  INSERT INTO skill_fts(skill_fts, rowid, name, tags, body) VALUES ('delete', old.rowid, old.name, old.tags, old.body);
  INSERT INTO skill_fts(rowid, name, tags, body) VALUES (new.rowid, new.name, new.tags, new.body);
END;
"""


class SQLiteStore:
    """
    Manages the lifecycle of a single SQLite database file.

    Usage:
        store = SQLiteStore("~/.loom/memory.db")
        async with store.connect() as db:
            await EpisodicMemory(db).write(entry)
    """

    def __init__(self, db_path: str = "~/.loom/memory.db") -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def initialize(self) -> None:
        """Create tables and indexes if they do not exist."""
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.commit()
            # Runtime migration: add `embedding` column to existing databases.
            # SQLite doesn't support IF NOT EXISTS on ALTER TABLE, so we
            # attempt the ALTER and silently ignore the "duplicate column" error.
            try:
                await db.execute(
                    "ALTER TABLE semantic_entries ADD COLUMN embedding TEXT"
                )
                await db.commit()
            except Exception:
                pass  # Column already exists — this is expected on all but the first run

            # Ensure FTS indexes are up-to-date with existing content (idempotent, fast)
            try:
                await db.execute("INSERT INTO semantic_fts(semantic_fts) VALUES ('rebuild')")
                await db.execute("INSERT INTO skill_fts(skill_fts) VALUES ('rebuild')")
                await db.commit()
            except Exception:
                pass

    @asynccontextmanager
    async def connect(self):
        """Return an async context-manager that yields an open connection with sqlite-vec."""
        async with aiosqlite.connect(self.path) as db:
            await db.enable_load_extension(True)
            await db.load_extension(sqlite_vec.loadable_path())
            yield db

    @staticmethod
    def _dumps(obj: dict) -> str:
        return json.dumps(obj, ensure_ascii=False)

    @staticmethod
    def _loads(raw: str) -> dict:
        return json.loads(raw)
