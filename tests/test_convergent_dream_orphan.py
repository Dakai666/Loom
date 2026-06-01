"""
Tests for convergent-dream orphan cleanup (#490, P3, slice 4b).

Clean phase: a redirect stub whose terminal survivor is gone (resolve_redirect
→ None) is a dangling orphan → KIND_CLEAN candidate → delete on approval.
Live stubs (target still resolves) are NOT orphans. dreaming-sourced entries
are allowed orphans (never cleaned). Stubs are excluded from merge/reconcile
scanning so they don't get re-clustered.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.cognition.consolidation import (
    ConsolidationPlan,
    ReviewDecision,
    KIND_CLEAN,
    KIND_MERGE,
    VERDICT_APPROVE,
    VERDICT_SKIP,
    build_plan,
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


def _stub_llm(resp):
    async def fn(messages):
        return resp
    return fn


async def _make_dangling_stub(semantic, *, src="manual", stub_key="a", surv_key="b"):
    await semantic.upsert(SemanticEntry(key=stub_key, value="orphan source", source=src))
    await semantic.upsert(SemanticEntry(key=surv_key, value="survivor", source="manual"))
    a, b = await semantic.get(stub_key), await semantic.get(surv_key)
    await semantic.consolidate([a, b], refined_value="merged", survivor_key=surv_key,
                               sacrificed_content=a.value, merge_rationale="r")  # stub_key→surv_key
    await semantic.delete(surv_key)   # survivor gone → stub dangles


class TestOrphanDetection:
    async def test_dangling_stub_is_clean_candidate(self, semantic):
        await _make_dangling_stub(semantic)
        plan = await build_plan(semantic)
        clean = [c for c in plan.clusters if c.kind == KIND_CLEAN]
        assert len(clean) == 1
        assert clean[0].member_keys == ["a"]

    async def test_live_stub_is_not_orphan(self, semantic):
        # a→b with b still present → a resolves → not dangling.
        await semantic.upsert(SemanticEntry(key="a", value="x", source="manual"))
        await semantic.upsert(SemanticEntry(key="b", value="y", source="manual"))
        a, b = await semantic.get("a"), await semantic.get("b")
        await semantic.consolidate([a, b], refined_value="m", survivor_key="b",
                                   sacrificed_content=a.value, merge_rationale="r")
        plan = await build_plan(semantic)
        assert [c for c in plan.clusters if c.kind == KIND_CLEAN] == []

    async def test_dreaming_dangling_stub_exempt(self, semantic):
        await _make_dangling_stub(semantic, src="dreaming")
        plan = await build_plan(semantic)
        assert [c for c in plan.clusters if c.kind == KIND_CLEAN] == []

    async def test_stub_excluded_from_merge_scan(self, db_conn):
        # A live stub must not be re-clustered for merge.
        class _Emb:
            async def embed(self, texts):
                return [[1.0, 0.0, 0.0] for _ in texts]
        sem = SemanticMemory(db_conn, embedding_provider=_Emb())
        await sem.upsert(SemanticEntry(key="a", value="same", source="manual"))
        await sem.upsert(SemanticEntry(key="b", value="same", source="manual"))
        await sem.upsert(SemanticEntry(key="c", value="same", source="manual"))
        a, b = await sem.get("a"), await sem.get("b")
        await sem.consolidate([a, b], refined_value="m", survivor_key="b",
                              sacrificed_content=a.value, merge_rationale="r")  # a→b stub
        plan = await build_plan(sem, min_similarity=0.85)
        merge_members = {k for c in plan.clusters if c.kind == KIND_MERGE for k in c.member_keys}
        assert "a" not in merge_members   # stub excluded


class TestOrphanExecution:
    async def _clean_plan(self, semantic):
        await _make_dangling_stub(semantic)
        plan = await build_plan(semantic)
        clean = [c for c in plan.clusters if c.kind == KIND_CLEAN][0]
        plan.decisions = [ReviewDecision(cluster_id=clean.cluster_id, verdict=VERDICT_APPROVE)]
        return plan, clean

    async def test_approved_clean_deletes_orphan(self, semantic):
        plan, clean = await self._clean_plan(semantic)
        result = await execute_plan(semantic, plan, _stub_llm("[]"))
        assert "a" in result.cleaned
        assert await semantic.get("a") is None     # orphan deleted

    async def test_skip_clean_keeps_orphan(self, semantic):
        plan, clean = await self._clean_plan(semantic)
        plan.decisions = [ReviewDecision(cluster_id=clean.cluster_id, verdict=VERDICT_SKIP)]
        result = await execute_plan(semantic, plan, _stub_llm("[]"))
        assert result.cleaned == []
        assert await semantic.get("a") is not None  # untouched

    async def test_clean_revalidates_orphan_at_execute(self, semantic):
        # Codex review #496 — TOCTTOU: survivor restored between plan and
        # execute. The stub row itself is unchanged (stale check passes), but
        # it now resolves again → must NOT be deleted (live fallback).
        plan, clean = await self._clean_plan(semantic)   # a→b dangling, approved
        await semantic.upsert(SemanticEntry(key="b", value="restored survivor", source="manual"))
        result = await execute_plan(semantic, plan, _stub_llm("[]"))
        assert "a" not in result.cleaned
        assert await semantic.get("a") is not None        # live old-key fallback preserved
        assert (await semantic.resolve_redirect("a")).key == "b"
