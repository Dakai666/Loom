"""
Tests for separating diff-inventory *tool failure* from *judgment* (#587).

Before this fix both collapsed into ``mergeable=False``:

  - a tool failure (LLM exception / unparseable body / bad shape, retries
    exhausted) was rendered as a "not mergeable" skip — a judgment nobody made;
  - a clean, covered "each member carries unique content" verdict was treated
    as a tool failure by ``_genuine_skip_signatures`` and never suppressed, so
    those clusters were re-reviewed every pass and permanently ate the
    ``max_clusters`` quota, starving the deferred backlog.

Contract:
  - ``DiffInventory.status`` is ``DIFF_OK`` for a trustworthy verdict and
    ``DIFF_ERROR`` when the gate could not produce one.
  - Tool error → ``defer`` (retry next pass), never ``skip``; never suppressed;
    not counted as cap overflow (``deferred_to_next_pass`` stays cap-only).
  - Clean not-mergeable → ``skip`` and suppressed like any other stable skip.
  - The report counts tool errors separately.
"""

from __future__ import annotations

import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.cognition.consolidation import (
    CandidateCluster,
    ConsolidationPlan,
    DIFF_ERROR,
    DIFF_OK,
    DiffInventory,
    KIND_MERGE,
    VERDICT_DEFER,
    VERDICT_SKIP,
    diff_inventory,
    load_suppressed_signatures,
    render_report,
    run_convergent_dream,
    self_review,
)


def _cluster(cid: str = "c1", *keys: str, diff: DiffInventory | None = None) -> CandidateCluster:
    keys = keys or ("a", "b")
    return CandidateCluster(
        cluster_id=cid, kind=KIND_MERGE,
        members=[SemanticEntry(key=k, value=f"val-{k}", source="manual") for k in keys],
        diff=diff,
    )


class _Seq:
    def __init__(self, *responses: str):
        self._r = list(responses)
        self.calls = 0

    async def __call__(self, messages):
        self.calls += 1
        return self._r[min(self.calls - 1, len(self._r) - 1)]


_CLEAN_FALSE = ('{"unique_by_key": {"a": "preference", "b": "complaint"}, '
                '"relation": "distinct", "rationale": "distinct insights"}')
_CLEAN_TRUE = ('{"unique_by_key": {"a": "", "b": "extra"}, '
               '"relation": "duplicate", "rationale": "b subsumes a"}')


# ---------------------------------------------------------------------------
# diff_inventory — status
# ---------------------------------------------------------------------------

class TestDiffInventoryStatus:
    async def test_clean_false_is_ok(self):
        diff = await diff_inventory(_cluster(), _Seq(_CLEAN_FALSE))
        assert diff.status == DIFF_OK
        assert diff.mergeable is False

    async def test_clean_true_is_ok(self):
        diff = await diff_inventory(_cluster(), _Seq(_CLEAN_TRUE))
        assert diff.status == DIFF_OK

    async def test_recovered_after_retry_is_ok(self):
        diff = await diff_inventory(_cluster(), _Seq("garbage", _CLEAN_FALSE))
        assert diff.status == DIFF_OK

    async def test_exhausted_unparseable_is_error(self):
        diff = await diff_inventory(_cluster(), _Seq("garbage", "garbage"))
        assert diff.status == DIFF_ERROR
        assert diff.mergeable is False  # still fail-safe

    async def test_exhausted_exception_is_error(self):
        async def boom(messages):
            raise RuntimeError("down")
        diff = await diff_inventory(_cluster(), boom)
        assert diff.status == DIFF_ERROR

    async def test_exhausted_coverage_miss_is_error(self):
        bad = '{"unique_by_key": {"a": ""}, "relation": "duplicate", "rationale": "r"}'
        diff = await diff_inventory(_cluster(), _Seq(bad, bad))
        assert diff.status == DIFF_ERROR
        assert diff.mergeable is False

    def test_default_status_is_ok(self):
        # Constructing a verdict directly means a real answer.
        assert DiffInventory(relation="distinct").status == DIFF_OK


# ---------------------------------------------------------------------------
# self_review — tool error defers, judgment skips
# ---------------------------------------------------------------------------

class _NoCallLLM:
    async def __call__(self, messages):  # pragma: no cover - must not be called
        raise AssertionError("gate-decided clusters must not reach the LLM")


class TestSelfReviewRouting:
    async def test_tool_error_defers_not_skips(self):
        plan = ConsolidationPlan(clusters=[_cluster(
            "m0", diff=DiffInventory(status=DIFF_ERROR, rationale="unparseable"),
        )])
        decisions = await self_review(plan, _NoCallLLM())
        assert [d.verdict for d in decisions] == [VERDICT_DEFER]
        assert "not mergeable" not in decisions[0].reason

    async def test_tool_error_is_not_cap_overflow(self):
        plan = ConsolidationPlan(clusters=[_cluster(
            "m0", diff=DiffInventory(status=DIFF_ERROR),
        )])
        await self_review(plan, _NoCallLLM())
        assert plan.deferred_to_next_pass == 0

    async def test_clean_not_mergeable_skips(self):
        plan = ConsolidationPlan(clusters=[_cluster(
            "m0", diff=DiffInventory(relation="distinct", rationale="distinct insights"),
        )])
        decisions = await self_review(plan, _NoCallLLM())
        assert [d.verdict for d in decisions] == [VERDICT_SKIP]


# ---------------------------------------------------------------------------
# run_convergent_dream — suppression follows judgment, not tool failure
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
    async def embed(self, texts):
        return [[1.0, 0.0] if "GROUPA" in t else [0.0, 1.0] for t in texts]


@pytest_asyncio.fixture
async def semantic_emb(db_conn):
    sem = SemanticMemory(db_conn, embedding_provider=_MarkerEmbeddings())
    await sem.upsert(SemanticEntry(key="m1", value="GROUPA x", source="manual"))
    await sem.upsert(SemanticEntry(key="m2", value="GROUPA y", source="manual"))
    return sem


def _gate_llm(diff_response: str):
    async def fn(messages):
        if "差異盤點" in messages[0]["content"]:
            return diff_response
        return "[]"
    return fn


_PAIR_FALSE = ('{"unique_by_key":{"m1":"a","m2":"b"},'
               '"relation":"distinct","rationale":"distinct"}')


class TestRunSuppression:
    async def test_clean_not_mergeable_is_suppressed_next_pass(self, semantic_emb, db_conn):
        fn = _gate_llm(_PAIR_FALSE)
        plan1, _ = await run_convergent_dream(semantic_emb, fn)
        assert any(c.kind == KIND_MERGE for c in plan1.clusters)
        assert await load_suppressed_signatures(db_conn) != set()
        plan2, _ = await run_convergent_dream(semantic_emb, fn)
        assert [c for c in plan2.clusters if c.kind == KIND_MERGE] == []

    async def test_tool_error_not_suppressed_and_retried(self, semantic_emb, db_conn):
        fn = _gate_llm("garbage")
        plan1, _ = await run_convergent_dream(semantic_emb, fn)
        assert [d.verdict for d in plan1.decisions] == [VERDICT_DEFER]
        assert await load_suppressed_signatures(db_conn) == set()
        plan2, _ = await run_convergent_dream(semantic_emb, fn)
        assert any(c.kind == KIND_MERGE for c in plan2.clusters)


# ---------------------------------------------------------------------------
# render_report — tool errors counted separately from cap deferral
# ---------------------------------------------------------------------------

class TestReport:
    async def test_report_counts_tool_errors(self):
        plan = ConsolidationPlan(clusters=[
            _cluster("m0", diff=DiffInventory(status=DIFF_ERROR)),
            _cluster("m1", "c", "d", diff=DiffInventory(status=DIFF_ERROR)),
        ])
        plan.decisions = await self_review(plan, _NoCallLLM())
        report = render_report(plan)
        assert "2 個簇差異盤點工具故障" in report
        assert "順延下輪" not in report  # not conflated with cap deferral
