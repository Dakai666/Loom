"""
Tests for ConvergentDreamLoop — the weekly read-only convergent-dream schedule
(#495, P4a).

Unlike DreamLoop (a simple in-process interval), the convergent dream runs
weekly and must be **restart-safe**: the due-ness is decided against a
persisted ``consolidation_dream.last_run`` in ``memory_meta``, not an in-memory
timer, so a daemon restart neither resets the week nor double-runs. A failed
pass must NOT mark last_run — it retries next check rather than silently
skipping the week.
"""

from __future__ import annotations

from datetime import datetime, UTC, timedelta

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.infra import AbortController
from loom.autonomy.maintenance import ConvergentDreamLoop


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def db_conn(store):
    async with store.connect() as conn:
        yield conn


def _loop(db, dream_fn, *, interval_days=7.0):
    return ConvergentDreamLoop(
        dream_fn=dream_fn, db=db, abort=AbortController(), interval_days=interval_days)


class TestDueGating:
    async def test_runs_when_never_run_before(self, db_conn):
        calls = []
        async def fn(): calls.append(1)
        ran = await _loop(db_conn, fn).maybe_run_once(datetime.now(UTC))
        assert ran is True
        assert len(calls) == 1

    async def test_skips_within_interval(self, db_conn):
        calls = []
        async def fn(): calls.append(1)
        loop = _loop(db_conn, fn, interval_days=7)
        now = datetime(2026, 6, 6, tzinfo=UTC)
        await loop.maybe_run_once(now)                       # runs, marks now
        ran = await loop.maybe_run_once(now + timedelta(days=3))
        assert ran is False
        assert len(calls) == 1

    async def test_runs_again_after_interval(self, db_conn):
        calls = []
        async def fn(): calls.append(1)
        loop = _loop(db_conn, fn, interval_days=7)
        now = datetime(2026, 6, 6, tzinfo=UTC)
        await loop.maybe_run_once(now)
        ran = await loop.maybe_run_once(now + timedelta(days=8))
        assert ran is True
        assert len(calls) == 2

    async def test_restart_safe_via_meta(self, db_conn):
        # a fresh loop instance (simulating a daemon restart) must read the
        # persisted last_run and stay within the week.
        calls = []
        async def fn(): calls.append(1)
        now = datetime(2026, 6, 6, tzinfo=UTC)
        await _loop(db_conn, fn).maybe_run_once(now)         # marks
        ran = await _loop(db_conn, fn).maybe_run_once(now + timedelta(days=2))
        assert ran is False
        assert len(calls) == 1


class TestFailureRetries:
    async def test_failure_does_not_mark_last_run(self, db_conn):
        # a failed pass must retry next check, never silently skip the week.
        async def boom(): raise RuntimeError("llm down")
        now = datetime(2026, 6, 6, tzinfo=UTC)
        with pytest.raises(RuntimeError):
            await _loop(db_conn, boom).maybe_run_once(now)

        ran_calls = []
        async def ok(): ran_calls.append(1)
        # one minute later it is still due (last_run was never written)
        ran = await _loop(db_conn, ok).maybe_run_once(now + timedelta(minutes=1))
        assert ran is True
        assert len(ran_calls) == 1


class TestCorruptMeta:
    async def test_unparseable_last_run_treated_as_due(self, db_conn):
        await db_conn.execute(
            "INSERT INTO memory_meta(key, value, updated_at) VALUES (?, ?, ?)",
            ("consolidation_dream.last_run", "not-a-date", "x"))
        await db_conn.commit()
        calls = []
        async def fn(): calls.append(1)
        ran = await _loop(db_conn, fn).maybe_run_once(datetime.now(UTC))
        assert ran is True
        assert len(calls) == 1
