"""
Prediction Spine — slice 3.5: prediction_reconcile tool adapter (epic #528).

The ToolDefinition that lets the reconciliation pipeline run on the convergent
dream's schedule (and be triggered manually). Mirrors make_convergent_dream_tool.

Review focus (絲絲): **I3 at the schedule level**. The function-level read-only
guarantee (slice 3) must survive being wired into a scheduled/triggerable tool.
The adapter enforces it the same way the convergent dream does — ``dry_run``
defaults to **True**, so the schedule produces a report without writing the
spine until ``dry_run=false`` is deliberately set.
"""

import json

import pytest
import pytest_asyncio

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.core.memory.store import SQLiteStore
from loom.core.memory.prediction import PredictionRecord, PredictionStore


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_reconcile_tool.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def db_conn(store):
    async with store.connect() as db:
        yield db


def _make_call(args: dict) -> ToolCall:
    return ToolCall(id="t1", tool_name="prediction_reconcile", args=args,
                    trust_level=TrustLevel.SAFE, session_id="s1")


async def _seed_settleable(db):
    """One pending bet whose tool call has settled successfully."""
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


class TestReconcileTool:
    def test_tool_is_safe(self):
        from loom.core.memory.maintenance import make_prediction_reconcile_tool
        tool = make_prediction_reconcile_tool(object())
        assert tool.trust_level == TrustLevel.SAFE
        assert tool.name == "prediction_reconcile"

    async def test_dry_run_default_is_read_only(self, db_conn, tmp_path):
        """I3 at the schedule level: the default scheduled pass writes nothing."""
        from loom.core.memory.maintenance import make_prediction_reconcile_tool
        pred = await _seed_settleable(db_conn)

        tool = make_prediction_reconcile_tool(db_conn, dreams_dir=tmp_path)
        result = await tool.executor(_make_call({}))  # no dry_run → defaults True
        assert result.success is True

        after = await PredictionStore(db_conn).get(pred.id)
        assert after.status == "pending"     # untouched
        assert after.score is None

    async def test_report_is_written(self, db_conn, tmp_path):
        from loom.core.memory.maintenance import make_prediction_reconcile_tool
        await _seed_settleable(db_conn)
        tool = make_prediction_reconcile_tool(db_conn, dreams_dir=tmp_path)
        await tool.executor(_make_call({}))
        assert list(tmp_path.glob("*.md"))  # a dated report landed

    async def test_execute_reconciles(self, db_conn, tmp_path):
        from loom.core.memory.maintenance import make_prediction_reconcile_tool
        pred = await _seed_settleable(db_conn)

        tool = make_prediction_reconcile_tool(db_conn, dreams_dir=tmp_path)
        result = await tool.executor(_make_call({"dry_run": False}))
        assert result.success is True

        after = await PredictionStore(db_conn).get(pred.id)
        assert after.status == "reconciled"
        assert after.score == 0.0
        assert after.observation_ref == "action:act-1"

    async def test_calibration_write_is_independent_of_dry_run(self, db_conn, tmp_path):
        """絲絲 PR #534: the calibration write is gated on its own arg, NOT folded
        into reconcile's execute. dry_run=false commits scores but writes NO
        calibration unless write_calibration=true is set deliberately."""
        from loom.core.memory.maintenance import make_prediction_reconcile_tool
        from loom.core.memory.semantic import SemanticMemory
        await _seed_settleable(db_conn)

        tool = make_prediction_reconcile_tool(db_conn, dreams_dir=tmp_path)
        # reconcile commits, but calibration stays unwritten
        await tool.executor(_make_call({"dry_run": False}))
        assert await SemanticMemory(db_conn).get("calibration:cli") is None

    async def test_write_calibration_persists_residue(self, db_conn, tmp_path):
        from loom.core.memory.maintenance import make_prediction_reconcile_tool
        from loom.core.memory.semantic import SemanticMemory
        await _seed_settleable(db_conn)

        tool = make_prediction_reconcile_tool(db_conn, dreams_dir=tmp_path)
        await tool.executor(_make_call({"dry_run": False, "write_calibration": True}))
        entry = await SemanticMemory(db_conn).get("calibration:cli")
        assert entry is not None
        assert entry.confidence == pytest.approx(1.0)
