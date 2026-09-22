"""
Tests for the four-way diff-inventory gate (#587 follow-up to #592).

The old gate was binary: "≥2 members carry unique content → not mergeable".
The 2026-09-20 real run showed most of those vetoes were facets of ONE claim
(e.g. five notes on load_skill caching, each adding a detail) — the union
should be kept, not the whole cluster blocked. The same run also showed
clusters whose members *disagree* (four inconsistent paths for one file);
union-merging those would weld a contradiction into one fact.

Contract:
  - gate returns ``relation`` ∈ duplicate | extends | distinct | conflict;
    anything else is a tool error (fail safe, retry).
  - ``mergeable`` is derived: status ok AND relation ∈ {duplicate, extends}.
  - distinct / conflict → skip; conflict is counted separately in the report
    because it needs arbitration, not fusion.
  - extends → synthesis must carry every non-survivor member's unique detail
    as a tagged bullet (date + source tier, assembled in code); a synthesis
    that drops one is rejected (never a lossy merge).
  - the suppression set is versioned: skips judged under the old binary gate
    must not bury clusters the new gate would merge.
"""

from __future__ import annotations

from datetime import datetime, UTC

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
    REL_CONFLICT,
    REL_DISTINCT,
    REL_DUPLICATE,
    REL_EXTENDS,
    VERDICT_SKIP,
    _META_KEY_SUPPRESSED,
    diff_inventory,
    load_suppressed_signatures,
    record_suppressed_signatures,
    render_report,
    self_review,
    synthesize_merge,
)


def _cluster(cid="c1", *keys, diff=None, sources=None):
    keys = keys or ("a", "b")
    sources = sources or {}
    return CandidateCluster(
        cluster_id=cid, kind=KIND_MERGE,
        members=[
            SemanticEntry(
                key=k, value=f"val-{k}", source=sources.get(k, "manual"),
                created_at=datetime(2026, 9, 1 + i, tzinfo=UTC),
            )
            for i, k in enumerate(keys)
        ],
        diff=diff,
    )


def _stub(resp: str):
    async def fn(messages):
        return resp
    return fn


def _gate(relation: str, ubk: str = '{"a": "", "b": "extra"}') -> str:
    return f'{{"unique_by_key": {ubk}, "relation": "{relation}", "rationale": "r"}}'


class _NoCallLLM:
    async def __call__(self, messages):  # pragma: no cover
        raise AssertionError("must not be called")


# ---------------------------------------------------------------------------
# gate parsing
# ---------------------------------------------------------------------------

class TestGateRelation:
    async def test_each_relation_parses(self):
        for rel in (REL_DUPLICATE, REL_EXTENDS, REL_DISTINCT, REL_CONFLICT):
            diff = await diff_inventory(_cluster(), _stub(_gate(rel)))
            assert diff.status == DIFF_OK
            assert diff.relation == rel

    async def test_mergeable_is_derived(self):
        expect = {REL_DUPLICATE: True, REL_EXTENDS: True,
                  REL_DISTINCT: False, REL_CONFLICT: False}
        for rel, merge in expect.items():
            diff = await diff_inventory(_cluster(), _stub(_gate(rel)))
            assert diff.mergeable is merge, rel

    async def test_unknown_relation_is_tool_error(self):
        diff = await diff_inventory(_cluster(), _stub(_gate("mergeable")))
        assert diff.status == DIFF_ERROR
        assert diff.mergeable is False

    async def test_missing_relation_is_tool_error(self):
        resp = '{"unique_by_key": {"a": "", "b": ""}, "mergeable": true, "rationale": "r"}'
        diff = await diff_inventory(_cluster(), _stub(resp))
        assert diff.status == DIFF_ERROR

    async def test_relation_case_and_space_tolerated(self):
        diff = await diff_inventory(_cluster(), _stub(_gate(" Extends ")))
        assert diff.relation == REL_EXTENDS

    def test_error_status_never_mergeable(self):
        assert DiffInventory(relation=REL_DUPLICATE, status=DIFF_ERROR).mergeable is False

    def test_default_is_not_mergeable(self):
        assert DiffInventory().mergeable is False


# ---------------------------------------------------------------------------
# self_review routing
# ---------------------------------------------------------------------------

class TestRouting:
    async def test_conflict_and_distinct_auto_skip(self):
        plan = ConsolidationPlan(clusters=[
            _cluster("m0", diff=DiffInventory(relation=REL_CONFLICT)),
            _cluster("m1", "c", "d", diff=DiffInventory(relation=REL_DISTINCT)),
        ])
        decisions = await self_review(plan, _NoCallLLM())
        assert [d.verdict for d in decisions] == [VERDICT_SKIP, VERDICT_SKIP]
        assert "conflict" in decisions[0].reason

    async def test_extends_goes_to_review(self):
        plan = ConsolidationPlan(clusters=[
            _cluster("m0", diff=DiffInventory(relation=REL_EXTENDS)),
        ])
        seen = {}

        async def llm(messages):
            seen["prompt"] = messages[-1]["content"]
            return '[{"cluster_id":"m0","verdict":"approve","reason":"ok"}]'

        decisions = await self_review(plan, llm)
        assert decisions[0].verdict == "approve"
        assert "relation=extends" in seen["prompt"]


# ---------------------------------------------------------------------------
# synthesis — union of details for extends
# ---------------------------------------------------------------------------

_UBK = {"a": "", "b": "adds the unload advice", "c": "adds the script path"}


class TestExtendsSynthesis:
    def _extends_cluster(self):
        return _cluster(
            "m0", "a", "b", "c",
            sources={"a": "memorize", "b": "session:x", "c": "session:y"},
            diff=DiffInventory(unique_by_key=dict(_UBK), relation=REL_EXTENDS),
        )

    async def test_details_appended_with_date_and_tier(self):
        resp = ('{"refined_value": "core claim", "rationale": "same claim", "details": ['
                '{"from_key": "b", "detail": "unload after use"},'
                '{"from_key": "c", "detail": "run the script directly"}]}')
        syn = await synthesize_merge(self._extends_cluster(), _stub(resp))
        assert syn is not None
        lines = syn.refined_value.splitlines()
        assert lines[0] == "core claim"
        assert "- unload after use（2026-09-02 · session_compress）" in lines
        assert "- run the script directly（2026-09-03 · session_compress）" in lines

    async def test_missing_detail_rejects_synthesis(self):
        # c's unique detail dropped → lossy merge → rejected.
        resp = ('{"refined_value": "core claim", "rationale": "r", "details": ['
                '{"from_key": "b", "detail": "unload after use"}]}')
        assert await synthesize_merge(self._extends_cluster(), _stub(resp)) is None

    async def test_phantom_from_key_rejects_synthesis(self):
        resp = ('{"refined_value": "core", "rationale": "r", "details": ['
                '{"from_key": "b", "detail": "x"}, {"from_key": "c", "detail": "y"},'
                '{"from_key": "zzz", "detail": "ghost"}]}')
        assert await synthesize_merge(self._extends_cluster(), _stub(resp)) is None

    async def test_empty_detail_text_does_not_count(self):
        resp = ('{"refined_value": "core", "rationale": "r", "details": ['
                '{"from_key": "b", "detail": "x"}, {"from_key": "c", "detail": "  "}]}')
        assert await synthesize_merge(self._extends_cluster(), _stub(resp)) is None

    async def test_survivor_unique_needs_no_bullet(self):
        # Survivor (memorize, highest trust) anchors the core — its own unique
        # content lives there, no bullet required for it.
        ubk = {"a": "survivor-only framing", "b": "", "c": ""}
        cluster = _cluster(
            "m0", "a", "b", "c",
            sources={"a": "memorize", "b": "session:x", "c": "session:y"},
            diff=DiffInventory(unique_by_key=ubk, relation=REL_EXTENDS),
        )
        syn = await synthesize_merge(cluster, _stub('{"refined_value": "core", "rationale": "r"}'))
        assert syn is not None and syn.refined_value == "core"

    async def test_survivor_bullet_is_dropped(self):
        # The survivor anchors the core; a bullet for it would repeat the core.
        resp = ('{"refined_value": "core", "rationale": "r", "details": ['
                '{"from_key": "a", "detail": "survivor restated"},'
                '{"from_key": "b", "detail": "x"}, {"from_key": "c", "detail": "y"}]}')
        syn = await synthesize_merge(self._extends_cluster(), _stub(resp))
        assert syn is not None
        assert "survivor restated" not in syn.refined_value
        assert len(syn.refined_value.splitlines()) == 3   # core + b + c

    async def test_extends_prompt_carries_inventory(self):
        seen = {}

        async def llm(messages):
            seen["user"] = messages[-1]["content"]
            return ('{"refined_value": "core", "rationale": "r", "details": ['
                    '{"from_key": "b", "detail": "x"}, {"from_key": "c", "detail": "y"}]}')

        await synthesize_merge(self._extends_cluster(), llm)
        assert "adds the unload advice" in seen["user"]
        assert "adds the script path" in seen["user"]

    async def test_duplicate_ignores_details_requirement(self):
        cluster = _cluster("m0", diff=DiffInventory(
            unique_by_key={"a": "", "b": "extra"}, relation=REL_DUPLICATE))
        syn = await synthesize_merge(cluster, _stub('{"refined_value": "fused", "rationale": "r"}'))
        assert syn is not None and syn.refined_value == "fused"


# ---------------------------------------------------------------------------
# report + suppression versioning
# ---------------------------------------------------------------------------

class TestReportAndSuppression:
    async def test_report_counts_conflicts(self):
        plan = ConsolidationPlan(clusters=[
            _cluster("m0", diff=DiffInventory(relation=REL_CONFLICT)),
        ])
        plan.decisions = await self_review(plan, _NoCallLLM())
        report = render_report(plan)
        assert "1 個簇成員互相矛盾" in report

    def test_suppression_key_is_versioned(self):
        # Skips recorded under the binary gate live under the old key and must
        # not be loaded by the four-way gate.
        assert _META_KEY_SUPPRESSED != "consolidation_dream.suppressed_skips"


@pytest_asyncio.fixture
async def db_conn(tmp_path):
    s = SQLiteStore(str(tmp_path / "t.db"))
    await s.initialize()
    async with s.connect() as conn:
        yield conn


async def test_old_binary_gate_skips_are_not_loaded(db_conn):
    await db_conn.execute(
        "INSERT INTO memory_meta(key, value, updated_at) VALUES (?, ?, ?)",
        ("consolidation_dream.suppressed_skips",
         '{"oldsig": "2026-09-20T00:00:00+00:00"}', "2026-09-20T00:00:00"),
    )
    await db_conn.commit()
    assert await load_suppressed_signatures(db_conn) == set()


async def test_record_drops_legacy_binary_gate_key(db_conn):
    # The pre-v2 key would otherwise sit orphaned in memory_meta forever.
    await db_conn.execute(
        "INSERT INTO memory_meta(key, value, updated_at) VALUES (?, ?, ?)",
        ("consolidation_dream.suppressed_skips", '{"oldsig": "x"}', "2026-09-20T00:00:00"),
    )
    await db_conn.commit()
    await record_suppressed_signatures(db_conn, {"newsig"})
    cursor = await db_conn.execute(
        "SELECT key FROM memory_meta WHERE key LIKE 'consolidation_dream.suppressed_skips%'")
    assert [r[0] for r in await cursor.fetchall()] == [_META_KEY_SUPPRESSED]
