"""Tests for Issue #475 — persisted ``sessions.model`` must track the active model.

``create_session`` writes the base/default model once at creation. After a tier
escalation or a ``/model`` switch the model that actually serves diverges, but the
persisted row never moved — so ``loom sessions list`` reported the wrong model
(evidence: ledger session ``2bb9e21f`` served on codex/gpt-5.5 yet stored
``minimax-m2.7``).

Fix: ``update_session`` accepts an optional ``model`` and writes it through the
same per-turn update path, with COALESCE so callers that omit it (and the
existing positional callers) preserve the stored value.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest_asyncio

from loom.core.memory.session_log import SessionLog


@pytest_asyncio.fixture
async def session_log(tmp_path: Path):
    conn = await aiosqlite.connect(str(tmp_path / "sessions.db"))
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
        """
    )
    await conn.commit()
    log = SessionLog(conn)
    yield log
    await conn.close()


async def test_update_session_persists_active_model(session_log: SessionLog):
    """A model passed to update_session lands in the persisted row."""
    await session_log.create_session("s1", "minimax-m2.7", title=None)

    await session_log.update_session(
        "s1", turn_count=3, last_active="2026-06-11T00:00:00+00:00",
        title="hi", model="codex/gpt-5.5",
    )

    row = await session_log.get_session("s1")
    assert row["model"] == "codex/gpt-5.5"
    assert row["turn_count"] == 3


async def test_update_session_omitting_model_preserves_stored(session_log: SessionLog):
    """Callers that don't pass model (e.g. legacy positional) keep the stored value."""
    await session_log.create_session("s1", "minimax-m2.7", title=None)

    # Legacy positional signature: (session_id, turn_count, last_active, title)
    await session_log.update_session(
        "s1", 1, "2026-06-11T00:00:00+00:00", None,
    )

    row = await session_log.get_session("s1")
    assert row["model"] == "minimax-m2.7"


async def test_update_session_none_model_does_not_clobber(session_log: SessionLog):
    """Explicit model=None is a no-op on the stored model (COALESCE guard)."""
    await session_log.create_session("s1", "minimax-m2.7", title=None)

    await session_log.update_session(
        "s1", turn_count=2, last_active="2026-06-11T00:00:00+00:00",
        title=None, model=None,
    )

    row = await session_log.get_session("s1")
    assert row["model"] == "minimax-m2.7"
