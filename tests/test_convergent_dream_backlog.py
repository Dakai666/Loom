"""
Tests for cross-pass skip suppression — draining the deferred backlog (#553).

The convergent dream re-derives the same clusters from the whole corpus every
pass. With a per-pass cap and no memory of prior verdicts, the overflow is
labelled "deferred to next pass" but is in fact unreachable forever, and stable
``skip`` clusters are re-reviewed every week for nothing.

The fix is a *content-addressed* suppression set persisted in ``memory_meta``:
a cluster 絲絲 reviewed and skipped is filtered out of later passes — until any
member's content changes (the signature carries each member's ``updated_at``).
Two judgments are deliberately NOT suppressed: a ``defer`` (ask again later) and
a diff-inventory tooling auto-skip (should retry, #554).
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.cognition.consolidation import (
    KIND_MERGE,
    build_plan,
    run_convergent_dream,
    _cluster_signature,
    load_suppressed_signatures,
    record_suppressed_signatures,
    _META_KEY_SUPPRESSED,
)


# ---------------------------------------------------------------------------
# Fixtures (mirror test_convergent_dream_readonly)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def store(tmp_path):
    s = SQLiteStore(str(tmp_path / "test.db"))
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def db_conn(store):
    async with store.connect() as conn:
        yield conn


class _MarkerEmbeddings:
    _BASIS = {
        "GROUPA": [1.0, 0.0, 0.0, 0.0],
        "GROUPB": [0.0, 1.0, 0.0, 0.0],
        "GROUPC": [0.0, 0.0, 1.0, 0.0],
    }

    async def embed(self, texts):
        out = []
        for t in texts:
            vec = [0.0, 0.0, 0.0, 1.0]
            for marker, basis in self._BASIS.items():
                if marker in t:
                    vec = basis
                    break
            out.append(vec)
        return out


@pytest_asyncio.fixture
async def semantic_emb(db_conn):
    return SemanticMemory(db_conn, embedding_provider=_MarkerEmbeddings())


async def _snapshot(db_conn):
    cursor = await db_conn.execute(
        "SELECT key, value, confidence, source, metadata, created_at, updated_at, "
        "embedding FROM semantic_entries ORDER BY key"
    )
    return list(await cursor.fetchall())


# ---------------------------------------------------------------------------
# Signature — content-addressed, order-independent, updated_at-sensitive
# ---------------------------------------------------------------------------

class TestClusterSignature:
    def test_order_independent(self):
        from datetime import datetime, UTC
        t = datetime(2026, 6, 14, tzinfo=UTC)
        a = SemanticEntry(key="a", value="x", updated_at=t)
        b = SemanticEntry(key="b", value="y", updated_at=t)
        assert _cluster_signature([a, b]) == _cluster_signature([b, a])

    def test_changes_when_member_updated_at_changes(self):
        from datetime import datetime, UTC
        a = SemanticEntry(key="a", value="x", updated_at=datetime(2026, 6, 14, tzinfo=UTC))
        b1 = SemanticEntry(key="b", value="y", updated_at=datetime(2026, 6, 14, tzinfo=UTC))
        b2 = SemanticEntry(key="b", value="y", updated_at=datetime(2026, 6, 15, tzinfo=UTC))
        assert _cluster_signature([a, b1]) != _cluster_signature([a, b2])


# ---------------------------------------------------------------------------
# Persistence roundtrip + pruning
# ---------------------------------------------------------------------------

class TestSuppressionStore:
    async def test_record_then_load_roundtrips(self, db_conn):
        await record_suppressed_signatures(db_conn, {"sigA", "sigB"})
        assert await load_suppressed_signatures(db_conn) == {"sigA", "sigB"}

    async def test_record_accumulates_union(self, db_conn):
        await record_suppressed_signatures(db_conn, {"sigA"})
        await record_suppressed_signatures(db_conn, {"sigB"})
        assert await load_suppressed_signatures(db_conn) == {"sigA", "sigB"}

    async def test_empty_load_is_empty_set(self, db_conn):
        assert await load_suppressed_signatures(db_conn) == set()

    async def test_old_signatures_pruned_by_retention(self, db_conn):
        from datetime import datetime, timedelta, UTC
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        await record_suppressed_signatures(db_conn, {"old"}, now=t0, retention_days=30)
        later = t0 + timedelta(days=200)
        await record_suppressed_signatures(db_conn, {"fresh"}, now=later, retention_days=30)
        assert await load_suppressed_signatures(db_conn) == {"fresh"}

    async def test_corrupt_value_loads_as_empty(self, db_conn):
        await db_conn.execute(
            "INSERT INTO memory_meta(key, value, updated_at) VALUES (?, ?, ?)",
            (_META_KEY_SUPPRESSED, "not json", "2026-06-14T00:00:00"),
        )
        await db_conn.commit()
        assert await load_suppressed_signatures(db_conn) == set()


# ---------------------------------------------------------------------------
# build_plan honours the suppression set (pre-cap, frees quota)
# ---------------------------------------------------------------------------

class TestBuildPlanSuppression:
    async def _seed_three_pairs(self, semantic_emb):
        for marker, p in [("GROUPA", "a"), ("GROUPB", "b"), ("GROUPC", "c")]:
            await semantic_emb.upsert(SemanticEntry(key=f"{p}1", value=f"{marker} one", source="manual"))
            await semantic_emb.upsert(SemanticEntry(key=f"{p}2", value=f"{marker} two", source="manual"))

    async def test_suppressed_cluster_excluded(self, semantic_emb):
        await semantic_emb.upsert(SemanticEntry(key="m1", value="GROUPA tea", source="manual"))
        await semantic_emb.upsert(SemanticEntry(key="m2", value="GROUPA tea too", source="manual"))
        plan = await build_plan(semantic_emb)
        cluster = next(c for c in plan.clusters if c.kind == KIND_MERGE)
        sig = _cluster_signature(cluster.members)

        plan2 = await build_plan(semantic_emb, suppress_signatures={sig})
        assert [c for c in plan2.clusters if c.kind == KIND_MERGE] == []

    async def test_suppression_frees_cap_quota_so_backlog_drains(self, semantic_emb):
        # 3 merge clusters, cap 2 → 1 normally deferred & unreachable. Suppress
        # one stable-skip cluster and the other two BOTH fit under the cap.
        await self._seed_three_pairs(semantic_emb)
        baseline = await build_plan(semantic_emb, max_clusters=2)
        assert baseline.deferred_to_next_pass == 1

        # Signature of the GROUPC pair (read its members back).
        full = await build_plan(semantic_emb, max_clusters=20)
        c_cluster = next(
            c for c in full.clusters
            if c.kind == KIND_MERGE and set(c.member_keys) == {"c1", "c2"}
        )
        sig_c = _cluster_signature(c_cluster.members)

        plan = await build_plan(semantic_emb, max_clusters=2, suppress_signatures={sig_c})
        merge = [c for c in plan.clusters if c.kind == KIND_MERGE]
        keys = {frozenset(c.member_keys) for c in merge}
        assert frozenset({"c1", "c2"}) not in keys
        assert frozenset({"a1", "a2"}) in keys
        assert frozenset({"b1", "b2"}) in keys
        assert plan.deferred_to_next_pass == 0


# ---------------------------------------------------------------------------
# run_convergent_dream — records genuine skips, drains across passes
# ---------------------------------------------------------------------------

def _combined_llm(self_review_response: str, mergeable: bool = True):
    """diff-inventory → mergeable; self_review → the given verdict array."""
    async def fn(messages):
        sys = messages[0]["content"]
        if "差異盤點" in sys:
            return ('{"unique_by_key":{"m1":"","m2":"more"},'
                    f'"mergeable":{"true" if mergeable else "false"},"rationale":"r"}}')
        return self_review_response
    return fn


class TestRunDrainsBacklog:
    async def _seed_pair(self, semantic_emb):
        await semantic_emb.upsert(SemanticEntry(key="m1", value="GROUPA x", source="manual"))
        await semantic_emb.upsert(SemanticEntry(key="m2", value="GROUPA y", source="manual"))

    async def test_genuine_skip_recorded_and_suppressed_next_pass(self, semantic_emb, db_conn):
        await self._seed_pair(semantic_emb)
        skip = '[{"cluster_id":"%s","verdict":"skip","reason":"each carries unique content"}]'

        # Pass 1: cluster proposed, 絲絲 skips it.
        async def fn1(messages):
            if "差異盤點" in messages[0]["content"]:
                return '{"unique_by_key":{"m1":"","m2":"more"},"mergeable":true,"rationale":"r"}'
            # echo whatever cluster_id is in the prompt
            import re
            cid = re.search(r'cluster_id="([^"]+)"', messages[-1]["content"]).group(1)
            return skip % cid

        plan1, _ = await run_convergent_dream(semantic_emb, fn1)
        assert any(c.kind == KIND_MERGE for c in plan1.clusters)
        assert await load_suppressed_signatures(db_conn) != set()

        # Pass 2: same corpus, but the skipped cluster must not reappear.
        plan2, _ = await run_convergent_dream(semantic_emb, fn1)
        assert [c for c in plan2.clusters if c.kind == KIND_MERGE] == []

    async def test_diff_inventory_auto_skip_not_suppressed(self, semantic_emb, db_conn):
        # mergeable=false → self_review auto-skips WITHOUT consulting 絲絲. That
        # is a tooling verdict, not a judgment — it must retry, not be buried.
        await self._seed_pair(semantic_emb)
        fn = _combined_llm("[]", mergeable=False)
        plan1, _ = await run_convergent_dream(semantic_emb, fn)
        assert await load_suppressed_signatures(db_conn) == set()
        plan2, _ = await run_convergent_dream(semantic_emb, fn)
        assert any(c.kind == KIND_MERGE for c in plan2.clusters)

    async def test_defer_not_suppressed(self, semantic_emb, db_conn):
        await self._seed_pair(semantic_emb)

        async def fn(messages):
            if "差異盤點" in messages[0]["content"]:
                return '{"unique_by_key":{"m1":"","m2":"more"},"mergeable":true,"rationale":"r"}'
            import re
            cid = re.search(r'cluster_id="([^"]+)"', messages[-1]["content"]).group(1)
            return '[{"cluster_id":"%s","verdict":"defer","reason":"fermenting"}]' % cid

        await run_convergent_dream(semantic_emb, fn)
        assert await load_suppressed_signatures(db_conn) == set()

    async def test_recording_does_not_mutate_semantic_store(self, semantic_emb, db_conn):
        # Read-only invariant: suppression state lives in memory_meta, never in
        # semantic_entries.
        await self._seed_pair(semantic_emb)
        before = await _snapshot(db_conn)

        async def fn(messages):
            if "差異盤點" in messages[0]["content"]:
                return '{"unique_by_key":{"m1":"","m2":"more"},"mergeable":true,"rationale":"r"}'
            import re
            cid = re.search(r'cluster_id="([^"]+)"', messages[-1]["content"]).group(1)
            return '[{"cluster_id":"%s","verdict":"skip","reason":"unique"}]' % cid

        await run_convergent_dream(semantic_emb, fn)
        after = await _snapshot(db_conn)
        assert before == after
