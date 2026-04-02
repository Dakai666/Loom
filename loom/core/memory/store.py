"""
SQLiteStore — the unified persistence backend for all memory types.

All four memory types (episodic, semantic, procedural, audit) share a
single SQLite file.  Each memory class receives an open `aiosqlite`
connection and operates on its own table(s).
"""

import json
from pathlib import Path

import aiosqlite

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
    updated_at  TEXT NOT NULL
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

CREATE INDEX IF NOT EXISTS idx_episodic_session  ON episodic_entries(session_id);
CREATE INDEX IF NOT EXISTS idx_episodic_created  ON episodic_entries(created_at);
CREATE INDEX IF NOT EXISTS idx_semantic_key       ON semantic_entries(key);
CREATE INDEX IF NOT EXISTS idx_audit_session      ON audit_log(session_id);
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

    def connect(self) -> aiosqlite.Connection:
        """Return an async context-manager that yields an open connection."""
        return aiosqlite.connect(self.path)

    @staticmethod
    def _dumps(obj: dict) -> str:
        return json.dumps(obj, ensure_ascii=False)

    @staticmethod
    def _loads(raw: str) -> dict:
        return json.loads(raw)
