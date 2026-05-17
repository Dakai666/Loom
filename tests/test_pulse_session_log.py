"""Tests for Issue #377 — Pulse injects persist to session_log.

Covers two halves: (1) MemoryPulse now buffers PulseRecord instead of
bare strings, (2) the drain in session.stream_turn (verified directly
against SessionLog here, decoupled from the full session boot) writes
a role='system' row tagged with pulse_type / pulse_source.
"""

from __future__ import annotations

import json
from datetime import datetime, UTC
from pathlib import Path

import aiosqlite
import pytest_asyncio

from loom.core.memory.contradiction import ConflictType, Contradiction, Resolution
from loom.core.memory.pulse import (
    PULSE_TYPE_CONTRADICTION,
    PULSE_TYPE_SESSION_BRIEF,
    MemoryPulse,
    PulseRecord,
)
from loom.core.memory.semantic import SemanticEntry
from loom.core.memory.session_log import SessionLog


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_with_schema(tmp_path: Path):
    """Minimal schema: just the tables Pulse + SessionLog touch."""
    db_path = tmp_path / "pulse.db"
    conn = await aiosqlite.connect(str(db_path))
    await conn.executescript(
        """
        CREATE TABLE sessions (
            session_id TEXT, model TEXT, title TEXT,
            started_at TEXT, last_active TEXT, turn_count INTEGER DEFAULT 0
        );
        CREATE TABLE session_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            turn_index INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            raw_json TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE semantic_entries (
            key TEXT PRIMARY KEY, value TEXT, confidence REAL,
            domain TEXT, temporal TEXT,
            updated_at TEXT, last_accessed_at TEXT
        );
        CREATE TABLE memory_meta (
            key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
        );
        """
    )
    await conn.commit()
    yield conn
    await conn.close()


# ---------------------------------------------------------------------------
# Hook G — session_brief emits PulseRecord
# ---------------------------------------------------------------------------


async def test_hook_g_emits_pulse_record(db_with_schema):
    conn = db_with_schema

    # Seed a prior session + a milestone-class fact touched since then.
    prior_start = "2026-05-01T00:00:00+00:00"
    await conn.execute(
        "INSERT INTO sessions VALUES ('prev','m','t',?,?,0)",
        (prior_start, "2026-05-10T00:00:00+00:00"),
    )
    await conn.execute(
        "INSERT INTO semantic_entries VALUES "
        "('release.v0341','ship dream loop',0.9,'project','milestone',"
        "'2026-05-15T00:00:00+00:00','2026-05-15T00:00:00+00:00')"
    )
    await conn.commit()

    buf: list[PulseRecord] = []
    pulse = MemoryPulse(
        db=conn,
        semantic=None,  # session_brief queries raw SQL, no SemanticMemory call
        session_id="curr",
        session_started_at=datetime.now(UTC),
        pending_buffer=buf,
    )
    await pulse.session_brief()

    assert len(buf) == 1
    rec = buf[0]
    assert isinstance(rec, PulseRecord)
    assert rec.pulse_type == PULSE_TYPE_SESSION_BRIEF
    assert rec.pulse_source == "hook_G"
    assert "release.v0341" in rec.text


async def test_hook_g_noop_when_no_prior_session(db_with_schema):
    buf: list[PulseRecord] = []
    pulse = MemoryPulse(
        db=db_with_schema, semantic=None, session_id="curr",
        session_started_at=datetime.now(UTC), pending_buffer=buf,
    )
    await pulse.session_brief()
    assert buf == []


# ---------------------------------------------------------------------------
# Hook A — contradiction_inject emits PulseRecord keyed by fact key
# ---------------------------------------------------------------------------


def _make_contradiction(key: str, old: str, new: str) -> Contradiction:
    return Contradiction(
        existing=SemanticEntry(key=key, value=old, confidence=0.8),
        proposed=SemanticEntry(key=key, value=new, confidence=0.9),
        conflict_type=ConflictType.KEY_MATCH,
        resolution=Resolution.REPLACE,
    )


async def test_hook_a_emits_pulse_record(db_with_schema):
    buf: list[PulseRecord] = []
    pulse = MemoryPulse(
        db=db_with_schema, semantic=None, session_id="s1",
        session_started_at=datetime.now(UTC), pending_buffer=buf,
    )
    contradiction = _make_contradiction(
        "user.timezone", "UTC", "Asia/Taipei",
    )
    await pulse.contradiction_inject(contradiction)

    assert len(buf) == 1
    rec = buf[0]
    assert rec.pulse_type == PULSE_TYPE_CONTRADICTION
    assert rec.pulse_source == "user.timezone"
    assert "UTC" in rec.text and "Asia/Taipei" in rec.text


async def test_hook_a_once_per_key_per_session(db_with_schema):
    """Second contradiction on the same key in the same session is gated."""
    buf: list[PulseRecord] = []
    pulse = MemoryPulse(
        db=db_with_schema, semantic=None, session_id="s1",
        session_started_at=datetime.now(UTC), pending_buffer=buf,
    )
    c = _make_contradiction("user.tz", "UTC", "Asia/Taipei")
    await pulse.contradiction_inject(c)
    await pulse.contradiction_inject(c)
    assert len(buf) == 1  # second one suppressed by gate


# ---------------------------------------------------------------------------
# Drain → session_log: simulate the session.stream_turn write path
# ---------------------------------------------------------------------------


async def test_drain_writes_system_row_with_pulse_metadata(db_with_schema):
    """Mirrors the session.stream_turn pulse drain: for each PulseRecord,
    call log_message('system', text, metadata={pulse_type, pulse_source}).
    """
    conn = db_with_schema
    log = SessionLog(conn)
    await log.create_session("s1", "test-model")

    records = [
        PulseRecord(text="brief", pulse_type=PULSE_TYPE_SESSION_BRIEF,
                    pulse_source="hook_G"),
        PulseRecord(text="conflict on user.tz", pulse_type=PULSE_TYPE_CONTRADICTION,
                    pulse_source="user.tz"),
    ]
    for rec in records:
        await log.log_message(
            "s1", turn_index=3, role="system", content=rec.text,
            metadata={"pulse_type": rec.pulse_type,
                      "pulse_source": rec.pulse_source},
        )

    cursor = await conn.execute(
        "SELECT role, turn_index, content, metadata FROM session_log "
        "ORDER BY id"
    )
    rows = await cursor.fetchall()
    assert len(rows) == 2
    assert {r[0] for r in rows} == {"system"}
    assert {r[1] for r in rows} == {3}

    meta_by_source = {
        json.loads(r[3])["pulse_source"]: json.loads(r[3]) for r in rows
    }
    assert meta_by_source["hook_G"]["pulse_type"] == PULSE_TYPE_SESSION_BRIEF
    assert meta_by_source["user.tz"]["pulse_type"] == PULSE_TYPE_CONTRADICTION


async def test_system_rows_excluded_from_load_messages(db_with_schema):
    """Pulse rows are observation-only; they must not replay into history
    on session resume (session_log.load_messages already filters role='system').
    """
    conn = db_with_schema
    log = SessionLog(conn)
    await log.create_session("s1", "test-model")
    await log.log_message("s1", 0, "user", "hi")
    await log.log_message(
        "s1", 0, "system", "memory preheat …",
        metadata={"pulse_type": PULSE_TYPE_SESSION_BRIEF,
                  "pulse_source": "hook_G"},
    )

    msgs = await log.load_messages("s1")
    assert [m["role"] for m in msgs] == ["user"]
    assert all("memory preheat" not in m["content"] for m in msgs)
