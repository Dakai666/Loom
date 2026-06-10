"""
Prediction Spine — explicit ``predict`` tool (epic #528, P0.5-a slice A, #537).

slice B's heartbeat only ever bets ``tool_success @ expect=True`` — flat, always
optimistic, one-dimensional. The ``predict`` tool lets Loom make *deliberate*,
confidence-varying, resolver-rich wagers ("this grep returns 0 rows", "output
contains PASS", "this will be slow") — the non-trivial signal the §8 acceptance
gate needs. It is the memory-layer sibling of ``prediction_reconcile``: SAFE,
writes one ``pending`` bet, settled later by the existing reconcile via the new
``next_action`` due-condition.

Contract pinned here (red first):

* **SAFE, named** — writes a meta-memory bet, drives no behaviour.
* **next_action due** — built from the *call's* ``session_id``, the named
  ``tool``, and an ``after`` anchor, so the existing reconcile settles it
  against the next ``action_records`` row (I2-clean; no ``call_id`` needed).
* **resolver fail-fast** — an unknown resolver kind is refused (no bet written):
  P0 refuses to score what it cannot judge mechanically (I2), and refusing at
  *write* time beats writing an unresolvable bet that silently rots.
* **provenance** — an explicit bet is tagged distinctly from the heartbeat so
  later analysis can tell deliberate wagers apart.
* **round-trip** — predict → the tool runs → reconcile scores it against the
  observed action.
"""

import json
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.core.memory.store import SQLiteStore
from loom.core.memory.prediction import PredictionStore


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_predict_tool.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def db_conn(store):
    async with store.connect() as db:
        yield db


def _make_call(args: dict, *, session="s1") -> ToolCall:
    return ToolCall(id="t1", tool_name="predict", args=args,
                    trust_level=TrustLevel.SAFE, session_id=session)


async def _insert_action(db, *, id, session, tool, created_at,
                         final_state="memorialized", output=None, duration_ms=120.0):
    states = ["authorized", "executing", "observed", "committed", "memorialized"]
    hist = [{"from": "x", "to": s, "ts": created_at} for s in states]
    await db.execute(
        "INSERT INTO action_records "
        "(id, envelope_id, session_id, turn_index, tool_name, call_id, "
        " final_state, duration_ms, state_history, created_at) "
        "VALUES (?, 'env', ?, 0, ?, ?, ?, ?, ?, ?)",
        (id, session, tool, f"call-{id}", final_state, duration_ms,
         json.dumps(hist), created_at),
    )
    await db.commit()


async def _only_bet(db):
    pending = await PredictionStore(db).list_by_status("pending")
    assert len(pending) == 1
    return pending[0]


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

class TestToolShape:
    def test_tool_is_safe_and_named(self):
        from loom.core.memory.maintenance import make_predict_tool
        tool = make_predict_tool(object())
        assert tool.name == "predict"
        assert tool.trust_level == TrustLevel.SAFE


class TestWritesBet:
    async def test_writes_pending_next_action_bet(self, db_conn):
        from loom.core.memory.maintenance import make_predict_tool
        tool = make_predict_tool(db_conn)
        result = await tool.executor(_make_call({
            "claim": "next run_bash returns 0 rows",
            "tool": "run_bash",
            "resolver": {"kind": "row_count", "expect": 0},
        }))
        assert result.success is True

        bet = await _only_bet(db_conn)
        assert bet.status == "pending"
        assert bet.session_id == "s1"                       # from the call
        assert bet.due_condition["kind"] == "next_action"
        assert bet.due_condition["session_id"] == "s1"
        assert bet.due_condition["tool"] == "run_bash"
        assert bet.due_condition["after"]                   # anchor stamped
        assert bet.resolver == {"kind": "row_count", "expect": 0}
        assert bet.score is None

    async def test_domain_defaults_to_tool(self, db_conn):
        from loom.core.memory.maintenance import make_predict_tool
        tool = make_predict_tool(db_conn)
        await tool.executor(_make_call({
            "claim": "x", "tool": "fetch_url",
            "resolver": {"kind": "tool_success", "expect": True},
        }))
        assert (await _only_bet(db_conn)).domain == "fetch_url"

    async def test_domain_overridable(self, db_conn):
        from loom.core.memory.maintenance import make_predict_tool
        tool = make_predict_tool(db_conn)
        await tool.executor(_make_call({
            "claim": "x", "tool": "run_bash", "domain": "git",
            "resolver": {"kind": "tool_success", "expect": True},
        }))
        assert (await _only_bet(db_conn)).domain == "git"

    async def test_explicit_bet_is_tagged_distinctly(self, db_conn):
        """Provenance: an explicit wager must be tellable from the heartbeat's
        ``auto:`` bets (which carry ``auto:implicit_tool_success``)."""
        from loom.core.memory.maintenance import make_predict_tool
        tool = make_predict_tool(db_conn)
        await tool.executor(_make_call({
            "claim": "x", "tool": "run_bash",
            "resolver": {"kind": "tool_success", "expect": True},
        }))
        bet = await _only_bet(db_conn)
        assert bet.context and not bet.context.startswith("auto:")


# ---------------------------------------------------------------------------
# Fail-fast validation — no half-written bets
# ---------------------------------------------------------------------------

class TestValidation:
    async def test_unknown_resolver_kind_is_refused(self, db_conn):
        """I2: P0 refuses to score what it cannot judge mechanically. Refuse at
        write time rather than persist an unresolvable bet that silently rots."""
        from loom.core.memory.maintenance import make_predict_tool
        tool = make_predict_tool(db_conn)
        result = await tool.executor(_make_call({
            "claim": "vibes are good", "tool": "run_bash",
            "resolver": {"kind": "vibe_check"},
        }))
        assert result.success is False
        assert await PredictionStore(db_conn).list_by_status("pending") == []

    async def test_missing_claim_is_refused(self, db_conn):
        from loom.core.memory.maintenance import make_predict_tool
        tool = make_predict_tool(db_conn)
        result = await tool.executor(_make_call({
            "tool": "run_bash", "resolver": {"kind": "tool_success"},
        }))
        assert result.success is False
        assert await PredictionStore(db_conn).list_by_status("pending") == []

    async def test_missing_tool_is_refused(self, db_conn):
        from loom.core.memory.maintenance import make_predict_tool
        tool = make_predict_tool(db_conn)
        result = await tool.executor(_make_call({
            "claim": "x", "resolver": {"kind": "tool_success"},
        }))
        assert result.success is False
        assert await PredictionStore(db_conn).list_by_status("pending") == []

    async def test_missing_resolver_is_refused(self, db_conn):
        from loom.core.memory.maintenance import make_predict_tool
        tool = make_predict_tool(db_conn)
        result = await tool.executor(_make_call({"claim": "x", "tool": "run_bash"}))
        assert result.success is False
        assert await PredictionStore(db_conn).list_by_status("pending") == []


# ---------------------------------------------------------------------------
# Round-trip — predict → action → reconcile
# ---------------------------------------------------------------------------

class TestRoundTrip:
    async def test_predict_then_reconcile_scores_the_bet(self, db_conn):
        from loom.core.memory.maintenance import make_predict_tool
        from loom.core.cognition.prediction_reconcile import run_prediction_reconciliation

        tool = make_predict_tool(db_conn)
        await tool.executor(_make_call({
            "claim": "next run_bash will succeed",
            "tool": "run_bash",
            "resolver": {"kind": "tool_success", "expect": True},
        }, session="sess-rt"))
        bet = await _only_bet(db_conn)

        # the predicted tool runs *after* the bet's anchor
        after = datetime.fromisoformat(bet.due_condition["after"])
        later = (after + timedelta(seconds=1)).isoformat()
        await _insert_action(db_conn, id="ran", session="sess-rt",
                             tool="run_bash", created_at=later)

        report = await run_prediction_reconciliation(
            PredictionStore(db_conn), db_conn, execute=True)
        assert report.executed
        scored = await PredictionStore(db_conn).get(bet.id)
        assert scored.status == "reconciled"
        assert scored.score == 0.0                     # predicted success, got success
        assert scored.observation_ref == "action:ran"
