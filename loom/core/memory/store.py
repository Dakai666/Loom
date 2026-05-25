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
    id              TEXT PRIMARY KEY,
    key             TEXT UNIQUE NOT NULL,
    value           TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 1.0,
    source          TEXT,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    embedding       TEXT,                                              -- JSON float array; NULL until first embed
    -- Memory Ontology v0.1 (issue #281): three-axis classification.
    -- See loom/core/memory/ontology.py for canonical values.
    domain          TEXT NOT NULL DEFAULT 'knowledge',
    temporal        TEXT NOT NULL DEFAULT 'recent',
    last_accessed_at TEXT                                              -- ISO timestamp; updated on recall hit
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

-- Issue #451 phase B: ``relational_entries`` table retired. Triples
-- live in ``semantic_entries`` under key prefix ``rel:{subject}::{predicate}``
-- (see ``loom/core/memory/relational_bridge.py``). Legacy DBs still
-- carrying the table get a one-shot backfill + DROP in
-- ``_backfill_relational_into_semantic`` below.

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

-- Issue #142: Agent self-observability snapshots (one row per dimension per session)
CREATE TABLE IF NOT EXISTS agent_telemetry (
    dimension   TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (dimension, session_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_telemetry_updated ON agent_telemetry(updated_at DESC);

-- Issue #281 P3: Memory subsystem meta kv (lifecycle.last_run_at, …)
CREATE TABLE IF NOT EXISTS memory_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

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
        # #342 — at-rest baseline. Tighten the parent dir before the
        # first connect so the freshly-created DB inherits a private
        # directory; tighten the DB + WAL/SHM siblings on the way out
        # of initialize.
        from loom.core.infra.file_permissions import tighten_dir, tighten_sqlite_db
        tighten_dir(self.path.parent)
        async with aiosqlite.connect(self.path) as db:
            # Issue #166: wait up to 5s on a write lock instead of failing
            # immediately with `database is locked`. Pairs with WAL mode set in
            # SCHEMA — WAL allows concurrent readers, but writes still serialize.
            await db.execute("PRAGMA busy_timeout = 5000;")
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
                # Memory Ontology v0.1 (issue #281): three-axis classification on
                # semantic facts. See loom/core/memory/ontology.py.
                "ALTER TABLE semantic_entries ADD COLUMN domain TEXT NOT NULL DEFAULT 'knowledge'",
                "ALTER TABLE semantic_entries ADD COLUMN temporal TEXT NOT NULL DEFAULT 'recent'",
                "ALTER TABLE semantic_entries ADD COLUMN last_accessed_at TEXT",
                # #451 phase A relational_entries ALTERs kept for legacy DBs
                # that still have the table — backfill + DROP runs further
                # below. New DBs never see this column scheme.
                "ALTER TABLE relational_entries ADD COLUMN domain TEXT NOT NULL DEFAULT 'knowledge'",
                "ALTER TABLE relational_entries ADD COLUMN temporal TEXT NOT NULL DEFAULT 'recent'",
                "ALTER TABLE relational_entries ADD COLUMN last_accessed_at TEXT",
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
                # Memory Ontology v0.1 (issue #281): filter by (domain, temporal)
                # is the primary recall path for axis-aware queries.
                "CREATE INDEX IF NOT EXISTS idx_semantic_domain_temporal "
                "ON semantic_entries(domain, temporal)",
            ]:
                try:
                    await db.execute(_index_sql)
                    await db.commit()
                except Exception:
                    pass

            # Issue #451 phase B: final backfill + drop of relational_entries.
            # Backfill is idempotent (INSERT OR IGNORE on rel:* keys); DROP
            # runs only if the table is present. Legacy DBs migrate cleanly
            # on the first boot after this commit; fresh installs never see
            # the table at all.
            try:
                await self._backfill_relational_into_semantic(db)
                await db.execute("DROP TABLE IF EXISTS relational_entries")
                await db.commit()
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(
                    "Relational table retirement (backfill+drop) failed: %s",
                    exc,
                )

            # Ensure FTS indexes are up-to-date with existing content (idempotent, fast)
            try:
                await db.execute("INSERT INTO semantic_fts(semantic_fts) VALUES ('rebuild')")
                await db.execute("INSERT INTO skill_fts(skill_fts) VALUES ('rebuild')")
                await db.commit()
            except Exception:
                pass

        # #342 — tighten the DB file + WAL/SHM siblings now that the
        # connection has been closed and any side files have flushed.
        tighten_sqlite_db(self.path)

    @asynccontextmanager
    async def connect(self):
        """Return an async context-manager that yields an open connection with sqlite-vec."""
        async with aiosqlite.connect(self.path) as db:
            await db.enable_load_extension(True)
            await db.load_extension(sqlite_vec.loadable_path())
            # Issue #166: wait up to 5s on a write lock under concurrency
            # (Discord daemon + CLI session + Dreaming jobs hitting WAL together)
            # instead of immediately raising `database is locked`.
            await db.execute("PRAGMA busy_timeout = 5000;")
            yield db

    @staticmethod
    async def _backfill_relational_into_semantic(db: aiosqlite.Connection) -> None:
        """Copy every ``relational_entries`` row into ``semantic_entries`` with
        a ``rel:{subject}::{predicate}`` key. Uses ``INSERT OR IGNORE`` so
        re-runs are no-ops once the row exists.

        Skips silently when the legacy table is absent — fresh installs (and
        users who have already migrated through #451 phase B) never carry
        the table.

        Embeddings are *not* populated here — running provider calls inline
        with startup would slow boot. ``LoomSession.start()`` calls
        ``SemanticMemory.ensure_embeddings_for_prefix("rel:")`` once the
        embedding provider is available.
        """
        # Skip if the table doesn't exist (post-#451-phase-B installs).
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='relational_entries'"
        )
        if await cursor.fetchone() is None:
            return

        cursor = await db.execute(
            "SELECT id, subject, predicate, object, confidence, source, metadata, "
            "created_at, updated_at, domain, temporal, last_accessed_at "
            "FROM relational_entries"
        )
        rows = await cursor.fetchall()
        if not rows:
            return

        inserted = 0
        for (
            rel_id, subject, predicate, obj, confidence, source, metadata_raw,
            created_at, updated_at, domain, temporal, last_accessed_at,
        ) in rows:
            try:
                md = json.loads(metadata_raw) if metadata_raw else {}
            except Exception:
                md = {}
            md["subject"] = subject
            md["predicate"] = predicate
            md["object"] = obj
            key = f"rel:{subject}::{predicate}"
            value = f"{subject} {predicate} {obj}"
            # #451 phase B P1 fix: phase-A dual-write was best-effort
            # (semantic write failures were silently swallowed), so a row
            # may exist in semantic but be stale relative to its
            # authoritative relational source. INSERT OR IGNORE would
            # discard the relational update; instead, upsert with a
            # ``WHERE excluded.updated_at > semantic_entries.updated_at``
            # guard so the legacy table always wins on newer timestamps
            # but never overwrites a semantic row that was updated AFTER
            # phase B's bridge writers took over.
            cur = await db.execute(
                """
                INSERT INTO semantic_entries
                    (id, key, value, confidence, source, metadata,
                     created_at, updated_at, domain, temporal, last_accessed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value      = excluded.value,
                    confidence = excluded.confidence,
                    source     = excluded.source,
                    metadata   = excluded.metadata,
                    updated_at = excluded.updated_at,
                    domain     = excluded.domain,
                    temporal   = excluded.temporal,
                    last_accessed_at = excluded.last_accessed_at
                WHERE excluded.updated_at > semantic_entries.updated_at
                """,
                (
                    rel_id,
                    key,
                    value,
                    confidence,
                    source,
                    json.dumps(md, ensure_ascii=False),
                    created_at,
                    updated_at,
                    domain or "knowledge",
                    temporal or "recent",
                    last_accessed_at,
                ),
            )
            if cur.rowcount and cur.rowcount > 0:
                inserted += 1
        await db.commit()
        if inserted:
            import logging
            logging.getLogger(__name__).info(
                "Backfilled %d relational triples into semantic store "
                "(issue #451 phase A).", inserted,
            )

    @staticmethod
    def _dumps(obj: dict) -> str:
        return json.dumps(obj, ensure_ascii=False)

    @staticmethod
    def _loads(raw: str) -> dict:
        return json.loads(raw)
