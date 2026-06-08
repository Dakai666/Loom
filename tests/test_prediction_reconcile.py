"""
Prediction Spine — P0 slice 3: reconciliation pipeline (epic #528, spec §5).

The convergent-dream sibling that closes the spine's loop:

    pending bet → find settling observation → resolve ref → apply resolver
                → propose (dry-run) / mark_reconciled (execute)

Review focus (絲絲): the **dry-run report shape** and **I3 at the function
level** — ``execute=False`` must be read-only *by construction* (the only write
path is gated behind ``execute=True``). The report is an inert, auditable
projection of "what would happen": each proposal names the prediction, the
observation it was judged against, the resolver, and the resulting error_score.
"""

import json

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.memory.prediction import PredictionRecord, PredictionStore
from loom.core.cognition.prediction_reconcile import (
    run_prediction_reconciliation,
    ReconcileReport,
    ReconcileProposal,
)


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_reconcile.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def db_conn(store):
    async with store.connect() as db:
        yield db


async def _insert_action(db, *, id, call_id, final_state, history_to_states, duration_ms=120.0):
    history = [{"from": "x", "to": s, "ts": "2026-06-08T00:00:00+00:00"} for s in history_to_states]
    await db.execute(
        "INSERT INTO action_records "
        "(id, envelope_id, session_id, turn_index, tool_name, call_id, "
        " final_state, duration_ms, state_history, created_at) "
        "VALUES (?, 'env', 'sess', 0, 'run_bash', ?, ?, ?, ?, ?)",
        (id, call_id, final_state, duration_ms, json.dumps(history), "2026-06-08T00:00:00+00:00"),
    )
    await db.commit()


def _prediction(call_id="c1", **resolver_over) -> PredictionRecord:
    resolver = {"kind": "tool_success", "expect": True}
    resolver.update(resolver_over)
    return PredictionRecord(
        session_id="sess",
        claim="this run_bash will succeed",
        due_condition={"kind": "after_action", "call_id": call_id},
        resolver=resolver,
        domain="cli",
    )


async def _settled_ok_action(db, call_id="c1", id="act-1"):
    await _insert_action(
        db, id=id, call_id=call_id, final_state="memorialized",
        history_to_states=["authorized", "executing", "observed", "committed", "memorialized"],
    )


# ---------------------------------------------------------------------------
# dry-run report shape  (絲絲 review focus #1)
# ---------------------------------------------------------------------------

class TestDryRunReportShape:
    async def test_proposal_is_auditable(self, db_conn):
        ps = PredictionStore(db_conn)
        pred = _prediction()
        await ps.write(pred)
        await _settled_ok_action(db_conn, id="act-1")

        report = await run_prediction_reconciliation(ps, db_conn, execute=False)
        assert isinstance(report, ReconcileReport)
        assert report.executed is False
        assert len(report.proposals) == 1

        prop = report.proposals[0]
        assert isinstance(prop, ReconcileProposal)
        assert prop.prediction_id == pred.id
        assert prop.domain == "cli"
        assert prop.observation_ref == "action:act-1"   # auditable, reverse-resolvable
        assert prop.resolver_kind == "tool_success"
        assert prop.matched is True
        assert prop.error_score == 0.0
        assert prop.detail  # non-empty resolver detail

    async def test_unsettled_bet_is_skipped_with_reason(self, db_conn):
        ps = PredictionStore(db_conn)
        await ps.write(_prediction(call_id="never-ran"))
        report = await run_prediction_reconciliation(ps, db_conn, execute=False)
        assert report.proposals == []
        assert len(report.skipped) == 1
        assert report.skipped[0].reason == "not_settled"
        assert report.skipped[0].domain == "cli"  # carried for slice-4 attribution


# ---------------------------------------------------------------------------
# I3 at the function level — dry-run is read-only by construction
# ---------------------------------------------------------------------------

class TestDryRunIsReadOnly:
    async def test_dry_run_does_not_mutate_prediction(self, db_conn):
        ps = PredictionStore(db_conn)
        pred = _prediction()
        await ps.write(pred)
        await _settled_ok_action(db_conn)

        await run_prediction_reconciliation(ps, db_conn, execute=False)

        after = await ps.get(pred.id)
        assert after.status == "pending"      # untouched
        assert after.score is None
        assert after.observation_ref is None
        assert after.reconciled_at is None

    async def test_dry_run_and_execute_propose_the_same(self, db_conn):
        ps = PredictionStore(db_conn)
        pred = _prediction()
        await ps.write(pred)
        await _settled_ok_action(db_conn)

        dry = await run_prediction_reconciliation(ps, db_conn, execute=False)
        # re-write so the second pass sees a fresh pending bet
        wet = await run_prediction_reconciliation(ps, db_conn, execute=True)
        assert [p.error_score for p in dry.proposals] == [p.error_score for p in wet.proposals]


# ---------------------------------------------------------------------------
# execute path — writes the score + observation_ref
# ---------------------------------------------------------------------------

class TestExecute:
    async def test_execute_reconciles(self, db_conn):
        ps = PredictionStore(db_conn)
        pred = _prediction()
        await ps.write(pred)
        await _settled_ok_action(db_conn, id="act-9")

        report = await run_prediction_reconciliation(ps, db_conn, execute=True)
        assert report.executed is True

        after = await ps.get(pred.id)
        assert after.status == "reconciled"
        assert after.score == 0.0
        assert after.observation_ref == "action:act-9"
        assert after.reconciled_at is not None

    async def test_wrong_prediction_scores_one(self, db_conn):
        ps = PredictionStore(db_conn)
        pred = _prediction()  # expects success
        await ps.write(pred)
        await _insert_action(
            db_conn, id="act-bad", call_id="c1", final_state="memorialized",
            history_to_states=["authorized", "executing", "aborted", "memorialized"],
        )
        report = await run_prediction_reconciliation(ps, db_conn, execute=True)
        assert report.proposals[0].error_score == 1.0
        assert (await ps.get(pred.id)).score == 1.0

    async def test_execute_is_idempotent(self, db_conn):
        ps = PredictionStore(db_conn)
        pred = _prediction()
        await ps.write(pred)
        await _settled_ok_action(db_conn)

        await run_prediction_reconciliation(ps, db_conn, execute=True)
        second = await run_prediction_reconciliation(ps, db_conn, execute=True)
        # already reconciled — nothing left to do, no double-reconcile error
        assert second.proposals == []

    async def test_resolver_mismatch_is_skipped_not_scored_zero(self, db_conn):
        """A resolver needing a field the observation lacks is unresolvable,
        not a free 0.0 (no silent pass)."""
        ps = PredictionStore(db_conn)
        pred = _prediction()
        pred.resolver = {"kind": "output_contains", "needle": "X", "expect": True}
        await ps.write(pred)
        await _settled_ok_action(db_conn)  # action obs has no "output" field

        report = await run_prediction_reconciliation(ps, db_conn, execute=True)
        assert report.proposals == []
        assert len(report.skipped) == 1
        assert report.skipped[0].reason == "unresolvable"
        assert (await ps.get(pred.id)).status == "pending"
