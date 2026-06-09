"""
Prediction Spine — P0.5-a slice B: the auto-betting "mouth" (epic #528, #537).

P0 built the metabolism (store → reconcile → calibrate) but nothing *made* bets,
so the dry-run schedule reconciles an empty table. This slice gives the spine its
involuntary heartbeat: every tool action that actually *executed* leaves behind a
flat implicit bet — "this tool will succeed" — co-written at the lifecycle
persistence seam (``_on_lifecycle``), settled later by the existing reconcile.

``implicit_bet_for`` is the pure half — given a terminal ``ActionRecord`` it
returns the bet to write, or ``None``. TDD-first contract:

* **gated** — ``enabled=False`` makes no bet (consistent with the spine's
  opt-in discipline; nothing flows until DK flips it).
* **executed-only** — only an action whose history reached ``EXECUTING`` is bet
  on. A permission-denied / precondition-aborted action never *tried*, so its
  "failure" is not a world-model miss; betting on it would pollute calibration
  with permission noise (I6-adjacent: calibration measures capability, not
  whether the user allowed the act).
* **shape** — a flat ``tool_success`` bet, ``domain=tool_name``, born
  ``pending``, due ``after_action(call_id)`` so the existing reconcile settles it
  against the very action_record being persisted.
"""

from datetime import datetime, UTC

import pytest
import pytest_asyncio

from loom.core.harness.lifecycle import ActionRecord, ActionState, StateTransition
from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.auto_predict import implicit_bet_for, co_write_implicit_bet
from loom.core.memory.store import SQLiteStore
from loom.core.memory.prediction import PredictionStore


def _record(*, tool="run_bash", session="s1", states, call=True):
    call_obj = (
        ToolCall(id="c1", tool_name=tool, args={},
                 trust_level=TrustLevel.SAFE, session_id=session)
        if call else None
    )
    rec = ActionRecord(call=call_obj)
    prev = ActionState.DECLARED
    for s in states:
        nxt = ActionState(s)
        rec.state_history.append(
            StateTransition(from_state=prev, to_state=nxt,
                            timestamp=datetime.now(UTC), reason=None)
        )
        prev = nxt
    rec.state = prev
    return rec


_EXECUTED = ("authorized", "prepared", "executing", "observed", "committed", "memorialized")
_DENIED = ("awaiting_confirm", "denied")


class TestGate:
    def test_disabled_makes_no_bet(self):
        rec = _record(states=_EXECUTED)
        assert implicit_bet_for(rec, enabled=False) is None

    def test_enabled_executed_makes_a_bet(self):
        rec = _record(states=_EXECUTED)
        assert implicit_bet_for(rec, enabled=True) is not None


class TestExecutedOnly:
    def test_denied_action_is_not_bet_on(self):
        """Permission-denied never executed — not a world-model miss."""
        rec = _record(states=_DENIED)
        assert implicit_bet_for(rec, enabled=True) is None

    def test_aborted_before_executing_is_not_bet_on(self):
        rec = _record(states=("authorized", "prepared", "aborted"))
        assert implicit_bet_for(rec, enabled=True) is None

    def test_callless_record_is_skipped(self):
        rec = _record(states=_EXECUTED, call=False)
        assert implicit_bet_for(rec, enabled=True) is None


class TestBetShape:
    def test_flat_tool_success_bet(self):
        rec = _record(tool="fetch_url", session="sess-9", states=_EXECUTED)
        bet = implicit_bet_for(rec, enabled=True)
        assert bet.resolver == {"kind": "tool_success", "expect": True}
        assert bet.due_condition == {"kind": "after_action", "call_id": "c1"}
        assert bet.domain == "fetch_url"            # per-tool calibration grain
        assert bet.session_id == "sess-9"
        assert "fetch_url" in bet.claim
        assert bet.status == "pending"              # born pending; reconcile settles
        assert bet.score is None
        assert bet.context and bet.context.startswith("auto:")  # provenance tag

    def test_failed_execution_still_makes_a_bet(self):
        """An action that ran and *failed* (executed then aborted) is exactly the
        signal we want — a world-model miss. It still gets a bet; the resolver
        scores it 1.0 at reconcile time."""
        rec = _record(states=("authorized", "prepared", "executing", "aborted"))
        assert implicit_bet_for(rec, enabled=True) is not None


# ---------------------------------------------------------------------------
# co_write_implicit_bet — the isolated IO half (no Session needed)
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_auto_predict.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


class TestCoWrite:
    async def test_executed_action_persists_one_pending_bet(self, store):
        async with store.connect() as db:
            rec = _record(tool="run_bash", states=_EXECUTED)
            wrote = await co_write_implicit_bet(db, rec, enabled=True)
            assert wrote is True
            pending = await PredictionStore(db).list_by_status("pending")
            assert len(pending) == 1
            assert pending[0].domain == "run_bash"
            assert pending[0].context.startswith("auto:")

    async def test_disabled_persists_nothing(self, store):
        async with store.connect() as db:
            rec = _record(states=_EXECUTED)
            wrote = await co_write_implicit_bet(db, rec, enabled=False)
            assert wrote is False
            assert await PredictionStore(db).list_by_status("pending") == []

    async def test_denied_action_persists_nothing(self, store):
        async with store.connect() as db:
            rec = _record(states=_DENIED)
            assert await co_write_implicit_bet(db, rec, enabled=True) is False
            assert await PredictionStore(db).list_by_status("pending") == []

    async def test_write_failure_is_swallowed(self, store, monkeypatch):
        """A betting failure must never crash the lifecycle persistence."""
        async def _boom(self, rec):
            raise RuntimeError("db went sideways")
        monkeypatch.setattr(PredictionStore, "write", _boom)
        async with store.connect() as db:
            rec = _record(states=_EXECUTED)
            # must NOT raise
            assert await co_write_implicit_bet(db, rec, enabled=True) is False
