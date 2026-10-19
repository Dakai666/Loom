"""Near-duplicate clustering shared by consolidation and the corpus census.

Single-linkage union-find over near-duplicate edges chains transitively
(A~B~C…), so one weak bridge can weld unrelated facts together.
:func:`split_oversized` re-splits such components at stepwise stricter
similarity. The census uses the same two steps so its duplicate figures are
the clusters consolidation would actually propose (#613-D: a raw 845-member
"group" had mean pairwise cos 0.52).
"""

from __future__ import annotations

from typing import Hashable, Iterable, TypeVar


K = TypeVar("K", bound=Hashable)


def union_find_clusters(pairs: Iterable[tuple[K, K]]) -> list[set[K]]:
    """Group keys into connected components from a list of (key_a, key_b) edges."""
    parent: dict[K, K] = {}

    def find(x: K) -> K:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: K, b: K) -> None:
        parent[find(a)] = find(b)

    for a, b in pairs:
        union(a, b)

    groups: dict[K, set[K]] = {}
    for key in parent:
        root = find(key)
        groups.setdefault(root, set()).add(key)
    return [g for g in groups.values() if len(g) >= 2]


# Largest merge cluster proposed whole (#603). Diff inventory was verified on a
# real 42-member cluster; beyond that a single response is unreliable and a
# chained union-find component is rarely one duplicate claim anyway.
MAX_CLUSTER_MEMBERS = 40

SPLIT_STEP = 0.03


# Scores are float32 cosines: an identical pair can read 0.99999994, so the
# final 1.0 step must not lose it (#618 review).
_SCORE_EPS = 1e-6


def split_component(
    group: set[K],
    pair_score: dict[frozenset[K], float],
    *,
    base: float,
    max_members: int = MAX_CLUSTER_MEMBERS,
) -> tuple[list[set[K]], list[set[K]]]:
    """Split one component; return ``(pieces, unsplittable)``.

    ``pieces`` all fit ``max_members``. ``unsplittable`` are sub-components
    still oversized at similarity 1.0 — dense duplicate floods, not chains.
    Thresholds advance by whole steps from ``base`` (no float accumulation).
    """
    pieces: list[set[K]] = []
    unsplittable: list[set[K]] = []

    def split(sub: set[K], step: int) -> None:
        if len(sub) <= max_members:
            pieces.append(sub)
            return
        threshold = round(base + step * SPLIT_STEP, 6)
        if threshold > 1.0 + _SCORE_EPS:
            unsplittable.append(sub)
            return
        edges = [
            tuple(pair) for pair, score in pair_score.items()
            if score >= threshold - _SCORE_EPS and pair <= sub
        ]
        subs = union_find_clusters(edges)  # type: ignore[arg-type]
        if not subs:
            # Shattered straight to singletons: a dense group that never broke
            # along a weak link — not splittable.
            unsplittable.append(sub)
            return
        for piece in subs:
            split(piece, step + 1)

    split(group, 1)
    return pieces, unsplittable


def split_oversized(
    groups: list[set[K]],
    pair_score: dict[frozenset[K], float],
    *,
    base: float,
    max_members: int = MAX_CLUSTER_MEMBERS,
) -> tuple[list[set[K]], list[str]]:
    """Re-split components larger than ``max_members`` (#603).

    Union-find chains near-duplicates transitively (A~B~C~…), so one weak
    bridge can weld unrelated groups together. An oversized component is
    re-clustered over its own edges at a stepwise stricter threshold until
    every piece fits; chains break at their weakest links first. A piece that
    still exceeds the cap at similarity 1.0 is not proposed. Returns the
    resulting groups plus notes for the plan (no silent caps).
    """
    out: list[set[K]] = []
    notes: list[str] = []
    for group in groups:
        if len(group) <= max_members:
            out.append(group)
            continue
        pieces, unsplittable = split_component(
            group, pair_score, base=base, max_members=max_members,
        )
        out += pieces
        note = (
            f"oversized merge cluster ({len(group)} members, chained) split at "
            f"stricter similarity into {len(pieces)} cluster(s)"
        )
        dropped = sum(len(u) for u in unsplittable)
        if dropped:
            note += f"; {dropped} facts in still-oversized pieces not proposed"
        notes.append(note)
    return out, notes
