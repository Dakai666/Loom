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
    fold_contradiction_pulses,
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


# ---------------------------------------------------------------------------
# Issue #378 — fold_contradiction_pulses: drain-side noise reduction
# ---------------------------------------------------------------------------


def _make_contradiction_record(key: str, old: str, new: str,
                                resolution: str = "replace") -> PulseRecord:
    text = (
        f"Memory contradiction on key={key!r} (resolution: {resolution}):\n"
        f"  existing: {old}\n"
        f"  proposed: {new}\n"
        f"Reconcile if the new value should override the old."
    )
    return PulseRecord(
        text=text,
        pulse_type=PULSE_TYPE_CONTRADICTION,
        pulse_source=key,
        details={"key": key, "existing": old, "proposed": new,
                 "resolution": resolution},
    )


def test_fold_renders_header_bullets_and_footer():
    records = [
        _make_contradiction_record("user.tz", "UTC", "Asia/Taipei"),
        _make_contradiction_record("user.lang", "en", "zh-TW"),
        _make_contradiction_record("project.codename", "Loom", "Loom v0.4"),
    ]
    body = fold_contradiction_pulses(records)

    # Header announces the count.
    assert "3 stored facts conflict" in body
    # Every key appears in its own bullet.
    for key in ("user.tz", "user.lang", "project.codename"):
        assert f"- {key}" in body
    # Old + new values surface inline.
    assert '"UTC" → proposed "Asia/Taipei"' in body
    # Footer instructs the agent to reconcile.
    assert body.rstrip().endswith("Reconcile if the new values should override the old.")
    # No overflow marker for a small batch.
    assert "more (see session_log" not in body


def test_fold_truncates_pathological_batch():
    """A compression batch with >20 contradictions still produces a single
    inject; the bulleted list is capped and an overflow line tells the
    agent where to find the rest."""
    records = [
        _make_contradiction_record(f"fact:{i}", f"old{i}", f"new{i}")
        for i in range(25)
    ]
    body = fold_contradiction_pulses(records)

    assert "25 stored facts conflict" in body
    # Only the first 20 bullets are rendered.
    assert "- fact:19" in body
    assert "- fact:20" not in body
    # Overflow marker mentions remaining count.
    assert "+5 more (see session_log" in body


def test_fold_uses_pulse_source_when_details_missing():
    """A PulseRecord without details (e.g. from an older code path) still
    folds — falls back to pulse_source and 'pending' / '?' placeholders
    instead of crashing."""
    bare = PulseRecord(
        text="legacy text", pulse_type=PULSE_TYPE_CONTRADICTION,
        pulse_source="legacy.key", details=None,
    )
    body = fold_contradiction_pulses([bare, bare])
    assert "- legacy.key (pending)" in body
    assert 'existing "?"' in body


def test_fold_empty_returns_empty_string():
    assert fold_contradiction_pulses([]) == ""


# ---------------------------------------------------------------------------
# Drain simulation — mirror session.stream_turn's branching against the
# real SessionLog. End-to-end would need a full session boot, so we
# reproduce the drain loop here and assert the observable contract:
#   • messages list grows by exactly the expected number of reminders
#   • session_log gets one row per PulseRecord regardless of folding
# ---------------------------------------------------------------------------


def _simulate_drain(pulses: list[PulseRecord]) -> list[str]:
    """Pure-Python mirror of the session.py drain branching for
    contradictions. Returns the list of reminder bodies that would be
    appended to ``self.messages``."""
    contradictions = [r for r in pulses if r.pulse_type == PULSE_TYPE_CONTRADICTION]
    others = [r for r in pulses if r.pulse_type != PULSE_TYPE_CONTRADICTION]
    out = [r.text for r in others]
    if len(contradictions) >= 2:
        out.append(fold_contradiction_pulses(contradictions))
    else:
        out.extend(r.text for r in contradictions)
    return out


async def test_drain_folds_multiple_contradictions_into_single_reminder(db_with_schema):
    conn = db_with_schema
    log = SessionLog(conn)
    await log.create_session("s1", "test-model")

    pulses = [
        _make_contradiction_record("k1", "old1", "new1"),
        _make_contradiction_record("k2", "old2", "new2"),
        _make_contradiction_record("k3", "old3", "new3"),
        PulseRecord(text="preheat", pulse_type=PULSE_TYPE_SESSION_BRIEF,
                    pulse_source="hook_G"),
    ]

    reminders = _simulate_drain(pulses)
    # 1 brief + 1 folded contradiction reminder — NOT 4.
    assert len(reminders) == 2
    folded = next(r for r in reminders if r.startswith("Memory contradictions —"))
    for key in ("k1", "k2", "k3"):
        assert f"- {key}" in folded

    # session_log still records every individual pulse — analytics
    # granularity is unchanged by folding.
    for record in pulses:
        await log.log_message(
            "s1", turn_index=7, role="system", content=record.text,
            metadata={"pulse_type": record.pulse_type,
                      "pulse_source": record.pulse_source},
        )
    cursor = await conn.execute(
        "SELECT COUNT(*), "
        "SUM(CASE WHEN json_extract(metadata,'$.pulse_type')='contradiction' "
        "         THEN 1 ELSE 0 END) "
        "FROM session_log WHERE role='system'"
    )
    total, contradictions_logged = await cursor.fetchone()
    assert total == 4
    assert contradictions_logged == 3


def test_drain_single_contradiction_uses_original_text():
    """N=1 should NOT fold — agent sees the original verbose multi-line
    notice (preserves the standalone-friendly form Hook A wrote)."""
    rec = _make_contradiction_record("user.tz", "UTC", "Asia/Taipei")
    reminders = _simulate_drain([rec])
    assert reminders == [rec.text]
    assert "Memory contradictions —" not in reminders[0]  # fold header absent
    assert "Memory contradiction on key=" in reminders[0]  # original singular form


def test_drain_passes_through_non_contradiction_pulses_unchanged():
    brief = PulseRecord(
        text="preheat lines …",
        pulse_type=PULSE_TYPE_SESSION_BRIEF, pulse_source="hook_G",
    )
    reminders = _simulate_drain([brief])
    assert reminders == [brief.text]
