"""
Tests for the convergent dream — read-only consolidation planner (#488, P1).

The headline invariant is **read-only**: a convergent-dream pass plans, gates,
and self-reviews, but must never mutate the semantic store. Everything else
(dreaming exemption, diff-inventory auto-skip, fail-safe defer, no-silent-cap)
guards the spec-#56 design contract.

Embedding strategy: the reconcile path is embedding-free (key/prefix
contradiction), so most planning is tested without vectors. The merge path
uses a deterministic marker-based fake provider — texts sharing a GROUP marker
embed to the same basis vector (cosine 1.0), distinct markers stay orthogonal —
so clustering is order-independent and does not depend on sqlite-vec call
sequencing.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.cognition import consolidation as cd
from loom.core.cognition.consolidation import (
    CandidateCluster,
    ConsolidationPlan,
    DiffInventory,
    ReviewDecision,
    KIND_MERGE,
    KIND_RECONCILE,
    VERDICT_APPROVE,
    VERDICT_SKIP,
    VERDICT_DEFER,
    build_plan,
    diff_inventory,
    self_review,
    render_report,
    run_convergent_dream,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
    """Embedding-free semantic memory (reconcile path)."""
    return SemanticMemory(db_conn)


class _MarkerEmbeddings:
    """Deterministic fake: text → basis vector by GROUP marker substring.

    Same marker → same vector (cosine 1.0); different markers → orthogonal.
    Order-independent, so clustering tests don't depend on embed call order.
    """

    _BASIS = {
        "GROUPA": [1.0, 0.0, 0.0, 0.0],
        "GROUPB": [0.0, 1.0, 0.0, 0.0],
        "GROUPC": [0.0, 0.0, 1.0, 0.0],
    }

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            vec = [0.0, 0.0, 0.0, 1.0]  # default: unique "no-group" axis
            for marker, basis in self._BASIS.items():
                if marker in t:
                    vec = basis
                    break
            out.append(vec)
        return out


@pytest_asyncio.fixture
async def semantic_emb(db_conn):
    """Semantic memory with the deterministic marker embedding provider."""
    return SemanticMemory(db_conn, embedding_provider=_MarkerEmbeddings())


# ---------------------------------------------------------------------------
# LLM stubs
# ---------------------------------------------------------------------------

def _stub_llm(response: str):
    async def fn(messages):
        return response
    return fn


def _failing_llm(exc: Exception):
    async def fn(messages):
        raise exc
    return fn


async def _snapshot(db_conn) -> list[tuple]:
    """Full read of the semantic table for the read-only invariant."""
    cursor = await db_conn.execute(
        "SELECT key, value, confidence, source, metadata, created_at, updated_at, "
        "embedding FROM semantic_entries ORDER BY key"
    )
    return list(await cursor.fetchall())


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestPureHelpers:
    def test_union_find_groups_connected(self):
        groups = cd._union_find_clusters([("a", "b"), ("b", "c"), ("x", "y")])
        as_sets = sorted((sorted(g) for g in groups), key=len, reverse=True)
        assert ["a", "b", "c"] in [sorted(g) for g in groups]
        assert ["x", "y"] in [sorted(g) for g in groups]

    def test_union_find_drops_singletons(self):
        # No edges → no clusters (clusters need ≥2 members)
        assert cd._union_find_clusters([]) == []

    def test_is_dreaming_classifies_source(self):
        dream = SemanticEntry(key="rel:x", value="v", source="dreaming")
        user = SemanticEntry(key="k", value="v", source="manual")
        assert cd._is_dreaming(dream) is True
        assert cd._is_dreaming(user) is False

    def test_parse_json_object_tolerates_fences(self):
        obj = cd._parse_json_object('```json\n{"mergeable": true}\n```')
        assert obj == {"mergeable": True}

    def test_parse_json_array_tolerates_prose(self):
        arr = cd._parse_json_array('here you go: [{"verdict":"skip"}] done')
        assert arr == [{"verdict": "skip"}]

    def test_parse_json_object_returns_none_on_garbage(self):
        assert cd._parse_json_object("not json at all") is None


# ---------------------------------------------------------------------------
# build_plan — reconcile path (embedding-free)
# ---------------------------------------------------------------------------

class TestBuildPlanReconcile:
    async def test_prefix_contradiction_becomes_reconcile_cluster(self, semantic):
        # Same 3-segment prefix + same depth + disjoint values → contradiction.
        await semantic.upsert(SemanticEntry(
            key="user:pref:tone:formal", value="always answer using polished prose",
            source="manual"))
        await semantic.upsert(SemanticEntry(
            key="user:pref:tone:casual", value="reply with short blunt fragments",
            source="manual"))

        plan = await build_plan(semantic)
        reconcile = [c for c in plan.clusters if c.kind == KIND_RECONCILE]
        assert len(reconcile) == 1
        assert set(reconcile[0].member_keys) == {"user:pref:tone:formal", "user:pref:tone:casual"}

    async def test_reconcile_pairs_deduped(self, semantic):
        # detect() fires from both directions; the plan must hold one cluster.
        await semantic.upsert(SemanticEntry(
            key="a:b:c:one", value="zebra mountain ocean", source="manual"))
        await semantic.upsert(SemanticEntry(
            key="a:b:c:two", value="purple guitar telescope", source="manual"))
        plan = await build_plan(semantic)
        reconcile = [c for c in plan.clusters if c.kind == KIND_RECONCILE]
        assert len(reconcile) == 1

    async def test_no_false_reconcile_for_unrelated_keys(self, semantic):
        await semantic.upsert(SemanticEntry(key="proj:x", value="alpha", source="manual"))
        await semantic.upsert(SemanticEntry(key="user:y", value="beta", source="manual"))
        plan = await build_plan(semantic)
        assert [c for c in plan.clusters if c.kind == KIND_RECONCILE] == []


# ---------------------------------------------------------------------------
# build_plan — merge path (deterministic embeddings)
# ---------------------------------------------------------------------------

class TestBuildPlanMerge:
    async def test_near_duplicates_form_merge_cluster(self, semantic_emb):
        await semantic_emb.upsert(SemanticEntry(key="m1", value="GROUPA the user likes tea", source="manual"))
        await semantic_emb.upsert(SemanticEntry(key="m2", value="GROUPA tea is the user's favourite", source="manual"))
        await semantic_emb.upsert(SemanticEntry(key="u1", value="GROUPB unrelated fact", source="manual"))

        plan = await build_plan(semantic_emb, min_similarity=0.85)
        merge = [c for c in plan.clusters if c.kind == KIND_MERGE]
        assert len(merge) == 1
        assert set(merge[0].member_keys) == {"m1", "m2"}
        assert "u1" not in merge[0].member_keys

    async def test_no_merge_without_embeddings(self, semantic):
        await semantic.upsert(SemanticEntry(key="m1", value="same text", source="manual"))
        await semantic.upsert(SemanticEntry(key="m2", value="same text", source="manual"))
        plan = await build_plan(semantic)
        assert [c for c in plan.clusters if c.kind == KIND_MERGE] == []
        assert any("no embedding provider" in n for n in plan.notes)

    async def test_source_versions_snapshotted(self, semantic_emb):
        await semantic_emb.upsert(SemanticEntry(key="m1", value="GROUPA one", source="manual"))
        await semantic_emb.upsert(SemanticEntry(key="m2", value="GROUPA two", source="manual"))
        plan = await build_plan(semantic_emb)
        assert "m1" in plan.source_versions and "m2" in plan.source_versions


# ---------------------------------------------------------------------------
# Dreaming exemption (spec §6.5)
# ---------------------------------------------------------------------------

class TestDreamingExemption:
    async def test_dreaming_facts_excluded_from_merge(self, semantic_emb):
        await semantic_emb.upsert(SemanticEntry(key="m1", value="GROUPA fact", source="manual"))
        await semantic_emb.upsert(SemanticEntry(key="m2", value="GROUPA fact too", source="manual"))
        await semantic_emb.upsert(SemanticEntry(key="d1", value="GROUPA dreamt link", source="dreaming"))

        plan = await build_plan(semantic_emb, min_similarity=0.85)
        all_keys = {k for c in plan.clusters for k in c.member_keys}
        assert "d1" not in all_keys

    async def test_dreaming_facts_excluded_from_reconcile(self, semantic):
        await semantic.upsert(SemanticEntry(
            key="x:y:z:human", value="apple banana cherry", source="manual"))
        await semantic.upsert(SemanticEntry(
            key="x:y:z:dream", value="violin trumpet drums", source="dreaming"))
        plan = await build_plan(semantic)
        all_keys = {k for c in plan.clusters for k in c.member_keys}
        assert "x:y:z:dream" not in all_keys


# ---------------------------------------------------------------------------
# diff_inventory gate (spec §6.4)
# ---------------------------------------------------------------------------

class TestDiffInventory:
    async def test_parses_mergeable_true(self):
        cluster = CandidateCluster(
            cluster_id="c1", kind=KIND_MERGE,
            members=[SemanticEntry(key="a", value="x"), SemanticEntry(key="b", value="y")])
        llm = _stub_llm('{"unique_by_key": {"a": "", "b": "extra detail"}, '
                        '"mergeable": true, "rationale": "b subsumes a"}')
        diff = await diff_inventory(cluster, llm)
        assert diff.mergeable is True
        assert diff.unique_by_key["b"] == "extra detail"

    async def test_both_unique_not_mergeable(self):
        cluster = CandidateCluster(
            cluster_id="c1", kind=KIND_MERGE,
            members=[SemanticEntry(key="a", value="x"), SemanticEntry(key="b", value="y")])
        llm = _stub_llm('{"unique_by_key": {"a": "preference", "b": "complaint"}, '
                        '"mergeable": false, "rationale": "distinct insights"}')
        diff = await diff_inventory(cluster, llm)
        assert diff.mergeable is False

    async def test_llm_failure_fails_safe_to_not_mergeable(self):
        cluster = CandidateCluster(cluster_id="c1", kind=KIND_MERGE, members=[])
        diff = await diff_inventory(cluster, _failing_llm(RuntimeError("down")))
        assert diff.mergeable is False
        assert "unavailable" in diff.rationale

    async def test_unparseable_output_not_mergeable(self):
        cluster = CandidateCluster(cluster_id="c1", kind=KIND_MERGE, members=[])
        diff = await diff_inventory(cluster, _stub_llm("garbage"))
        assert diff.mergeable is False

    async def test_missing_member_key_not_mergeable(self):
        # Codex review #493 — inventory must COVER the cluster members. A
        # response that omits a member but claims mergeable must fail safe.
        cluster = CandidateCluster(
            cluster_id="c1", kind=KIND_MERGE,
            members=[SemanticEntry(key="a", value="x"), SemanticEntry(key="b", value="y")])
        llm = _stub_llm('{"unique_by_key": {"a": ""}, "mergeable": true, "rationale": "looks same"}')
        diff = await diff_inventory(cluster, llm)
        assert diff.mergeable is False

    async def test_phantom_key_not_mergeable(self):
        # Extra/hallucinated keys that don't belong to the cluster → fail safe.
        cluster = CandidateCluster(
            cluster_id="c1", kind=KIND_MERGE,
            members=[SemanticEntry(key="a", value="x"), SemanticEntry(key="b", value="y")])
        llm = _stub_llm('{"unique_by_key": {"a": "", "b": "", "zzz": "ghost"}, '
                        '"mergeable": true, "rationale": "r"}')
        diff = await diff_inventory(cluster, llm)
        assert diff.mergeable is False


# ---------------------------------------------------------------------------
# self_review — strong veto, fail-safe (spec §6.2)
# ---------------------------------------------------------------------------

class TestSelfReview:
    def _merge_cluster(self, cid="c1", mergeable=True):
        c = CandidateCluster(
            cluster_id=cid, kind=KIND_MERGE,
            members=[SemanticEntry(key="a", value="x"), SemanticEntry(key="b", value="y")])
        c.diff = DiffInventory(mergeable=mergeable, rationale="r")
        return c

    async def test_auto_skips_non_mergeable_without_llm(self):
        plan = ConsolidationPlan(clusters=[self._merge_cluster(mergeable=False)])
        # LLM that would approve — must NOT be consulted for the vetoed cluster.
        decisions = await self_review(plan, _stub_llm('[{"cluster_id":"c1","verdict":"approve"}]'))
        assert decisions[0].verdict == VERDICT_SKIP
        assert "not mergeable" in decisions[0].reason

    async def test_applies_llm_verdicts(self):
        plan = ConsolidationPlan(clusters=[self._merge_cluster(cid="c1")])
        llm = _stub_llm('[{"cluster_id":"c1","verdict":"approve","reason":"safe"}]')
        decisions = await self_review(plan, llm)
        assert decisions[0].verdict == VERDICT_APPROVE

    async def test_missing_verdict_defers(self):
        plan = ConsolidationPlan(clusters=[self._merge_cluster(cid="c1")])
        llm = _stub_llm('[{"cluster_id":"other","verdict":"approve"}]')
        decisions = await self_review(plan, llm)
        assert decisions[0].verdict == VERDICT_DEFER

    async def test_llm_failure_defers_never_approves(self):
        plan = ConsolidationPlan(clusters=[self._merge_cluster(cid="c1")])
        decisions = await self_review(plan, _failing_llm(RuntimeError("down")))
        assert all(d.verdict == VERDICT_DEFER for d in decisions)

    async def test_malformed_verdict_defers(self):
        plan = ConsolidationPlan(clusters=[self._merge_cluster(cid="c1")])
        llm = _stub_llm('[{"cluster_id":"c1","verdict":"banana"}]')
        decisions = await self_review(plan, llm)
        assert decisions[0].verdict == VERDICT_DEFER

    async def test_duplicate_cluster_id_defers_not_last_wins(self):
        # Codex review #493 — a repeated id is malformed output. A later
        # "approve" must NOT overwrite an earlier "skip"/"defer" (strong veto).
        plan = ConsolidationPlan(clusters=[self._merge_cluster(cid="c1")])
        llm = _stub_llm('[{"cluster_id":"c1","verdict":"skip","reason":"real problem"},'
                        '{"cluster_id":"c1","verdict":"approve"}]')
        decisions = await self_review(plan, llm)
        assert len(decisions) == 1
        assert decisions[0].verdict == VERDICT_DEFER


# ---------------------------------------------------------------------------
# render_report
# ---------------------------------------------------------------------------

class TestRenderReport:
    def test_report_contains_summary_and_footer(self):
        plan = ConsolidationPlan(scanned=42)
        plan.clusters.append(CandidateCluster(
            cluster_id="merge-1", kind=KIND_MERGE,
            members=[SemanticEntry(key="a", value="x"), SemanticEntry(key="b", value="y")]))
        plan.decisions.append(ReviewDecision(cluster_id="merge-1", verdict=VERDICT_APPROVE, reason="ok"))
        report = render_report(plan)
        assert plan.pass_id in report
        assert "42" in report
        assert "Merge 記錄" in report
        assert "read-only" in report.lower()
        assert "✅" in report

    def test_deferred_count_surfaced(self):
        plan = ConsolidationPlan(scanned=5, deferred_to_next_pass=3)
        report = render_report(plan)
        assert "3" in report and "順延" in report


# ---------------------------------------------------------------------------
# Read-only invariant — the headline contract
# ---------------------------------------------------------------------------

class TestReadOnlyInvariant:
    async def test_build_plan_does_not_mutate_store(self, semantic_emb, db_conn):
        await semantic_emb.upsert(SemanticEntry(key="m1", value="GROUPA tea", source="manual"))
        await semantic_emb.upsert(SemanticEntry(key="m2", value="GROUPA tea too", source="manual"))
        await semantic_emb.upsert(SemanticEntry(
            key="u:v:w:a", value="alpha beta", source="manual"))
        await semantic_emb.upsert(SemanticEntry(
            key="u:v:w:b", value="gamma delta", source="manual"))

        before = await _snapshot(db_conn)
        await build_plan(semantic_emb)
        after = await _snapshot(db_conn)
        assert before == after

    async def test_full_pass_does_not_mutate_store(self, semantic_emb, db_conn):
        await semantic_emb.upsert(SemanticEntry(key="m1", value="GROUPA x", source="manual"))
        await semantic_emb.upsert(SemanticEntry(key="m2", value="GROUPA y", source="manual"))

        before = await _snapshot(db_conn)

        async def combined(messages):
            sys = messages[0]["content"]
            if "difference inventory" in sys:
                return '{"unique_by_key":{"m1":"","m2":"more"},"mergeable":true,"rationale":"r"}'
            # self_review — verdict is irrelevant to the read-only invariant
            return '[]'

        plan, report = await run_convergent_dream(semantic_emb, combined)
        after = await _snapshot(db_conn)
        assert before == after
        assert plan.execution_status == "planned"
        assert "read-only" in report.lower()


# ---------------------------------------------------------------------------
# No silent caps (feedback: no-silent-truncation)
# ---------------------------------------------------------------------------

class TestNoSilentCap:
    async def test_cap_records_deferred(self, semantic_emb):
        # Three independent GROUPA/B/C pairs → 3 merge clusters; cap at 2.
        pairs = [("GROUPA", "a"), ("GROUPB", "b"), ("GROUPC", "c")]
        for marker, p in pairs:
            await semantic_emb.upsert(SemanticEntry(key=f"{p}1", value=f"{marker} one", source="manual"))
            await semantic_emb.upsert(SemanticEntry(key=f"{p}2", value=f"{marker} two", source="manual"))

        plan = await build_plan(semantic_emb, min_similarity=0.85, max_clusters=2)
        merge = [c for c in plan.clusters if c.kind == KIND_MERGE]
        assert len(merge) == 2
        assert plan.deferred_to_next_pass == 1
        assert any("deferred" in n for n in plan.notes)
