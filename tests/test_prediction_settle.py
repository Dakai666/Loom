"""
In-session settlement of explicit ``predict`` bets (#528 retirement, 2026-09-17).

The batch reconcile / calibration / affect layers were retired: nearly all of
their corpus was the auto-heartbeat's hard-coded ``expect: fast`` /
``expect: true``, and the dawn report narrated those constants as the agent's own
beliefs. What survives is the deliberate ``predict`` bet, and its one valuable
"after" is **seeing the verdict while still present**. So a bet now settles the
moment its target tool runs, and the verdict rides on that tool's result.

Contract pinned here:

* A ``next_action`` bet settles against the first terminal action of the named
  tool in the same session, created after the bet's anchor — scored, stamped
  with the ``observation_ref`` (ground truth only, I2), and a verdict line
  returned.
* Other tools / other sessions / actions before the anchor never settle it.
* A bet the settling action can't judge (resolver can't read its field) goes
  ``stale`` with an explicit "could not judge" line — never a silent score, and
  never left rotting ``pending`` (the next run is not the predicted one).
* Terminal bets are never re-settled.
"""

from __future__ import annotations

import json

import pytest_asyncio

from loom.core.memory.observation import capture_output_fields
from loom.core.memory.prediction import PredictionRecord, PredictionStore
from loom.core.memory.prediction_settle import (
    format_settlement_suffix,
    settle_bets_for_action,
)
from loom.core.memory.store import SQLiteStore

ANCHOR = "2026-09-17T01:00:00+00:00"
BEFORE = "2026-09-17T00:59:00+00:00"
AFTER = "2026-09-17T01:05:00+00:00"


@pytest_asyncio.fixture
async def db(tmp_path):
    s = SQLiteStore(str(tmp_path / "settle.db"))
    await s.initialize()
    async with s.connect() as conn:
        yield conn


async def _bet(db, *, claim="probe prints INJECTION_ALLOWED", session="sess",
               tool="run_bash", resolver=None) -> PredictionRecord:
    rec = PredictionRecord(
        session_id=session,
        claim=claim,
        due_condition={"kind": "next_action", "session_id": session,
                       "tool": tool, "after": ANCHOR},
        resolver=resolver or {"kind": "output_contains",
                              "needle": "INJECTION_ALLOWED"},
        domain=tool,
        context="explicit:predict_tool",
    )
    await PredictionStore(db).write(rec)
    return rec


async def _action(db, *, id="a1", session="sess", tool="run_bash",
                  created_at=AFTER, output="INJECTION_ALLOWED", success=True,
                  capture=True):
    history = json.dumps([{"from": "executing", "to": "memorialized",
                           "ts": created_at}])
    cols = ("id, envelope_id, session_id, turn_index, tool_name, call_id, "
            "final_state, duration_ms, state_history, created_at")
    vals = [id, "e1", session, 0, tool, f"call-{id}", "memorialized", 12.0,
            history, created_at]
    if capture:
        cap = capture_output_fields(output, success=success, error=None)
        cols += ", output_prefix, output_digest, output_len, output_rows"
        vals += [cap["output_prefix"], cap["output_digest"], cap["output_len"],
                 cap["output_rows"]]
    await db.execute(
        f"INSERT INTO action_records ({cols}) VALUES ({','.join('?' * len(vals))})",
        vals,
    )
    await db.commit()


class TestSettles:
    async def test_hit_is_scored_and_reported(self, db):
        bet = await _bet(db)
        await _action(db, output="CASE4 INJECTION_ALLOWED")

        lines = await settle_bets_for_action(db, session_id="sess", tool_name="run_bash")

        rec = await PredictionStore(db).get(bet.id)
        assert rec.status == "reconciled"
        assert rec.score == 0.0
        assert rec.observation_ref == "action:a1"
        assert len(lines) == 1
        assert "probe prints INJECTION_ALLOWED" in lines[0]
        assert "HIT" in lines[0]

    async def test_miss_is_scored_and_reported(self, db):
        bet = await _bet(db)
        await _action(db, output="blocked")

        lines = await settle_bets_for_action(db, session_id="sess", tool_name="run_bash")

        assert (await PredictionStore(db).get(bet.id)).score == 1.0
        assert "MISS" in lines[0]


class TestDoesNotSettle:
    async def test_other_tool(self, db):
        bet = await _bet(db, tool="fetch_url")
        await _action(db, tool="run_bash")
        assert await settle_bets_for_action(db, session_id="sess", tool_name="run_bash") == []
        assert (await PredictionStore(db).get(bet.id)).status == "pending"

    async def test_other_session(self, db):
        bet = await _bet(db, session="other")
        await _action(db, session="sess")
        assert await settle_bets_for_action(db, session_id="sess", tool_name="run_bash") == []
        assert (await PredictionStore(db).get(bet.id)).status == "pending"

    async def test_action_before_anchor(self, db):
        bet = await _bet(db)
        await _action(db, created_at=BEFORE)
        assert await settle_bets_for_action(db, session_id="sess", tool_name="run_bash") == []
        assert (await PredictionStore(db).get(bet.id)).status == "pending"

    async def test_terminal_bet_not_resettled(self, db):
        bet = await _bet(db)
        await _action(db)
        await settle_bets_for_action(db, session_id="sess", tool_name="run_bash")
        await _action(db, id="a2", created_at="2026-09-17T01:06:00+00:00")
        assert await settle_bets_for_action(db, session_id="sess", tool_name="run_bash") == []
        assert (await PredictionStore(db).get(bet.id)).observation_ref == "action:a1"


class TestUnjudgeable:
    async def test_goes_stale_with_explicit_line(self, db):
        """Settling row carries no output capture → the resolver can't read it.
        Not a silent score, not left pending forever."""
        bet = await _bet(db)
        await _action(db, capture=False)

        lines = await settle_bets_for_action(db, session_id="sess", tool_name="run_bash")

        rec = await PredictionStore(db).get(bet.id)
        assert rec.status == "stale"
        assert rec.score is None
        assert "could not judge" in lines[0]


class TestSuffix:
    def test_empty_is_empty(self):
        assert format_settlement_suffix([]) == ""

    def test_lines_render_as_trailing_block(self):
        out = format_settlement_suffix(["a", "b"])
        assert out.startswith("\n\n")
        assert "a" in out and "b" in out


class TestSessionWiring:
    async def test_on_lifecycle_buffers_verdict_by_call_id(self, db):
        """The session settles on persist and buffers the verdict under the
        settling call id — stream_turn pops it onto that tool's result."""
        from datetime import datetime, timedelta

        from loom.core.harness.lifecycle import ActionRecord, ActionState
        from loom.core.harness.middleware import ToolCall, ToolResult
        from loom.core.harness.permissions import TrustLevel
        from loom.core.session import LoomSession

        session = LoomSession.__new__(LoomSession)
        session._db = db
        session.session_id = "sess"
        session._envelope_counter = 0
        session._turn_index = 0
        session._predict_tool_enabled = True
        session._bet_settlements = {}

        await _bet(db)  # anchored at ANCHOR
        record = ActionRecord(
            state=ActionState.MEMORIALIZED,
            call=ToolCall(id="call-probe", tool_name="run_bash", args={},
                          trust_level=TrustLevel.SAFE, session_id="sess"),
            result=ToolResult(call_id="call-probe", tool_name="run_bash",
                              success=True, output="INJECTION_ALLOWED"),
            created_at=datetime.fromisoformat(ANCHOR) + timedelta(minutes=1),
        )
        await session._on_lifecycle(record)

        assert "HIT" in session._bet_settlements["call-probe"][0]

    async def test_disabled_tool_does_not_settle(self, db):
        from loom.core.harness.lifecycle import ActionRecord, ActionState
        from loom.core.harness.middleware import ToolCall, ToolResult
        from loom.core.harness.permissions import TrustLevel
        from loom.core.session import LoomSession

        session = LoomSession.__new__(LoomSession)
        session._db = db
        session.session_id = "sess"
        session._envelope_counter = 0
        session._turn_index = 0
        session._predict_tool_enabled = False
        session._bet_settlements = {}

        bet = await _bet(db)
        await session._on_lifecycle(ActionRecord(
            state=ActionState.MEMORIALIZED,
            call=ToolCall(id="c", tool_name="run_bash", args={},
                          trust_level=TrustLevel.SAFE, session_id="sess"),
            result=ToolResult(call_id="c", tool_name="run_bash", success=True,
                              output="INJECTION_ALLOWED"),
        ))
        assert session._bet_settlements == {}
        assert (await PredictionStore(db).get(bet.id)).status == "pending"
