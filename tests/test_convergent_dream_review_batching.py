"""
Tests for self_review batching + per-pass cap (#499, slice 2c).

Real-run finding (2026-06-01): self_review rendered ALL pending clusters into a
single LLM prompt. At scale (the reconcile-noise blowup, #497) that prompt
exceeded the context limit → the provider returned 2013 → every cluster fell
back to ``defer``. The fail-safe was correct (defer, never auto-approve) but
self_review was effectively dead at scale.

The fix: review in batches of N clusters per prompt (one context-bounded call
each), with a per-pass total cap whose overflow is deferred to the next pass
via the existing ``deferred_to_next_pass`` accounting — never silently dropped.
A batch that fails defers only its own clusters, never the whole pass.
"""

from __future__ import annotations

import json
import re

from loom.core.memory.semantic import SemanticEntry
from loom.core.cognition.consolidation import (
    CandidateCluster,
    ConsolidationPlan,
    DiffInventory,
    KIND_MERGE,
    KIND_RECONCILE,
    VERDICT_APPROVE,
    VERDICT_DEFER,
    VERDICT_SKIP,
    self_review,
)


def _cluster(i: int, kind: str = KIND_RECONCILE) -> CandidateCluster:
    return CandidateCluster(
        cluster_id=f"c{i}",
        kind=kind,
        members=[
            SemanticEntry(key=f"k{i}a", value="x", source="manual"),
            SemanticEntry(key=f"k{i}b", value="y", source="manual"),
        ],
    )


def _plan(n: int) -> ConsolidationPlan:
    return ConsolidationPlan(clusters=[_cluster(i) for i in range(n)])


class _RecordingLLM:
    """Approves every cluster_id present in the prompt; records each call's ids."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def __call__(self, messages):
        ids = re.findall(r'cluster_id="([^"]+)"', messages[1]["content"])
        self.calls.append(ids)
        return json.dumps([{"cluster_id": cid, "verdict": "approve"} for cid in ids])


class TestBatching:
    async def test_splits_into_batches_of_batch_size(self):
        plan = _plan(25)
        llm = _RecordingLLM()
        decisions = await self_review(plan, llm, batch_size=10, max_review_clusters=100)
        # 25 clusters / 10 per batch → three calls of sizes 10, 10, 5
        assert [len(c) for c in llm.calls] == [10, 10, 5]
        # every cluster reviewed exactly once
        assert len(decisions) == 25
        assert all(d.verdict == VERDICT_APPROVE for d in decisions)

    async def test_every_cluster_gets_exactly_one_decision(self):
        plan = _plan(17)
        decisions = await self_review(plan, _RecordingLLM(), batch_size=5, max_review_clusters=100)
        ids = [d.cluster_id for d in decisions]
        assert sorted(ids) == sorted(c.cluster_id for c in plan.clusters)
        assert len(ids) == len(set(ids))  # no duplicates


class TestBatchFailureIsolation:
    async def test_failing_batch_defers_only_its_own_clusters(self):
        # The second batch raises; batches 1 and 3 must still be approved.
        class _Flaky:
            def __init__(self):
                self.n = 0

            async def __call__(self, messages):
                ids = re.findall(r'cluster_id="([^"]+)"', messages[1]["content"])
                self.n += 1
                if self.n == 2:
                    raise RuntimeError("context window exceeded (2013)")
                return json.dumps([{"cluster_id": cid, "verdict": "approve"} for cid in ids])

        plan = _plan(15)  # batch_size 5 → 3 batches
        decisions = await self_review(plan, _Flaky(), batch_size=5, max_review_clusters=100)
        by_id = {d.cluster_id: d for d in decisions}
        # batch 1 (c0-c4) + batch 3 (c10-c14) approved; batch 2 (c5-c9) deferred
        assert all(by_id[f"c{i}"].verdict == VERDICT_APPROVE for i in range(0, 5))
        assert all(by_id[f"c{i}"].verdict == VERDICT_DEFER for i in range(5, 10))
        assert all(by_id[f"c{i}"].verdict == VERDICT_APPROVE for i in range(10, 15))
        assert len(decisions) == 15


class TestPerPassCap:
    async def test_overflow_deferred_to_next_pass(self):
        plan = _plan(30)
        llm = _RecordingLLM()
        decisions = await self_review(plan, llm, batch_size=10, max_review_clusters=20)
        approved = [d for d in decisions if d.verdict == VERDICT_APPROVE]
        deferred = [d for d in decisions if d.verdict == VERDICT_DEFER]
        assert len(approved) == 20
        assert len(deferred) == 10
        # accounting borrows the existing no-silent-truncation channel
        assert plan.deferred_to_next_pass == 10
        assert any("cap" in n for n in plan.notes)
        # only the first 20 clusters were actually sent to the LLM
        assert sum(len(c) for c in llm.calls) == 20

    async def test_under_cap_reviews_all(self):
        plan = _plan(8)
        decisions = await self_review(plan, _RecordingLLM(), batch_size=10, max_review_clusters=40)
        assert all(d.verdict == VERDICT_APPROVE for d in decisions)
        assert plan.deferred_to_next_pass == 0


class TestAutoSkipNotCounted:
    async def test_non_mergeable_autoskip_does_not_consume_cap(self):
        # A merge cluster vetoed by the diff-inventory gate is auto-skipped
        # without an LLM call — it must not eat into the review cap budget.
        plan = ConsolidationPlan(clusters=[
            CandidateCluster(
                cluster_id="m0", kind=KIND_MERGE,
                members=[SemanticEntry(key="a", value="x", source="manual"),
                         SemanticEntry(key="b", value="y", source="manual")],
                diff=DiffInventory(mergeable=False, rationale="distinct insights"),
            ),
            *[_cluster(i) for i in range(20)],
        ])
        llm = _RecordingLLM()
        decisions = await self_review(plan, llm, batch_size=10, max_review_clusters=20)
        skip = [d for d in decisions if d.verdict == VERDICT_SKIP]
        approved = [d for d in decisions if d.verdict == VERDICT_APPROVE]
        assert len(skip) == 1  # the auto-skipped merge cluster
        # all 20 reconcile clusters still fit under the cap (autoskip not counted)
        assert len(approved) == 20
        assert plan.deferred_to_next_pass == 0
