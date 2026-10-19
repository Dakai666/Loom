"""Near-duplicate clustering shared by consolidation and the corpus census.

Single-linkage union-find over near-duplicate edges chains transitively
(A~B~C…), so one weak bridge can weld unrelated facts together.
:func:`split_oversized` re-splits such components at stepwise stricter
similarity. The census uses the same two steps so its duplicate figures are
the clusters consolidation would actually propose (#613-D: a raw 845-member
"group" had mean pairwise cos 0.52).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Hashable, Iterable, TypeVar

if TYPE_CHECKING:
    import numpy as np


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

# Near-duplicate neighbours linked per fact — shared by consolidation's
# planner and the corpus census so both build the same graph.
NEAR_DUP_NEIGHBOURS = 5


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


def stack_normalised(
    vectors: Iterable[object],
    *,
    dim: int | None = None,
) -> tuple["np.ndarray", list[int]]:
    """Parse vectors (JSON strings or float lists) into an L2-normalised
    float32 matrix; return it with the input positions kept.

    Unparseable / empty entries are skipped. Only vectors of ``dim`` are kept
    (default: the most common dimension) — embeddings from different models
    can't be compared.
    """
    import json

    import numpy as np

    parsed: list[tuple[int, "np.ndarray"]] = []
    for i, raw in enumerate(vectors):
        if raw is None:
            continue
        try:
            arr = np.asarray(json.loads(raw) if isinstance(raw, (str, bytes)) else raw,
                             dtype=np.float32)
        except (ValueError, TypeError):
            continue
        if arr.ndim == 1 and arr.size:
            parsed.append((i, arr))
    if dim is None and parsed:
        counts: dict[int, int] = {}
        for _, arr in parsed:
            counts[arr.size] = counts.get(arr.size, 0) + 1
        dim = max(counts, key=counts.__getitem__)
    kept = [(i, arr) for i, arr in parsed if arr.size == dim]
    if not kept:
        return np.zeros((0, dim or 0), dtype=np.float32), []
    m = np.stack([arr for _, arr in kept])
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    m /= np.where(norms == 0, 1, norms)
    return m, [i for i, _ in kept]


def knn_edges(
    stored: "np.ndarray",
    *,
    k: int,
    min_similarity: float,
    queries: "np.ndarray | None" = None,
    query_self: "np.ndarray | None" = None,
    block: int = 1024,
) -> tuple["np.ndarray", "np.ndarray", "np.ndarray"]:
    """Top-``k`` cosine neighbours among L2-normalised ``stored`` rows.

    Returns ``(q, s, score)``: query row ``q`` links to stored row ``s`` when
    ``s`` is among its ``k`` nearest and the cosine is ≥ ``min_similarity`` —
    the edge rule of ``find_near_duplicates(limit=k)``, in one blocked matmul
    instead of a full-table sqlite-vec scan per fact (~1.2 s each on an 11k
    corpus). ``queries`` defaults to ``stored`` itself; ``query_self[i]`` is
    the stored row that query ``i`` *is* (or -1), never its own neighbour.
    """
    import numpy as np

    if queries is None:
        queries = stored
        query_self = np.arange(len(stored))
    # Self-matches are masked to -inf and fall below any threshold, so k may
    # be the full stored size.
    k = min(k, len(stored))
    if k <= 0 or len(queries) == 0:
        empty = np.array([], dtype=np.int64)
        return empty, empty, np.array([], dtype=np.float32)
    out_q, out_s, out_v = [], [], []
    for start in range(0, len(queries), block):
        sims = queries[start:start + block] @ stored.T
        if query_self is not None:
            own = query_self[start:start + block]
            rows = np.nonzero(own >= 0)[0]
            sims[rows, own[rows]] = -np.inf
        idx = np.argpartition(-sims, k - 1, axis=1)[:, :k]
        top = np.take_along_axis(sims, idx, axis=1)
        r, c = np.nonzero(top >= min_similarity)
        out_q.append(r + start)
        out_s.append(idx[r, c])
        out_v.append(top[r, c])
    return np.concatenate(out_q), np.concatenate(out_s), np.concatenate(out_v)
