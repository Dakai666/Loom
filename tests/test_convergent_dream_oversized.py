"""
Oversized merge clusters are split, never sent whole (#603).

Union-find over near-duplicate edges chains A~B~C~… into one component even
when A and the far end share nothing. On the live store (2026-10-01) the
build_plan clustering produced a 258-member component once dreaming output
joined merge clustering (68 without it). A component that size cannot be a
single duplicate claim, and a diff-inventory over it would blow the LLM budget
every weekly pass. Oversized components are re-split at stepwise stricter
similarity so chains break at their weakest links; whatever still exceeds the
cap is not proposed, and the plan says so.
"""

from __future__ import annotations

from loom.core.cognition.consolidation import KIND_MERGE, _split_oversized, build_plan
from loom.core.memory.semantic import SemanticEntry

from tests.test_convergent_dream_readonly import db_conn, semantic_emb, store, tmp_db  # noqa: F401


def _scores(pairs: dict[tuple[str, str], float]) -> dict[frozenset[str], float]:
    return {frozenset(k): v for k, v in pairs.items()}


class TestSplitOversized:
    def test_small_groups_untouched(self):
        groups, notes = _split_oversized(
            [{"a", "b", "c"}], _scores({("a", "b"): 0.9, ("b", "c"): 0.9}),
            base=0.85, max_members=3,
        )
        assert groups == [{"a", "b", "c"}]
        assert notes == []

    def test_chain_breaks_at_weakest_link(self):
        # a=b=c tightly, c~d weak bridge, d=e=f tightly
        scores = _scores({
            ("a", "b"): 0.95, ("b", "c"): 0.95,
            ("c", "d"): 0.86,
            ("d", "e"): 0.95, ("e", "f"): 0.95,
        })
        groups, notes = _split_oversized(
            [{"a", "b", "c", "d", "e", "f"}], scores, base=0.85, max_members=3,
        )
        assert sorted(map(sorted, groups)) == [["a", "b", "c"], ["d", "e", "f"]]
        assert any("6" in n and "split" in n for n in notes)

    def test_unsplittable_clique_is_dropped_and_reported(self):
        keys = [f"k{i}" for i in range(5)]
        scores = _scores({(a, b): 0.999 for i, a in enumerate(keys) for b in keys[i + 1:]})
        groups, notes = _split_oversized([set(keys)], scores, base=0.85, max_members=3)
        assert groups == []
        assert any("not proposed" in n for n in notes)

    def test_singletons_from_split_are_not_clusters(self):
        scores = _scores({("a", "b"): 0.95, ("b", "c"): 0.86, ("c", "d"): 0.86})
        groups, _ = _split_oversized([{"a", "b", "c", "d"}], scores, base=0.85, max_members=3)
        assert groups == [{"a", "b"}]


class TestBuildPlanCap:
    async def test_oversized_component_not_proposed_whole(self, semantic_emb):  # noqa: F811
        for i in range(4):
            await semantic_emb.upsert(SemanticEntry(key=f"g{i}", value=f"GROUPA item {i}", source="manual"))
        plan = await build_plan(semantic_emb, min_similarity=0.85, max_cluster_members=3)
        assert all(len(c.members) <= 3 for c in plan.clusters if c.kind == KIND_MERGE)
        assert any("not proposed" in n or "split" in n for n in plan.notes)


def test_identical_float32_scores_survive_the_final_step():
    """float32 cosines of identical vectors read 0.99999994; the last split
    step (1.0) must keep those edges instead of shattering the group (#618)."""
    from loom.core.memory.clustering import split_component

    keys = [f"k{i}" for i in range(4)]
    scores = {frozenset((a, b)): 0.99999994 for a in keys for b in keys if a < b}
    scores[frozenset(("k0", "k1"))] = 0.90  # weakest link breaks first
    pieces, unsplittable = split_component(
        set(keys), scores, base=0.85, max_members=3,
    )
    # Splitting at ≥0.88 keeps every 0.99999994 edge: still 4 > 3 members,
    # stepping on to 1.0 must still see the edges, so the group is reported
    # as one unsplittable dense group rather than lost as singletons.
    assert pieces == []
    assert unsplittable == [set(keys)]
