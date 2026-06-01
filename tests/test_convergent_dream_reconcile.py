"""
Tests for convergent-dream reconcile execution (#490, P3, slice 1).

P2 left reconcile clusters in skipped_other. P3 wires them: an approved
reconcile cluster is classified (LLM) as either
  - "merge"     — same fact, different phrasing → synthesize + consolidate
  - "arbitrate" — genuinely opposing → trust/recency winner kept, loser
                  redirected to the winner with its value preserved as the
                  superseded belief.
Both arms reuse the P2 consolidate() primitive. Fail-safe: unclear/failed
classification skips the cluster (never a blind contradiction resolution).
"""

from __future__ import annotations

import json
import pytest
import pytest_asyncio
from datetime import datetime, UTC

from loom.core.memory.store import SQLiteStore
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.cognition.consolidation import (
    CandidateCluster,
    ConsolidationPlan,
    ReviewDecision,
    KIND_RECONCILE,
    VERDICT_APPROVE,
    classify_reconcile,
    execute_plan,
)


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


@pytest_asyncio.fixture
async def semantic(db_conn):
    return SemanticMemory(db_conn)


def _failing_llm():
    async def fn(messages):
        raise RuntimeError("down")
    return fn


def _reconcile_llm(kind: str):
    """Stub: classify → kind; synth (merge arm) → fused fact."""
    async def fn(messages):
        sys = messages[0]["content"]
        if "synthesizer" in sys:
            return '{"refined_value": "fused fact", "rationale": "same"}'
        if "opposing" in sys.lower():
            return json.dumps({"kind": kind, "reason": "stub"})
        return "[]"
    return fn


async def _seed_reconcile(semantic, *, a_src="manual", b_src="session:x:fact:0"):
    a = SemanticEntry(key="loc:a", value="the user lives in Taipei", source=a_src,
                      created_at=datetime(2026, 1, 1, tzinfo=UTC))
    b = SemanticEntry(key="loc:b", value="the user lives in Tokyo now", source=b_src,
                      created_at=datetime(2026, 5, 1, tzinfo=UTC))
    await semantic.upsert(a)
    await semantic.upsert(b)
    a, b = await semantic.get("loc:a"), await semantic.get("loc:b")
    cluster = CandidateCluster(cluster_id="rc1", kind=KIND_RECONCILE, members=[a, b])
    plan = ConsolidationPlan(clusters=[cluster])
    plan.source_versions = {"loc:a": a.updated_at.isoformat(), "loc:b": b.updated_at.isoformat()}
    plan.decisions = [ReviewDecision(cluster_id="rc1", verdict=VERDICT_APPROVE)]
    return plan


# ---------------------------------------------------------------------------
# classify_reconcile
# ---------------------------------------------------------------------------

class TestClassifyReconcile:
    def _cluster(self):
        return CandidateCluster(cluster_id="c", kind=KIND_RECONCILE, members=[
            SemanticEntry(key="a", value="x", source="manual"),
            SemanticEntry(key="b", value="y", source="manual")])

    async def test_returns_merge(self):
        assert await classify_reconcile(self._cluster(), _reconcile_llm("merge")) == "merge"

    async def test_returns_arbitrate(self):
        assert await classify_reconcile(self._cluster(), _reconcile_llm("arbitrate")) == "arbitrate"

    async def test_llm_failure_returns_skip(self):
        assert await classify_reconcile(self._cluster(), _failing_llm()) == "skip"

    async def test_unknown_kind_returns_skip(self):
        assert await classify_reconcile(self._cluster(), _reconcile_llm("banana")) == "skip"


# ---------------------------------------------------------------------------
# execute_plan — reconcile arm
# ---------------------------------------------------------------------------

class TestReconcileExecution:
    async def test_arbitrate_keeps_higher_trust_winner(self, semantic):
        plan = await _seed_reconcile(semantic)  # loc:a=manual(1.0), loc:b=session(0.8)
        result = await execute_plan(semantic, plan, _reconcile_llm("arbitrate"))

        # winner = higher trust (manual), value unchanged
        winner = await semantic.get("loc:a")
        assert winner.value == "the user lives in Taipei"
        # loser redirected to winner, its value preserved as superseded belief
        loser = await semantic.get("loc:b")
        assert loser.metadata.get("redirected_to") == "loc:a"
        ci = winner.metadata["consolidated_into"]
        assert any("Tokyo" in s["value"] for s in ci["sacrificed_entries"])
        assert "loc:a" in result.executed

    async def test_arbitrate_tie_trust_newer_wins(self, semantic):
        # both manual → tie → newer updated_at wins. loc:b created later.
        plan = await _seed_reconcile(semantic, b_src="manual")
        result = await execute_plan(semantic, plan, _reconcile_llm("arbitrate"))
        # loc:b is newer (created 2026-05) → winner; loc:a redirected
        winner = await semantic.get("loc:b")
        assert winner.metadata.get("redirected_to") is None
        assert (await semantic.get("loc:a")).metadata.get("redirected_to") == "loc:b"

    async def test_merge_classification_fuses(self, semantic):
        plan = await _seed_reconcile(semantic)
        result = await execute_plan(semantic, plan, _reconcile_llm("merge"))
        # merge arm → synthesize + consolidate; survivor value = fused
        # survivor is the higher-trust loc:a
        assert (await semantic.get("loc:a")).value == "fused fact"
        assert (await semantic.get("loc:b")).metadata.get("redirected_to") == "loc:a"

    async def test_skip_classification_does_not_execute(self, semantic):
        plan = await _seed_reconcile(semantic)
        result = await execute_plan(semantic, plan, _reconcile_llm("banana"))  # → skip
        assert result.executed == []
        assert (await semantic.get("loc:a")).value == "the user lives in Taipei"
        assert (await semantic.get("loc:b")).metadata.get("redirected_to") is None

    async def test_classify_failure_does_not_execute(self, semantic):
        plan = await _seed_reconcile(semantic)
        result = await execute_plan(semantic, plan, _failing_llm())
        assert result.executed == []
        assert (await semantic.get("loc:b")).metadata.get("redirected_to") is None
