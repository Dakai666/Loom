"""
Tests for the convergent-dream execute layer (#489, P2).

synthesize_merge: deterministic trust-aware survivor selection + verbatim
sacrificed_content (built in code, NOT trusted to the LLM), LLM only fuses the
refined value + rationale.

execute_plan: consumes a ConsolidationPlan, executes ONLY approved merge
clusters, skips stale clusters (source changed after the plan snapshot), and
leaves reconcile/clean to P3 (#490).
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
import json
import re

from loom.core.cognition.consolidation import (
    CandidateCluster,
    ConsolidationPlan,
    ReviewDecision,
    KIND_MERGE,
    KIND_RECONCILE,
    VERDICT_APPROVE,
    VERDICT_SKIP,
    VERDICT_DEFER,
    synthesize_merge,
    execute_plan,
    run_convergent_dream,
)


class _MarkerEmbeddings:
    async def embed(self, texts):
        return [[1.0, 0.0, 0.0] if "GROUPA" in t else [0.0, 1.0, 0.0] for t in texts]


async def _orchestrated_llm(messages):
    """Combined stub: diff → mergeable, self_review → approve all, synth → fused."""
    sys = messages[0]["content"]
    user = messages[-1]["content"]
    if "差異盤點" in sys:
        keys = re.findall(r'key="([^"]+)"', user)
        ubk = ", ".join(f'"{k}": ""' for k in keys)
        return '{"unique_by_key": {' + ubk + '}, "mergeable": true, "rationale": "same"}'
    if "reviewing how YOUR OWN" in sys:
        ids = re.findall(r'cluster_id="([^"]+)"', user)
        return json.dumps([{"cluster_id": i, "verdict": "approve"} for i in ids])
    if "synthesizer" in sys:
        return '{"refined_value": "fused tea fact", "rationale": "same"}'
    return "[]"


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


def _stub_llm(response: str):
    async def fn(messages):
        return response
    return fn


def _failing_llm():
    async def fn(messages):
        raise RuntimeError("llm down")
    return fn


_MERGE_RESP = '{"refined_value": "the merged refined fact", "rationale": "fused them"}'


# ---------------------------------------------------------------------------
# synthesize_merge — trust-aware survivor, verbatim sacrificed content
# ---------------------------------------------------------------------------

class TestSynthesizeMerge:
    async def test_highest_trust_is_survivor(self):
        # manual=user_explicit(1.0) outranks session_compress(0.8)
        members = [
            SemanticEntry(key="lo", value="low trust phrasing", source="session:x:fact:0"),
            SemanticEntry(key="hi", value="user's own words", source="manual"),
        ]
        cluster = CandidateCluster(cluster_id="c1", kind=KIND_MERGE, members=members)
        syn = await synthesize_merge(cluster, _stub_llm(_MERGE_RESP))
        assert syn.survivor_key == "hi"

    async def test_trust_tie_earliest_created_at_wins(self):
        from datetime import datetime, UTC
        old = SemanticEntry(key="old", value="established", source="manual",
                            created_at=datetime(2026, 1, 1, tzinfo=UTC))
        new = SemanticEntry(key="new", value="recent", source="manual",
                            created_at=datetime(2026, 5, 1, tzinfo=UTC))
        cluster = CandidateCluster(cluster_id="c1", kind=KIND_MERGE, members=[new, old])
        syn = await synthesize_merge(cluster, _stub_llm(_MERGE_RESP))
        assert syn.survivor_key == "old"

    async def test_sacrificed_content_is_verbatim(self):
        members = [
            SemanticEntry(key="hi", value="anchor wording", source="manual"),
            SemanticEntry(key="lo", value="UNIQUE SACRIFICED PHRASING xyz", source="session:x:fact:0"),
        ]
        cluster = CandidateCluster(cluster_id="c1", kind=KIND_MERGE, members=members)
        syn = await synthesize_merge(cluster, _stub_llm(_MERGE_RESP))
        # the sacrificed entry's full original text appears verbatim
        assert "UNIQUE SACRIFICED PHRASING xyz" in syn.sacrificed_content

    async def test_refined_value_from_llm(self):
        members = [SemanticEntry(key="a", value="x", source="manual"),
                   SemanticEntry(key="b", value="y", source="manual")]
        cluster = CandidateCluster(cluster_id="c1", kind=KIND_MERGE, members=members)
        syn = await synthesize_merge(cluster, _stub_llm(_MERGE_RESP))
        assert syn.refined_value == "the merged refined fact"

    async def test_llm_failure_returns_none(self):
        members = [SemanticEntry(key="a", value="x", source="manual"),
                   SemanticEntry(key="b", value="y", source="manual")]
        cluster = CandidateCluster(cluster_id="c1", kind=KIND_MERGE, members=members)
        assert await synthesize_merge(cluster, _failing_llm()) is None

    async def test_empty_refined_value_returns_none(self):
        members = [SemanticEntry(key="a", value="x", source="manual"),
                   SemanticEntry(key="b", value="y", source="manual")]
        cluster = CandidateCluster(cluster_id="c1", kind=KIND_MERGE, members=members)
        syn = await synthesize_merge(cluster, _stub_llm('{"refined_value": "", "rationale": "r"}'))
        assert syn is None


# ---------------------------------------------------------------------------
# execute_plan — approve-only, stale-aware, merge-only (P2)
# ---------------------------------------------------------------------------

async def _seed_merge_cluster(semantic, *, cid="m1"):
    a = SemanticEntry(key="k:a", value="the user prefers tea in the morning", source="manual")
    b = SemanticEntry(key="k:b", value="user likes morning tea", source="session:x:fact:0")
    await semantic.upsert(a)
    await semantic.upsert(b)
    a, b = await semantic.get("k:a"), await semantic.get("k:b")
    cluster = CandidateCluster(cluster_id=cid, kind=KIND_MERGE, members=[a, b])
    plan = ConsolidationPlan(clusters=[cluster])
    plan.source_versions = {
        "k:a": a.updated_at.isoformat(),
        "k:b": b.updated_at.isoformat(),
    }
    return plan, cluster


class TestExecutePlan:
    async def test_approved_merge_executes(self, semantic):
        plan, cluster = await _seed_merge_cluster(semantic)
        plan.decisions = [ReviewDecision(cluster_id=cluster.cluster_id, verdict=VERDICT_APPROVE)]
        result = await execute_plan(semantic, plan, _stub_llm(_MERGE_RESP))

        assert len(result.executed) == 1
        survivor = await semantic.get("k:a")          # manual = higher trust survivor
        assert survivor.value == "the merged refined fact"
        stub = await semantic.get("k:b")
        assert stub.metadata.get("redirected_to") == "k:a"

    async def test_skip_verdict_does_not_execute(self, semantic):
        plan, cluster = await _seed_merge_cluster(semantic)
        plan.decisions = [ReviewDecision(cluster_id=cluster.cluster_id, verdict=VERDICT_SKIP)]
        result = await execute_plan(semantic, plan, _stub_llm(_MERGE_RESP))

        assert result.executed == []
        assert (await semantic.get("k:a")).value == "the user prefers tea in the morning"

    async def test_defer_verdict_does_not_execute(self, semantic):
        plan, cluster = await _seed_merge_cluster(semantic)
        plan.decisions = [ReviewDecision(cluster_id=cluster.cluster_id, verdict=VERDICT_DEFER)]
        result = await execute_plan(semantic, plan, _stub_llm(_MERGE_RESP))
        assert result.executed == []

    async def test_stale_source_skips_cluster(self, semantic):
        plan, cluster = await _seed_merge_cluster(semantic)
        plan.decisions = [ReviewDecision(cluster_id=cluster.cluster_id, verdict=VERDICT_APPROVE)]
        # Mutate a member AFTER the plan snapshot → updated_at changes → stale.
        await semantic.upsert(SemanticEntry(key="k:b", value="changed after planning", source="manual"))

        result = await execute_plan(semantic, plan, _stub_llm(_MERGE_RESP))
        assert result.executed == []
        assert len(result.skipped_stale) == 1
        # untouched: survivor not consolidated
        assert (await semantic.get("k:a")).value == "the user prefers tea in the morning"

    async def test_reconcile_cluster_deferred_to_p3(self, semantic):
        await semantic.upsert(SemanticEntry(key="r:a", value="x", source="manual"))
        await semantic.upsert(SemanticEntry(key="r:b", value="y", source="manual"))
        a, b = await semantic.get("r:a"), await semantic.get("r:b")
        cluster = CandidateCluster(cluster_id="rc1", kind=KIND_RECONCILE, members=[a, b])
        plan = ConsolidationPlan(clusters=[cluster])
        plan.decisions = [ReviewDecision(cluster_id="rc1", verdict=VERDICT_APPROVE)]

        result = await execute_plan(semantic, plan, _stub_llm(_MERGE_RESP))
        assert result.executed == []
        assert len(result.skipped_other) == 1

    async def test_synthesis_failure_skips_without_writing(self, semantic):
        plan, cluster = await _seed_merge_cluster(semantic)
        plan.decisions = [ReviewDecision(cluster_id=cluster.cluster_id, verdict=VERDICT_APPROVE)]
        result = await execute_plan(semantic, plan, _failing_llm())
        assert result.executed == []
        assert (await semantic.get("k:a")).value == "the user prefers tea in the morning"


# ---------------------------------------------------------------------------
# run_convergent_dream(execute=...) — gated wiring (default read-only)
# ---------------------------------------------------------------------------

class TestRunConvergentDreamExecute:
    @pytest_asyncio.fixture
    async def semantic_emb(self, db_conn):
        return SemanticMemory(db_conn, embedding_provider=_MarkerEmbeddings())

    async def test_execute_false_is_read_only(self, semantic_emb, db_conn):
        await semantic_emb.upsert(SemanticEntry(key="k:a", value="GROUPA user likes tea", source="manual"))
        await semantic_emb.upsert(SemanticEntry(key="k:b", value="GROUPA tea preferred", source="session:x:fact:0"))
        before = list(await (await db_conn.execute(
            "SELECT key, value FROM semantic_entries ORDER BY key")).fetchall())
        plan, report = await run_convergent_dream(semantic_emb, _orchestrated_llm)  # execute defaults False
        after = list(await (await db_conn.execute(
            "SELECT key, value FROM semantic_entries ORDER BY key")).fetchall())
        assert before == after
        assert plan.execution_status == "planned"

    async def test_execute_true_consolidates(self, semantic_emb):
        await semantic_emb.upsert(SemanticEntry(key="k:a", value="GROUPA user likes tea", source="manual"))
        await semantic_emb.upsert(SemanticEntry(key="k:b", value="GROUPA tea preferred", source="session:x:fact:0"))

        plan, report = await run_convergent_dream(semantic_emb, _orchestrated_llm, execute=True)

        assert plan.execution_status == "executed"
        survivor = await semantic_emb.get("k:a")          # manual = higher trust
        assert survivor.value == "fused tea fact"
        stub = await semantic_emb.get("k:b")
        assert stub.metadata.get("redirected_to") == "k:a"
        assert "執行結果" in report  # execution section present when execute=True
