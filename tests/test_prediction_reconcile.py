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
    bet_provenance,
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

    async def test_proposal_carries_provenance_from_bet_context(self, db_conn):
        ps = PredictionStore(db_conn)
        pred = _prediction()
        pred.context = "explicit:predict_tool"  # a deliberate wager, not a heartbeat
        await ps.write(pred)
        await _settled_ok_action(db_conn, id="act-1")

        report = await run_prediction_reconciliation(ps, db_conn, execute=False)
        assert report.proposals[0].provenance == "explicit"

    async def test_unsettled_bet_is_skipped_with_reason(self, db_conn):
        ps = PredictionStore(db_conn)
        await ps.write(_prediction(call_id="never-ran"))
        report = await run_prediction_reconciliation(ps, db_conn, execute=False)
        assert report.proposals == []
        assert len(report.skipped) == 1
        assert report.skipped[0].reason == "not_settled"
        assert report.skipped[0].domain == "cli"  # carried for slice-4 attribution


# ---------------------------------------------------------------------------
# Report serialization  (OQ3 — the dream adapter logs this)
# ---------------------------------------------------------------------------

class TestReportSerialization:
    async def test_to_dict_shape(self, db_conn):
        ps = PredictionStore(db_conn)
        good = _prediction(call_id="c-ok")
        bad = _prediction(call_id="c-none")  # never settles
        await ps.write(good)
        await ps.write(bad)
        await _settled_ok_action(db_conn, call_id="c-ok", id="act-ok")

        report = await run_prediction_reconciliation(ps, db_conn, execute=False)
        d = report.to_dict()
        assert d["executed"] is False
        assert d["counts"] == {
            "proposed": 1, "skipped": 1, "scanned": 2,
            "by_provenance": {"other": 1},  # default-context bet (no auto:/explicit: prefix)
        }
        assert d["truncated"] is False
        assert isinstance(d["proposals"], list)
        assert d["proposals"][0]["observation_ref"] == "action:act-ok"
        assert isinstance(d["skipped"], list)
        assert d["skipped"][0]["reason"] == "not_settled"
        assert d["skipped"][0]["domain"] == "cli"


# ---------------------------------------------------------------------------
# Provenance split — measure explicit (predict tool) vs auto (heartbeat) flow
# so the nudge's effect on the monoculture is observable (#560 follow-up)
# ---------------------------------------------------------------------------

class TestBetProvenance:
    def test_auto_heartbeat_contexts(self):
        assert bet_provenance("auto:implicit_tool_success") == "auto"
        assert bet_provenance("auto:implicit_duration_bucket") == "auto"

    def test_explicit_predict_tool_context(self):
        assert bet_provenance("explicit:predict_tool") == "explicit"

    def test_unknown_or_missing_context_is_other(self):
        assert bet_provenance("") == "other"
        assert bet_provenance(None) == "other"
        assert bet_provenance("freeform note") == "other"


class TestProvenanceCounts:
    def _prop(self, provenance):
        return ReconcileProposal(
            prediction_id="p", domain="cli", observation_ref="action:a",
            resolver_kind="tool_success", matched=True, error_score=0.0,
            provenance=provenance,
        )

    def test_summary_breaks_down_proposals_by_provenance(self):
        report = ReconcileReport(
            executed=False,
            proposals=[self._prop("auto"), self._prop("auto"), self._prop("explicit")],
            scanned=3,
        )
        # at-a-glance signal in the dream journal: is the nudge pulling bets?
        assert "auto 2" in report.summary()
        assert "explicit 1" in report.summary()

    def test_to_dict_carries_by_provenance(self):
        report = ReconcileReport(
            executed=False,
            proposals=[self._prop("auto"), self._prop("explicit")],
            scanned=2,
        )
        assert report.to_dict()["counts"]["by_provenance"] == {"auto": 1, "explicit": 1}

    def test_no_proposals_omits_breakdown(self):
        # zero settled bets — keep the summary line clean, no "(auto 0, explicit 0)"
        report = ReconcileReport(executed=False, proposals=[], scanned=0)
        assert "auto" not in report.summary()


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


# ---------------------------------------------------------------------------
# Drain-loop — #557: a pass must drain the whole settleable backlog, not a
# single ≤limit batch. Keyset pagination over (created_at, id) so it sees past
# the window without re-processing, and so concurrent mark_reconciled (settled
# bets leaving the open set) never shifts rows out from under the cursor.
# ---------------------------------------------------------------------------

from datetime import datetime, UTC, timedelta


def _pred_at(call_id, *, seconds, **over):
    """A bet with an explicit, ordered created_at so keyset order is deterministic."""
    p = _prediction(call_id=call_id, **over)
    p.created_at = datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds)
    return p


class TestDrainLoop:
    async def test_drains_backlog_beyond_one_batch(self, db_conn):
        """5 settleable bets, batch_size=2 + the (large) default max_scan: a naive
        single-batch caps at 2; the drain-loop walks 3 batches ([c0,c1] [c2,c3]
        [c4]) and reconciles all 5 in one pass. ``scanned == 5`` is the proof the
        loop went *beyond* one batch — max_scan is left at its default so the
        budget never fires (that path is test_max_scan_budget_marks_truncated)."""
        ps = PredictionStore(db_conn)
        for i in range(5):
            await ps.write(_pred_at(f"c{i}", seconds=i))
            await _settled_ok_action(db_conn, call_id=f"c{i}", id=f"act-{i}")

        report = await run_prediction_reconciliation(
            ps, db_conn, execute=True, batch_size=2
        )
        assert report.scanned == 5          # walked every open bet across 3 batches
        assert len(report.proposals) == 5
        assert report.truncated is False
        assert await ps.list_by_status("pending") == []          # fully drained
        assert len(await ps.list_by_status("reconciled")) == 5

    async def test_keyset_does_not_starve_newer_settleable_behind_unsettleable(self, db_conn):
        """The oldest bet is permanently unsettleable (its action never ran). A
        naive limit-1 window would see only that head and never reach the newer
        settleable bet; keyset advances past it."""
        ps = PredictionStore(db_conn)
        await ps.write(_pred_at("never-ran", seconds=0))
        await ps.write(_pred_at("ran", seconds=1))
        await _settled_ok_action(db_conn, call_id="ran", id="act-ran")

        report = await run_prediction_reconciliation(
            ps, db_conn, execute=True, batch_size=1
        )
        assert len(report.proposals) == 1
        # the settleable bet drained; the unsettleable one remains pending
        pend = await ps.list_by_status("pending")
        assert len(pend) == 1
        assert pend[0].due_condition["call_id"] == "never-ran"

    async def test_dry_run_pages_through_all_without_writing(self, db_conn):
        """Dry-run reports the full backlog picture (not a single-batch sample)
        yet writes nothing — keyset advances the cursor regardless of marking."""
        ps = PredictionStore(db_conn)
        for i in range(5):
            await ps.write(_pred_at(f"c{i}", seconds=i))
            await _settled_ok_action(db_conn, call_id=f"c{i}", id=f"act-{i}")

        report = await run_prediction_reconciliation(
            ps, db_conn, execute=False, batch_size=2
        )
        assert len(report.proposals) == 5                        # full picture
        assert len(await ps.list_by_status("pending")) == 5      # nothing written (I3)
        assert await ps.list_by_status("reconciled") == []

    async def test_max_scan_budget_marks_truncated(self, db_conn):
        """A budget cap stops the drain and surfaces truncated — no silent cap."""
        ps = PredictionStore(db_conn)
        for i in range(5):
            await ps.write(_pred_at(f"c{i}", seconds=i))
            await _settled_ok_action(db_conn, call_id=f"c{i}", id=f"act-{i}")

        report = await run_prediction_reconciliation(
            ps, db_conn, execute=True, batch_size=2, max_scan=2
        )
        assert report.truncated is True
        assert report.scanned <= 2
        assert len(await ps.list_by_status("reconciled")) <= 2
        assert len(await ps.list_by_status("pending")) >= 3       # backlog remains

    async def test_truncated_set_when_max_scan_aligned_with_batch_boundary(self, db_conn):
        """Edge case (絲絲 PR #558 P2): when a full batch consumes exactly the
        remaining budget, the budget check fires at the *next* loop top — distinct
        from a short-page exhaustion. The remainder probe must still detect the
        leftover backlog and set truncated. 5 items, batch_size=4, max_scan=4:
        batch 1 fetches 4 (== budget), loop 2 sees scanned==max_scan, probes, finds
        c4 → truncated. Guards the bool(remainder) probe against regression."""
        ps = PredictionStore(db_conn)
        for i in range(5):
            await ps.write(_pred_at(f"c{i}", seconds=i))
            await _settled_ok_action(db_conn, call_id=f"c{i}", id=f"act-{i}")

        report = await run_prediction_reconciliation(
            ps, db_conn, execute=True, batch_size=4, max_scan=4
        )
        assert report.truncated is True
        assert report.scanned == 4
        assert len(report.proposals) == 4
        assert len(await ps.list_by_status("reconciled")) == 4
        assert len(await ps.list_by_status("pending")) == 1       # exactly c4 remains
