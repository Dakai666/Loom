"""
Memory corpus census — what the semantic store is made of (issue #587).

On 2026-09-18 Loom Agent hand-queried the corpus, got the composition right
(65% auto-extracted, all at 0.8) and then built a wrong mechanism on it
(“flat confidence ⇒ can't rank”; confidence does not rank recall). The census
exists so the next suspicion starts from evidence. Each metric answers a
question that was actually asked that day:

- composition / machine:hand — how much of memory did nobody choose to keep?
- duplicate density — how many facts exist as ≥3 coexisting versions? Uses
  consolidation's own threshold and exemptions, so it measures the gap
  consolidation leaves (e.g. 23 versions of the daily rhythm).
- decay / next prune — how long do facts live, what leaves next?
- access freshness — recall only happens when someone asks; "never accessed"
  is the one objective sign a memory is unused.
- archived by layer — does noise die faster than what was written by hand?
- short values — entries that carry nothing.

A census is a read of the whole table, not hot-path telemetry, so it lives
here rather than as an ``agent_health`` dimension (those are I/O-free
per-session counters). Snapshots go to ``memory_meta`` for week-over-week
trends.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from wcwidth import wcswidth

from loom.core.memory.lifecycle import effective_confidence
from loom.core.memory.ontology import (
    TEMPORAL_ARCHIVED,
    TEMPORAL_MILESTONE,
    TEMPORAL_RECENT,
)
from loom.core.memory.semantic import classify_source

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

#: The threshold consolidation clusters at (``build_plan`` min_similarity).
DUP_SIMILARITY = 0.85
#: A group of this many coexisting versions is a consolidation miss, not a pair.
DUP_GROUP_MIN = 3
#: Lifecycle's default demote/delete threshold (``MemoryLifecycle``).
PRUNE_THRESHOLD = 0.1
#: Display width, so a CJK character counts 2: '在本地' is short,
#: '用戶偏好繁體中文' is a fact (PR #589 review).
SHORT_VALUE_WIDTH = 10
BASELINE_DAYS = 7

_MACHINE_TIERS = {"session_compress", "dreaming"}
_HAND_TIERS = {"agent_memorize", "user_explicit"}
_DECAY_EDGES = ((0.1, "<0.1"), (0.3, "0.1–0.3"), (0.5, "0.3–0.5"), (0.8, "0.5–0.8"))
_SNAPSHOT_PREFIX = "census:"


@dataclass
class DupDensity:
    groups: int                # clusters with ≥ DUP_GROUP_MIN members
    facts_in_groups: int
    largest: int
    largest_sample: str        # a member's value, so the number has a face
    embedded: int              # rows the measurement covered
    unembedded: int            # rows it could not see


@dataclass
class CorpusCensus:
    taken_at: str
    total: int
    by_source: dict[str, int]
    machine: int
    hand: int
    decay: dict[str, int]
    due_archive: int
    due_delete: int
    archived_by_source: dict[str, int]
    access: dict[str, int]
    short_values: int
    dup: DupDensity | None = None
    dup_unavailable: str | None = None
    notes: list[str] = field(default_factory=list)

    # ── persistence ─────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CorpusCensus:
        raw = dict(raw)
        dup = raw.pop("dup", None)
        return cls(**raw, dup=DupDensity(**dup) if dup else None)

    # ── rendering ───────────────────────────────────────────────────────

    def _ratio(self) -> str:
        return f"{self.machine / self.hand:.1f}:1" if self.hand else f"{self.machine}:0"

    def _never_share(self) -> float:
        return self.access["never"] / self.total if self.total else 0.0

    def render_summary(self) -> str:
        dup = (
            f"{self.dup.groups} dup groups (≥{DUP_GROUP_MIN}, max {self.dup.largest})"
            if self.dup else "dup: n/a"
        )
        return (
            f"corpus: {self.total} facts · machine:hand {self._ratio()} · {dup} · "
            f"never accessed {self._never_share():.0%}"
        )

    def render_detail(self, baseline: CorpusCensus | None = None) -> str:
        def delta(now: int, then: int | None) -> str:
            if then is None:
                return ""
            d = now - then
            return f"  (Δ{BASELINE_DAYS}d {d:+d})"

        b = baseline
        lines = [
            f"## memory_corpus  ({self.taken_at[:16]}Z)",
            f"- total: {self.total}{delta(self.total, b and b.total)}",
            f"- machine:hand {self._ratio()} — machine {self.machine}"
            f"{delta(self.machine, b and b.machine)}, hand {self.hand}"
            f"{delta(self.hand, b and b.hand)}",
            "- by source: " + ", ".join(
                f"{k} {v} ({v / self.total:.0%})" for k, v in
                sorted(self.by_source.items(), key=lambda kv: -kv[1])
            ) if self.total else "- by source: (empty)",
        ]
        if self.dup:
            d = self.dup
            lines += [
                f"- duplicate groups (cos≥{DUP_SIMILARITY}, ≥{DUP_GROUP_MIN} versions): "
                f"{d.groups}{delta(d.groups, b and b.dup and b.dup.groups)}, "
                f"holding {d.facts_in_groups} facts"
                f"{delta(d.facts_in_groups, b and b.dup and b.dup.facts_in_groups)}",
                f"  largest: {d.largest} × “{d.largest_sample}”",
                f"  measured {d.embedded} rows; {d.unembedded} have no embedding",
            ]
        else:
            lines.append(f"- duplicate groups: unavailable ({self.dup_unavailable})")
        lines += [
            "- effective confidence: " + ", ".join(
                f"{k} {v}" for k, v in self.decay.items()
            ),
            f"- next prune: archives {self.due_archive}, deletes {self.due_delete}",
            "- archived now: " + (", ".join(
                f"{k} {v}" for k, v in sorted(self.archived_by_source.items())
            ) or "none"),
            f"- accessed ≤7d {self.access['7d']}, ≤30d {self.access['30d']}, "
            f"never {self.access['never']} ({self._never_share():.0%})"
            f"{delta(self.access['never'], b and b.access['never'])}",
            f"- short values (display width <{SHORT_VALUE_WIDTH}): {self.short_values}",
        ]
        if b is None:
            lines.append(f"- trend: no snapshot ≥{BASELINE_DAYS}d old yet")
        lines += [f"- note: {n}" for n in self.notes]
        return "\n".join(lines)


# ── census ──────────────────────────────────────────────────────────────

def _family(source: str | None) -> str:
    return (source or "unknown").split(":", 1)[0] or "unknown"


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


async def take_census(
    db: aiosqlite.Connection, *, now: datetime | None = None,
) -> CorpusCensus:
    """One read-only pass over ``semantic_entries``."""
    now = now or datetime.now(UTC)
    cursor = await db.execute(
        "SELECT value, confidence, source, metadata, updated_at, "
        "last_accessed_at, domain, temporal, embedding FROM semantic_entries"
    )
    rows = await cursor.fetchall()
    # Everything after the fetch is CPU work over ~10k rows (JSON parsing, a
    # matmul); keep it off the event loop the Discord / autonomy sessions
    # share (PR #589 review).
    return await asyncio.to_thread(_census_from_rows, rows, now)


def _census_from_rows(rows: list[Any], now: datetime) -> CorpusCensus:
    by_source: dict[str, int] = {}
    archived_by_source: dict[str, int] = {}
    decay = {label: 0 for _, label in _DECAY_EDGES} | {"≥0.8": 0}
    access = {"7d": 0, "30d": 0, "never": 0}
    machine = hand = short = due_archive = due_delete = 0
    to_cluster: list[tuple[str, str]] = []   # (value, embedding json)
    unembedded = 0

    for value, conf, source, meta_raw, updated, accessed, domain, temporal, emb in rows:
        family = _family(source)
        by_source[family] = by_source.get(family, 0) + 1
        tier, _ = classify_source(source)
        machine += tier in _MACHINE_TIERS
        hand += tier in _HAND_TIERS
        stripped = (value or "").strip()
        if max(wcswidth(stripped), len(stripped)) < SHORT_VALUE_WIDTH:
            short += 1

        updated_at = _parse_ts(updated) or now
        accessed_at = _parse_ts(accessed)
        eff = effective_confidence(
            conf, updated_at, accessed_at, domain, temporal, now=now,
        )
        for edge, label in _DECAY_EDGES:
            if eff < edge:
                decay[label] += 1
                break
        else:
            decay["≥0.8"] += 1
        # The same row sets the next prune acts on (lifecycle
        # _process_table_delete / _process_table_demote).
        if temporal == TEMPORAL_ARCHIVED:
            archived_by_source[family] = archived_by_source.get(family, 0) + 1
            due_delete += eff < PRUNE_THRESHOLD
        elif temporal in (TEMPORAL_RECENT, TEMPORAL_MILESTONE):
            due_archive += eff < PRUNE_THRESHOLD

        if accessed_at is None:
            access["never"] += 1
        else:
            age = now - accessed_at
            access["7d"] += age <= timedelta(days=7)
            access["30d"] += age <= timedelta(days=30)

        # Consolidation's exemptions (build_plan): dreaming output and
        # redirect stubs are not facts to merge.
        try:
            stub = bool(json.loads(meta_raw or "{}").get("redirected_to"))
        except (ValueError, AttributeError):
            stub = False
        if tier == "dreaming" or stub:
            continue
        if emb:
            to_cluster.append((value, emb))
        else:
            unembedded += 1

    census = CorpusCensus(
        taken_at=now.isoformat(), total=len(rows), by_source=by_source,
        machine=machine, hand=hand, decay=decay, due_archive=due_archive,
        due_delete=due_delete, archived_by_source=archived_by_source,
        access=access, short_values=short,
    )
    try:
        census.dup = _duplicate_density(to_cluster, unembedded, census.notes)
    except ImportError:
        census.dup_unavailable = "numpy not installed"
    return census


def _duplicate_density(
    rows: list[tuple[str, str]], unembedded: int, notes: list[str],
) -> DupDensity:
    """Single-linkage groups at ``DUP_SIMILARITY`` — the same union-find over
    near-duplicate edges that consolidation builds, but over the whole corpus
    at once. Blocked matmul keeps memory at ~block × N floats."""
    import numpy as np

    # Convert each row to float32 as it is parsed — a list of Python floats
    # costs ~8× the memory of the array it becomes.
    vectors: list[Any] = []
    values: list[str] = []
    for value, raw in rows:
        try:
            vectors.append(np.asarray(json.loads(raw), dtype=np.float32))
            values.append(value)
        except (ValueError, TypeError):
            unembedded += 1
    if not vectors:
        return DupDensity(0, 0, 0, "", 0, unembedded)

    # Embeddings from different providers can't be compared; measure the
    # dominant dimension and say how many were left out.
    dims: dict[int, int] = {}
    for v in vectors:
        dims[len(v)] = dims.get(len(v), 0) + 1
    dim = max(dims, key=dims.__getitem__)
    keep = [i for i, v in enumerate(vectors) if len(v) == dim]
    if len(keep) < len(vectors):
        notes.append(f"{len(vectors) - len(keep)} embeddings of another dimension not clustered")
        unembedded += len(vectors) - len(keep)
    m = np.stack([vectors[i] for i in keep])
    del vectors
    values = [values[i] for i in keep]
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    m /= np.where(norms == 0, 1, norms)

    parent = list(range(len(m)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    block = 1024
    for start in range(0, len(m), block):
        sims = m[start:start + block] @ m.T
        rows_i, cols = np.nonzero(sims >= DUP_SIMILARITY)
        for r, c in zip(rows_i.tolist(), cols.tolist()):
            a = start + r
            if a < c:
                parent[find(a)] = find(c)

    sizes: dict[int, int] = {}
    for i in range(len(m)):
        root = find(i)
        sizes[root] = sizes.get(root, 0) + 1
    groups = {r: n for r, n in sizes.items() if n >= DUP_GROUP_MIN}
    if not groups:
        return DupDensity(0, 0, 0, "", len(m), unembedded)
    top = max(groups, key=groups.__getitem__)
    sample = " ".join(values[top].split())
    return DupDensity(
        groups=len(groups), facts_in_groups=sum(groups.values()),
        largest=groups[top],
        largest_sample=sample[:60] + ("…" if len(sample) > 60 else ""),
        embedded=len(m), unembedded=unembedded,
    )


# ── snapshots ───────────────────────────────────────────────────────────

async def save_snapshot(db: aiosqlite.Connection, census: CorpusCensus) -> None:
    """One snapshot per UTC day (a later census the same day replaces it)."""
    await db.execute(
        "INSERT INTO memory_meta(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (_SNAPSHOT_PREFIX + census.taken_at[:10],
         json.dumps(census.to_dict(), ensure_ascii=False), census.taken_at),
    )
    await db.commit()


async def load_baseline(
    db: aiosqlite.Connection, *, now: datetime | None = None,
) -> CorpusCensus | None:
    """The newest snapshot at least ``BASELINE_DAYS`` old, or ``None``."""
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=BASELINE_DAYS)).strftime("%Y-%m-%d")
    cursor = await db.execute(
        "SELECT value FROM memory_meta WHERE key LIKE ? AND key <= ? "
        "ORDER BY key DESC LIMIT 1",
        (_SNAPSHOT_PREFIX + "%", _SNAPSHOT_PREFIX + cutoff),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    try:
        return CorpusCensus.from_dict(json.loads(row[0]))
    except (ValueError, TypeError) as exc:
        logger.warning("census baseline unreadable (%s)", exc)
        return None
