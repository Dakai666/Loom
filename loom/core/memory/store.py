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
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    content       TEXT NOT NULL,
    metadata      TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    compressed_at TEXT  -- NULL = uncompressed; ISO timestamp once folded into semantic memory
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
    details      TEXT NOT NULL DEFAULT '{}',
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
-- Note: idx_episodic_session_uncompressed (partial index on WHERE compressed_at
-- IS NULL) is built in the runtime migration block below, **after** the ALTER
-- TABLE that adds the column. Defining it here would break upgrades from
-- pre-compressed_at databases — the executescript would fail on the first
-- initialize() call because the partial predicate references a column that
-- doesn't exist yet.
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
    raw_json    TEXT,             -- tool_use/tool_result blocks preserved as JSON; NULL for plain text
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_log_session ON session_log(session_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_session_log_role    ON session_log(session_id, role);
CREATE INDEX IF NOT EXISTS idx_sessions_active     ON sessions(last_active DESC);

-- Issue #42: Action lifecycle records
CREATE TABLE IF NOT EXISTS action_records (
    id             TEXT PRIMARY KEY,
    envelope_id    TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    turn_index     INTEGER NOT NULL,
    tool_name      TEXT NOT NULL,
    call_id        TEXT NOT NULL DEFAULT '',
    final_state    TEXT NOT NULL,
    intent_summary TEXT,
    scope          TEXT NOT NULL DEFAULT 'general',
    duration_ms    REAL NOT NULL DEFAULT 0.0,
    state_history  TEXT NOT NULL DEFAULT '[]',
    has_rollback   INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_action_session ON action_records(session_id);
CREATE INDEX IF NOT EXISTS idx_action_state   ON action_records(final_state);
CREATE INDEX IF NOT EXISTS idx_action_env     ON action_records(envelope_id);

-- Issue #120 PR 2: Skill candidate pool (pre-promotion revisions)
CREATE TABLE IF NOT EXISTS skill_candidates (
    id                 TEXT PRIMARY KEY,
    parent_skill_name  TEXT NOT NULL,
    parent_version     INTEGER NOT NULL,
    candidate_body     TEXT NOT NULL,
    mutation_strategy  TEXT NOT NULL,
    diagnostic_keys    TEXT NOT NULL DEFAULT '[]',  -- JSON list of SemanticEntry keys
    origin_session_id  TEXT,
    status             TEXT NOT NULL DEFAULT 'generated',
    pareto_scores      TEXT NOT NULL DEFAULT '{}',  -- JSON dict (task_type → score)
    notes              TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skill_candidates_parent ON skill_candidates(parent_skill_name);
CREATE INDEX IF NOT EXISTS idx_skill_candidates_status ON skill_candidates(status);

-- Issue #120 PR 3: SKILL.md version history (snapshot before each promote/rollback)
CREATE TABLE IF NOT EXISTS skill_version_history (
    id                   TEXT PRIMARY KEY,
    skill_name           TEXT NOT NULL,
    version              INTEGER NOT NULL,
    body                 TEXT NOT NULL,
    reason               TEXT NOT NULL DEFAULT 'promote',  -- 'promote' | 'rollback' | 'manual'
    source_candidate_id  TEXT,                             -- NULL for rollbacks / manual archives
    archived_at          TEXT NOT NULL,
    UNIQUE(skill_name, version, archived_at)
);

CREATE INDEX IF NOT EXISTS idx_skill_history_name    ON skill_version_history(skill_name);
CREATE INDEX IF NOT EXISTS idx_skill_history_version ON skill_version_history(skill_name, version);

-- Issue #142: Agent self-observability snapshots (one row per dimension per session)
CREATE TABLE IF NOT EXISTS agent_telemetry (
    dimension   TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (dimension, session_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_telemetry_updated ON agent_telemetry(updated_at DESC);

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
            # Runtime migrations: ALTER TABLE for columns added in later versions.
            # SQLite doesn't support IF NOT EXISTS on ALTER TABLE, so we
            # attempt each ALTER and silently ignore "duplicate column" errors.
            for _migration in [
                "ALTER TABLE semantic_entries ADD COLUMN embedding TEXT",
                # Issue #11: add raw_json column to capture tool_use/tool_result
                # blocks separately from the human-readable content field.
                "ALTER TABLE session_log ADD COLUMN raw_json TEXT",
                # Issue #43: governance events stored in audit_log.details
                "ALTER TABLE audit_log ADD COLUMN details TEXT NOT NULL DEFAULT '{}'",
                # Issue #64: skill-declared precondition check references
                "ALTER TABLE skill_genomes ADD COLUMN precondition_check_refs TEXT NOT NULL DEFAULT '[]'",
                # Issue #142 soft-delete: mark compressed entries instead of deleting
                # them so compression losses can be audited and recovered.
                "ALTER TABLE episodic_entries ADD COLUMN compressed_at TEXT",
                # Issue #120 PR 4: maturity tag on skill genomes (mature / needs_improvement)
                "ALTER TABLE skill_genomes ADD COLUMN maturity_tag TEXT",
                # Issue #120 PR 4: fast-track flag bypasses shadow phase when Grader
                # proves ≥20% pass-rate improvement over the previous version.
                "ALTER TABLE skill_candidates ADD COLUMN fast_track INTEGER NOT NULL DEFAULT 0",
            ]:
                try:
                    await db.execute(_migration)
                    await db.commit()
                except Exception:
                    pass  # Column already exists — expected on all but the first run
            # Ensure new indexes exist even on pre-existing databases.
            for _index_sql in [
                "CREATE INDEX IF NOT EXISTS idx_session_log_role ON session_log(session_id, role)",
                "CREATE INDEX IF NOT EXISTS idx_episodic_session_uncompressed "
                "ON episodic_entries(session_id) WHERE compressed_at IS NULL",
            ]:
                try:
                    await db.execute(_index_sql)
                    await db.commit()
                except Exception:
                    pass

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
