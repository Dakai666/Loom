"""
memory_hygiene/dedup.py — batch deduplication of semantic memory.

Run via the memory_hygiene skill when ``loom status`` shows the fact count
creeping back into the thousands. This script's ``--analyze`` mode is
read-only and safe to run any time; ``--apply`` mutates the DB through the
framework API (``SemanticMemory.delete`` / ``upsert``), never raw SQL —
which keeps the skill's ``reject_direct_db_mutation`` precondition happy.

Pass 1 (exact-value duplicates):
    Same ``value`` string appearing N times under auto-compress sources is
    pure waste — the LLM emitted the same fact in N different sessions and
    the old admission gate (lexical-only word overlap) failed to catch the
    overlap when surrounding text differed. We keep the earliest entry
    (preserves history) and delete the rest.

Pass 2 (embedding-similarity clusters):
    For each auto-compress entry with an embedding, the script looks for an
    OLDER near-duplicate (cosine >= ``--cosine``, default 0.95) and marks
    the newer entry for deletion. The "older wins" rule makes the delete
    set deterministic and acyclic without explicit union-find.

Usage:
    python skills/memory_hygiene/dedup.py --analyze
    python skills/memory_hygiene/dedup.py --apply --backup-confirmed
    python skills/memory_hygiene/dedup.py --analyze --cosine 0.90  # looser

Always create a backup first:
    cp ~/.loom/memory.db ~/.loom/backups/memory.db.$(date +%Y%m%d).bak
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

# Make the script runnable from anywhere in the repo.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from loom.core.memory.semantic import SemanticMemory  # noqa: E402
from loom.core.memory.store import SQLiteStore  # noqa: E402


# Auto-compress entries are the dominant noise pool (~89% of the 6.6k bloat
# 絲絲 found in audit-A r2). The key pattern is set in
# ``loom/core/session.py:compress_session``:
#   session:<session_id>:<timestamp>:fact:<i>
AUTO_COMPRESS_KEY_PATTERN = "session:%:fact:%"

# Memorize entries (manual ``memorize`` tool writes) stay untouched in Pass 1.
# They're the agent's explicit saves — lower volume and deserve more careful
# judgment than mechanical exact-dedup. Phase 2 handles them separately.


async def _gather_counts(sem_db) -> dict[str, int]:
    """Read-only snapshot of the corpus distribution by source bucket."""
    queries = {
        "total": "SELECT COUNT(*) FROM semantic_entries",
        "auto_compress": (
            "SELECT COUNT(*) FROM semantic_entries "
            "WHERE source LIKE 'session:%'"
        ),
        "memorize": "SELECT COUNT(*) FROM semantic_entries WHERE source='memorize'",
        "skill_eval": (
            "SELECT COUNT(*) FROM semantic_entries WHERE source LIKE 'skill_eval%'"
        ),
        "with_embedding": (
            "SELECT COUNT(*) FROM semantic_entries WHERE embedding IS NOT NULL"
        ),
        "temporal_archived": (
            "SELECT COUNT(*) FROM semantic_entries WHERE temporal='archived'"
        ),
    }
    out: dict[str, int] = {}
    for name, sql in queries.items():
        cur = await sem_db.execute(sql)
        row = await cur.fetchone()
        out[name] = row[0] if row else 0
    return out


async def _exact_dup_clusters(sem_db) -> list[tuple[str, int, list[str], str]]:
    """Return (value, count, keys_sorted_by_created_at, earliest_created) for
    each exact-value duplicate cluster among auto-compress entries.

    Sorted by ``created_at`` so caller can keep keys[0] (oldest = most
    representative) and delete keys[1:].
    """
    cur = await sem_db.execute(
        """
        SELECT value,
               COUNT(*) AS c,
               json_group_array(key) AS keys,
               json_group_array(created_at) AS created_ats,
               MIN(created_at) AS first_seen
        FROM semantic_entries
        WHERE source LIKE 'session:%'
        GROUP BY value
        HAVING c > 1
        ORDER BY c DESC, first_seen ASC
        """
    )
    rows = await cur.fetchall()

    clusters: list[tuple[str, int, list[str], str]] = []
    for value, count, keys_json, created_ats_json, first_seen in rows:
        keys = json.loads(keys_json)
        created_ats = json.loads(created_ats_json)
        # Sort keys by their created_at — oldest first (kept), rest deleted.
        sorted_keys = [
            k for _, k in sorted(zip(created_ats, keys))
        ]
        clusters.append((value, count, sorted_keys, first_seen))
    return clusters


async def _embedding_dup_keys(
    sem_db, *, similarity: float
) -> list[tuple[str, str, float, str]]:
    """Pass 2: find auto-compress entries that have an OLDER near-duplicate.

    Returns (key_to_delete, canonical_key, score, value_snippet) tuples.
    The "older wins" rule makes the delete set deterministic — for any
    cluster, only the oldest entry survives, the rest mark each other off.

    Cost: O(n) per-entry queries, each leaning on sqlite-vec's
    vec_distance_cosine. ~4k entries finishes in seconds.
    """
    cur = await sem_db.execute(
        """
        SELECT key, value, created_at, embedding
        FROM semantic_entries
        WHERE source LIKE 'session:%' AND embedding IS NOT NULL
        ORDER BY created_at ASC
        """
    )
    rows = await cur.fetchall()

    to_delete: list[tuple[str, str, float, str]] = []
    for key, value, created_at, embedding in rows:
        cur = await sem_db.execute(
            """
            SELECT key, 1.0 - vec_distance_cosine(embedding, ?) AS score
            FROM semantic_entries
            WHERE source LIKE 'session:%'
              AND embedding IS NOT NULL
              AND key != ?
              AND created_at < ?
              AND (1.0 - vec_distance_cosine(embedding, ?)) >= ?
            ORDER BY score DESC
            LIMIT 1
            """,
            (embedding, key, created_at, embedding, similarity),
        )
        row = await cur.fetchone()
        if row:
            canonical_key, score = row
            snippet = value.replace("\n", " ")[:80]
            to_delete.append((key, canonical_key, float(score), snippet))
    return to_delete


def _print_analysis(
    counts: dict[str, int],
    clusters: list[tuple[str, int, list[str], str]],
    embedding_dups: list[tuple[str, str, float, str]],
    cosine_threshold: float,
    *,
    sample_size: int = 10,
) -> None:
    """Pretty-print the analyze report to stdout."""
    print("=" * 70)
    print(" semantic_entries snapshot")
    print("=" * 70)
    for k, v in counts.items():
        print(f"  {k:<22} {v:>8}")
    print()

    dup_rows = sum(c - 1 for _, c, _, _ in clusters)
    distinct_dup_groups = len(clusters)
    print("=" * 70)
    print(" Pass 1: exact-value duplicates (auto-compress source only)")
    print("=" * 70)
    print(f"  distinct duplicate groups : {distinct_dup_groups}")
    print(f"  redundant rows (deletable): {dup_rows}")
    if counts.get("total", 0) > 0:
        pct = 100.0 * dup_rows / counts["total"]
        print(f"  reduction if applied      : {pct:.1f}% of total")
    print()

    if clusters:
        print(f"Top {sample_size} exact-dup clusters by repetition count:")
        for i, (value, count, keys, first_seen) in enumerate(clusters[:sample_size], 1):
            snippet = value.replace("\n", " ")[:120]
            print(f"  [{i:>2}] x{count:<4} since {first_seen[:10]}  {snippet!r}")
        print()

    print("=" * 70)
    print(f" Pass 2: embedding-similarity dups (cosine >= {cosine_threshold:.2f})")
    print("=" * 70)
    print(f"  redundant rows (deletable): {len(embedding_dups)}")
    if counts.get("total", 0) > 0 and embedding_dups:
        pct = 100.0 * len(embedding_dups) / counts["total"]
        print(f"  reduction if applied      : {pct:.1f}% of total")
    print()

    if embedding_dups:
        print(f"Top {sample_size} embedding-dup pairs by cosine:")
        sorted_dups = sorted(embedding_dups, key=lambda r: r[2], reverse=True)
        for i, (dk, ck, score, snippet) in enumerate(sorted_dups[:sample_size], 1):
            print(f"  [{i:>2}] cos={score:.3f}  delete={dk!r}")
            print(f"        keep  ={ck!r}")
            print(f"        text  ={snippet!r}")
        print()


async def _apply(
    sem_db,
    clusters: list[tuple[str, int, list[str], str]],
    embedding_dups: list[tuple[str, str, float, str]],
) -> tuple[int, int]:
    """Delete redundant rows via SemanticMemory.delete (framework API).

    Returns (deleted, skipped_missing). Pass 1 runs first; Pass 2 picks up
    whatever's left — the union is computed by skipping keys already
    deleted in Pass 1 (SemanticMemory.delete returns False for missing
    keys, which we count as ``skipped``).
    """
    semantic = SemanticMemory(sem_db)
    deleted = 0
    skipped = 0

    # Pass 1: exact-value dup clusters — keep keys[0] (earliest), delete rest.
    pass1_deleted_keys: set[str] = set()
    for _, _, keys, _ in clusters:
        for k in keys[1:]:
            ok = await semantic.delete(k)
            if ok:
                deleted += 1
                pass1_deleted_keys.add(k)
            else:
                skipped += 1

    # Pass 2: embedding-similarity dups — skip any key already gone in Pass 1.
    for dk, _ck, _score, _snippet in embedding_dups:
        if dk in pass1_deleted_keys:
            continue
        ok = await semantic.delete(dk)
        if ok:
            deleted += 1
        else:
            skipped += 1

    return deleted, skipped


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", action="store_true",
                        help="Read-only report; safe to run any time.")
    parser.add_argument("--apply", action="store_true",
                        help="Execute deletions. Requires --backup-confirmed.")
    parser.add_argument("--backup-confirmed", action="store_true",
                        help="Acknowledges that ~/.loom/memory.db is backed up.")
    parser.add_argument("--db", default="~/.loom/memory.db",
                        help="Path to memory.db (default: ~/.loom/memory.db).")
    parser.add_argument("--sample", type=int, default=10,
                        help="How many top clusters to print (default: 10).")
    parser.add_argument("--cosine", type=float, default=0.95,
                        help="Pass-2 embedding cosine threshold (default: 0.95).")
    args = parser.parse_args()

    if not (args.analyze or args.apply):
        parser.error("pick one of --analyze or --apply")

    if args.apply and not args.backup_confirmed:
        parser.error(
            "--apply requires --backup-confirmed. "
            "Run: cp ~/.loom/memory.db ~/.loom/backups/memory.db.$(date +%Y%m%d).bak"
        )

    store = SQLiteStore(args.db)
    # Note: we do NOT call store.initialize() — the DB already exists and
    # initialize() runs ALTER TABLE migrations + permission tightening that
    # are unrelated to dedup. Just open a connection.
    async with store.connect() as db:
        counts_before = await _gather_counts(db)
        clusters = await _exact_dup_clusters(db)
        print("Scanning for embedding-similarity dups (this can take a minute)...")
        embedding_dups = await _embedding_dup_keys(db, similarity=args.cosine)

        _print_analysis(
            counts_before, clusters, embedding_dups, args.cosine,
            sample_size=args.sample,
        )

        if not args.apply:
            return 0

        print("Applying deletions via SemanticMemory.delete ...")
        deleted, skipped = await _apply(db, clusters, embedding_dups)
        print(f"  deleted: {deleted}")
        print(f"  skipped (already gone): {skipped}")
        print()

        counts_after = await _gather_counts(db)
        print("=" * 70)
        print(" After cleanup")
        print("=" * 70)
        for k in counts_before:
            delta = counts_after[k] - counts_before[k]
            sign = "+" if delta >= 0 else ""
            print(f"  {k:<22} {counts_after[k]:>8}  ({sign}{delta})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
