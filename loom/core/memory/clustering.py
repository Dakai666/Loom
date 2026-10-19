"""Near-duplicate clustering shared by consolidation and the corpus census.

Single-linkage union-find over near-duplicate edges chains transitively
(A~B~C…), so one weak bridge can weld unrelated facts together.
:func:`split_oversized` re-splits such components at stepwise stricter
similarity. The census uses the same two steps so its duplicate figures are
the clusters consolidation would actually propose (#613-D: a raw 845-member
"group" had mean pairwise cos 0.52).
"""

from __future__ import annotations


def union_find_clusters(pairs: list[tuple[str, str]]) -> list[set[str]]:
    """Group keys into connected components from a list of (key_a, key_b) edges."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for a, b in pairs:
        union(a, b)

    groups: dict[str, set[str]] = {}
    for key in parent:
        root = find(key)
        groups.setdefault(root, set()).add(key)
    return [g for g in groups.values() if len(g) >= 2]


# Largest merge cluster proposed whole (#603). Diff inventory was verified on a
# real 42-member cluster; beyond that a single response is unreliable and a
# chained union-find component is rarely one duplicate claim anyway.
MAX_CLUSTER_MEMBERS = 40

SPLIT_STEP = 0.03


def split_oversized(
    groups: list[set[str]],
    pair_score: dict[frozenset[str], float],
    *,
    base: float,
    max_members: int = MAX_CLUSTER_MEMBERS,
) -> tuple[list[set[str]], list[str]]:
    """Re-split components larger than ``max_members`` (#603).

    Union-find chains near-duplicates transitively (A~B~C~…), so one weak
    bridge can weld unrelated groups together. An oversized component is
    re-clustered over its own edges at a stepwise stricter threshold until
    every piece fits; chains break at their weakest links first. A piece that
    still exceeds the cap at similarity 1.0 is not proposed. Returns the
    resulting groups plus notes for the plan (no silent caps).
    """
    out: list[set[str]] = []
    notes: list[str] = []

    def split(group: set[str], threshold: float) -> tuple[list[set[str]], int]:
        if len(group) <= max_members:
            return [group], 0
        if threshold > 1.0:
            return [], len(group)
        edges = [
            tuple(pair) for pair, score in pair_score.items()
            if score >= threshold and pair <= group
        ]
        subs = union_find_clusters(edges)  # type: ignore[arg-type]
        if not subs:
            # Shattered straight to singletons: a dense group that never broke
            # along a weak link — not splittable, so not proposed (reported).
            return [], len(group)
        pieces: list[set[str]] = []
        dropped = 0
        for sub in subs:
            kept, lost = split(sub, threshold + SPLIT_STEP)
            pieces += kept
            dropped += lost
        return pieces, dropped

    for group in groups:
        if len(group) <= max_members:
            out.append(group)
            continue
        pieces, dropped = split(group, base + SPLIT_STEP)
        out += pieces
        note = (
            f"oversized merge cluster ({len(group)} members, chained) split at "
            f"stricter similarity into {len(pieces)} cluster(s)"
        )
        if dropped:
            note += f"; {dropped} facts in still-oversized pieces not proposed"
        notes.append(note)
    return out, notes
