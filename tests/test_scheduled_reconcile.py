"""
Prediction Spine — slice 3.5 scheduled reconcile isolation (epic #528, PR #533).

``_run_scheduled_reconcile`` is the daemon's hook that runs the reconciliation
pipeline on the weekly dream cadence. Two properties matter at the schedule
level (絲絲 review):

* **P3 — config default is no-run.** ``enabled=False`` (the shipped default)
  must not touch the spine at all.
* **P2 — failure isolation.** A reconcile that raises must NOT propagate out of
  the weekly loop (which would roll back the consolidation pass that already
  ran). Failures are swallowed + logged; the loop continues.
"""

import json

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.memory.prediction import PredictionRecord, PredictionStore
from loom.autonomy.daemon import _run_scheduled_reconcile


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_sched_reconcile.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def db_conn(store):
    async with store.connect() as db:
        yield db


async def _seed_settleable(db):
    ps = PredictionStore(db)
    pred = PredictionRecord(
        session_id="s1",
        claim="run_bash will succeed",
        due_condition={"kind": "after_action", "call_id": "c1"},
        resolver={"kind": "tool_success", "expect": True},
        domain="cli",
    )
    await ps.write(pred)
    history = [{"from": "x", "to": s, "ts": "2026-06-08T00:00:00+00:00"}
               for s in ("executing", "committed", "memorialized")]
    await db.execute(
        "INSERT INTO action_records "
        "(id, envelope_id, session_id, turn_index, tool_name, call_id, "
        " final_state, duration_ms, state_history, created_at) "
        "VALUES ('act-1', 'env', 's1', 0, 'run_bash', 'c1', 'memorialized', 50.0, ?, ?)",
        (json.dumps(history), "2026-06-08T00:00:00+00:00"),
    )
    await db.commit()
    return pred


class TestConfigDefaultIsNoRun:
    async def test_disabled_does_not_touch_spine(self, db_conn):
        pred = await _seed_settleable(db_conn)
        await _run_scheduled_reconcile(db_conn, enabled=False, execute=True)
        # even with execute=True, disabled means nothing ran
        assert (await PredictionStore(db_conn).get(pred.id)).status == "pending"

    async def test_enabled_execute_reconciles(self, db_conn):
        pred = await _seed_settleable(db_conn)
        await _run_scheduled_reconcile(db_conn, enabled=True, execute=True)
        after = await PredictionStore(db_conn).get(pred.id)
        assert after.status == "reconciled"
        assert after.score == 0.0


class TestFailureIsolation:
    async def test_reconcile_raise_is_swallowed(self, db_conn, monkeypatch):
        """A raising reconcile must not propagate — the weekly loop continues."""
        async def _boom(*a, **k):
            raise RuntimeError("schema went sideways")

        monkeypatch.setattr(
            "loom.core.cognition.prediction_reconcile.run_prediction_reconciliation",
            _boom,
        )
        # must NOT raise
        await _run_scheduled_reconcile(db_conn, enabled=True, execute=False)
