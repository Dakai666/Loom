"""
Prediction Spine — P0 slice 1 contract tests (epic #528, spec docs/designs/58 §2/§3/§8).

Covers the lifeline invariants the data model must hold *before* any
reconciliation pipeline exists:

* **I1** — every prediction must carry the ``(claim, due_condition, resolver)``
  triple; a record missing any leg cannot be written.
* **I4 (seed)** — status is an explicit state machine; a prediction is born
  ``pending`` and only a *reconciled* record carries a score. You cannot fake
  a verified bet at birth, and reconciling requires an ``observation_ref``
  (the structural seed of I2: a score must point at a ground-truth observation).

These are red-first: ``loom.core.memory.prediction`` does not exist yet.
"""

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.memory.prediction import PredictionRecord, PredictionStore


# ---------------------------------------------------------------------------
# Fixtures — fresh temp DB per test (mirrors tests/test_memory.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_prediction.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def db_conn(store):
    async with store.connect() as db:
        yield db


def _record(**overrides) -> PredictionRecord:
    """A fully-populated, writable prediction (all three legs present)."""
    base = dict(
        session_id="sess-1",
        claim="loom test suite will exit 0",
        due_condition={"kind": "after_action", "call_id": "call-42"},
        resolver={"kind": "tool_success", "expect": True},
        domain="cli",
    )
    base.update(overrides)
    return PredictionRecord(**base)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchema:
    async def test_initialize_creates_prediction_table(self, db_conn):
        cur = await db_conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='prediction_records'"
        )
        assert await cur.fetchone() is not None


# ---------------------------------------------------------------------------
# I1 — the (claim, due_condition, resolver) triple is mandatory
# ---------------------------------------------------------------------------

class TestI1RequiredTriple:
    async def test_full_triple_writes_and_reads_back(self, db_conn):
        ps = PredictionStore(db_conn)
        rec = _record()
        await ps.write(rec)

        got = await ps.get(rec.id)
        assert got is not None
        assert got.claim == rec.claim
        assert got.due_condition == rec.due_condition
        assert got.resolver == rec.resolver
        assert got.domain == "cli"

    @pytest.mark.parametrize("missing", ["claim", "due_condition", "resolver"])
    async def test_missing_leg_is_rejected(self, db_conn, missing):
        ps = PredictionStore(db_conn)
        empty = "" if missing == "claim" else {}
        rec = _record(**{missing: empty})
        with pytest.raises(ValueError):
            await ps.write(rec)

    async def test_whitespace_only_claim_is_rejected(self, db_conn):
        ps = PredictionStore(db_conn)
        with pytest.raises(ValueError):
            await ps.write(_record(claim="   "))


# ---------------------------------------------------------------------------
# I4 (seed) — status state machine; only reconciled carries a score
# ---------------------------------------------------------------------------

class TestStatusLifecycle:
    async def test_fresh_record_is_pending_with_no_score(self, db_conn):
        ps = PredictionStore(db_conn)
        rec = _record()
        await ps.write(rec)
        got = await ps.get(rec.id)
        assert got.status == "pending"
        assert got.score is None
        assert got.observation_ref is None
        assert got.reconciled_at is None

    async def test_cannot_be_born_reconciled(self, db_conn):
        """I4: a bet cannot be 'verified' at birth — no ground truth yet."""
        ps = PredictionStore(db_conn)
        with pytest.raises(ValueError):
            await ps.write(_record(status="reconciled"))

    async def test_invalid_status_rejected(self, db_conn):
        ps = PredictionStore(db_conn)
        with pytest.raises(ValueError):
            await ps.write(_record(status="bogus"))

    async def test_mark_due_pending_to_due(self, db_conn):
        ps = PredictionStore(db_conn)
        rec = _record()
        await ps.write(rec)
        await ps.mark_due(rec.id)
        assert (await ps.get(rec.id)).status == "due"

    async def test_reconcile_requires_observation_ref(self, db_conn):
        """Structural seed of I2: a score must point at an observation."""
        ps = PredictionStore(db_conn)
        rec = _record()
        await ps.write(rec)
        with pytest.raises(ValueError):
            await ps.mark_reconciled(rec.id, score=0.0, observation_ref="")

    async def test_reconcile_sets_score_ref_and_timestamp(self, db_conn):
        ps = PredictionStore(db_conn)
        rec = _record()
        await ps.write(rec)
        await ps.mark_reconciled(
            rec.id, score=0.25, observation_ref="action:call-42"
        )
        got = await ps.get(rec.id)
        assert got.status == "reconciled"
        assert got.score == 0.25
        assert got.observation_ref == "action:call-42"
        assert got.reconciled_at is not None

    async def test_double_reconcile_is_rejected(self, db_conn):
        """Idempotency seed — a reconciled bet is terminal."""
        ps = PredictionStore(db_conn)
        rec = _record()
        await ps.write(rec)
        await ps.mark_reconciled(rec.id, score=0.1, observation_ref="action:x")
        with pytest.raises(ValueError):
            await ps.mark_reconciled(rec.id, score=0.9, observation_ref="action:y")

    async def test_mark_stale_pending_to_stale(self, db_conn):
        ps = PredictionStore(db_conn)
        rec = _record()
        await ps.write(rec)
        await ps.mark_stale(rec.id)
        got = await ps.get(rec.id)
        assert got.status == "stale"
        assert got.score is None  # stale never carries a score

    async def test_cannot_stale_a_reconciled_record(self, db_conn):
        ps = PredictionStore(db_conn)
        rec = _record()
        await ps.write(rec)
        await ps.mark_reconciled(rec.id, score=0.0, observation_ref="action:x")
        with pytest.raises(ValueError):
            await ps.mark_stale(rec.id)

    async def test_list_by_status_filters(self, db_conn):
        ps = PredictionStore(db_conn)
        a, b, c = _record(), _record(), _record()
        await ps.write(a)
        await ps.write(b)
        await ps.write(c)
        await ps.mark_due(b.id)
        await ps.mark_reconciled(c.id, score=0.0, observation_ref="action:x")

        pending = await ps.list_by_status("pending")
        due = await ps.list_by_status("due")
        reconciled = await ps.list_by_status("reconciled")
        assert {r.id for r in pending} == {a.id}
        assert {r.id for r in due} == {b.id}
        assert {r.id for r in reconciled} == {c.id}
