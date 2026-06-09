"""
Prediction Spine — slice 4.5 scheduled calibration isolation (epic #528).

``_run_scheduled_calibration`` rolls reconciled bets into the persistent
``calibration:<domain>`` residue on the weekly dream cadence. Like its
reconcile sibling (PR #533), two schedule-level properties matter:

* **Independent gate.** Calibration writing is gated on ``calibration_write_enabled``,
  **separate** from ``reconcile_execute`` (絲絲 PR #534): flipping reconcile's
  execute must NOT start writing calibration. ``enabled=False`` (the shipped
  default) writes nothing.
* **Failure isolation.** A calibration failure must not propagate out of the
  weekly loop (which would roll back the consolidation + reconcile that already
  ran). Failures are swallowed + logged; the loop continues.
"""

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.memory.semantic import SemanticMemory
from loom.core.memory.prediction import PredictionRecord, PredictionStore
from loom.autonomy.daemon import _run_scheduled_calibration


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_sched_calibration.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def db_conn(store):
    async with store.connect() as db:
        yield db


async def _seed_reconciled(db, domain="cli", *, score=0.0):
    ps = PredictionStore(db)
    pred = PredictionRecord(
        session_id="s1", claim="bet",
        due_condition={"kind": "after_action", "call_id": "c1"},
        resolver={"kind": "tool_success", "expect": True},
        domain=domain,
    )
    await ps.write(pred)
    await ps.mark_reconciled(pred.id, score=score, observation_ref="action:a1")
    return pred


class TestGateIndependence:
    async def test_disabled_writes_no_residue(self, db_conn):
        await _seed_reconciled(db_conn, "cli", score=0.0)
        await _run_scheduled_calibration(db_conn, enabled=False)
        assert await SemanticMemory(db_conn).get("calibration:cli") is None

    async def test_enabled_writes_residue(self, db_conn):
        await _seed_reconciled(db_conn, "cli", score=0.0)
        await _run_scheduled_calibration(db_conn, enabled=True)
        entry = await SemanticMemory(db_conn).get("calibration:cli")
        assert entry is not None
        assert entry.confidence == pytest.approx(1.0)


class TestFailureIsolation:
    async def test_calibration_raise_is_swallowed(self, db_conn, monkeypatch):
        async def _boom(*a, **k):
            raise RuntimeError("calibration went sideways")

        monkeypatch.setattr(
            "loom.core.cognition.calibration.run_calibration_pass", _boom,
        )
        # must NOT raise — the weekly loop continues past a calibration failure
        await _run_scheduled_calibration(db_conn, enabled=True)
