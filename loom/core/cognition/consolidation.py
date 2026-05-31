"""
Convergent Dream — read-only memory consolidation planner for Loom (#488, P1).

The counterpart to ``dreaming.dream_cycle`` (the *divergent* dream that grows
new connections). The convergent dream *consolidates*: it scans the semantic
store, finds duplicate / contradicting / orphaned facts, and proposes how to
fuse or reconcile them — but in this first phase it **never writes to the DB**.

Pipeline (spec #56 §4, "說夢話 model")::

    index → plan → self_review → (execute) → report

P1 implements ``index → plan → self_review → report`` only. ``execute`` (the
``semantic.consolidate()`` call) lands in P2 (#489); the MERGE arm + batch
reconcile + orphan criteria land in P3 (#490). Everything here is read-only
so the convergent dream can be "seen, talked through, and recorded" before it
is ever allowed to touch a fact.

Design decisions
----------------
* **Read-only by construction** — this module imports no write path. It calls
  ``semantic`` only through ``list_recent`` / ``find_near_duplicates`` /
  ``get`` and ``ContradictionDetector.detect`` (all reads). The orchestrator
  asserts the corpus is byte-identical before and after.
* **Pure cognition** — no imports from platform, harness, or autonomy. The
  ``ToolDefinition`` adapter lives in ``loom.core.memory.maintenance``.
* **dreaming exemption is principled, not tactical** (spec §6.5) — facts whose
  source classifies as ``dreaming`` are the divergent dream's own output and
  are never proposed for merge/reconcile; a dreaming fact with no relations is
  an *allowed orphan* (a connection still waiting for its place), not garbage.
* **Strong veto** (spec §6.2) — ``self_review`` verdicts are hard boundaries,
  not advisory scores. ``skip`` / ``defer`` mean the cluster is not executed.
* **No silent caps** (feedback: no-silent-truncation) — when a batch cap drops
  clusters, the plan records how many were deferred for the next pass.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Awaitable, Callable, TYPE_CHECKING

from loom.core.memory.semantic import SemanticEntry, classify_source

if TYPE_CHECKING:
    from loom.core.memory.semantic import SemanticMemory

logger = logging.getLogger(__name__)

LLMFn = Callable[[list[dict]], Awaitable[str]]

# Cluster kinds — what the convergent dream proposes to do with a group.
KIND_MERGE = "merge"          # ≥2 near-duplicate facts to fuse into one
KIND_RECONCILE = "reconcile"  # facts that contradict and need arbitration
KIND_CLEAN = "clean"          # orphaned references to delete

# Review verdicts (spec §6.2). Hard boundaries, not scores.
VERDICT_APPROVE = "approve"
VERDICT_SKIP = "skip"
VERDICT_DEFER = "defer"


# ---------------------------------------------------------------------------
# Data structures (spec §5.1 ConsolidationPlan)
# ---------------------------------------------------------------------------

@dataclass
class DiffInventory:
    """Result of the LLM "差異盤點" gate for a merge cluster (spec §6.4).

    ``unique_by_key`` maps each member key → what that member says that no
    other member does. ``mergeable`` is False when ≥2 members carry unique
    content (they are distinct insights that merely look alike).
    """
    unique_by_key: dict[str, str] = field(default_factory=dict)
    mergeable: bool = False
    rationale: str = ""


@dataclass
class CandidateCluster:
    """A group of facts the convergent dream proposes to act on.

    Read-only: holds the member entries and the *proposed* action, never the
    executed result. ``proposed_action`` is a human/agent-readable description
    of what execute (P2) would do — not a mutation.
    """
    cluster_id: str
    kind: str                                   # KIND_MERGE / KIND_RECONCILE / KIND_CLEAN
    members: list[SemanticEntry]
    similarity: float = 0.0                     # max pairwise cosine (merge) or detector score
    diff: DiffInventory | None = None           # merge only — set by diff_inventory()
    proposed_action: str = ""                   # what execute would do (read-only description)

    @property
    def member_keys(self) -> list[str]:
        return [m.key for m in self.members]


@dataclass
class ReviewDecision:
    """絲絲's self-review verdict on one cluster (spec §6.2). Hard boundary."""
    cluster_id: str
    verdict: str                                # VERDICT_APPROVE / SKIP / DEFER
    reason: str = ""


@dataclass
class ConsolidationPlan:
    """Durable, read-only plan produced by one convergent-dream pass (spec §5.1).

    ``source_versions`` snapshots each member's ``updated_at`` so the execute
    step (P2) can detect facts that changed after review and mark the cluster
    stale rather than acting on a stale plan.
    """
    pass_id: str = field(default_factory=lambda: f"dream-{uuid.uuid4().hex[:12]}")
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    clusters: list[CandidateCluster] = field(default_factory=list)
    decisions: list[ReviewDecision] = field(default_factory=list)
    source_versions: dict[str, str] = field(default_factory=dict)  # key → updated_at iso
    execution_status: str = "planned"           # planned (P1) → executed/stale (P2)
    # Coverage accounting — no silent caps.
    scanned: int = 0
    deferred_to_next_pass: int = 0
    notes: list[str] = field(default_factory=list)

    def decision_for(self, cluster_id: str) -> ReviewDecision | None:
        for d in self.decisions:
            if d.cluster_id == cluster_id:
                return d
        return None

    def counts(self) -> dict[str, int]:
        c = {KIND_MERGE: 0, KIND_RECONCILE: 0, KIND_CLEAN: 0}
        for cl in self.clusters:
            c[cl.kind] = c.get(cl.kind, 0) + 1
        return c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_dreaming(entry: SemanticEntry) -> bool:
    """True if the entry is the divergent dream's own output (spec §6.5)."""
    tier, _ = classify_source(entry.source)
    return tier == "dreaming"


def _union_find_clusters(pairs: list[tuple[str, str]]) -> list[set[str]]:
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


# ---------------------------------------------------------------------------
# Step 2 — plan (read-only candidate discovery)
# ---------------------------------------------------------------------------

async def build_plan(
    semantic: "SemanticMemory",
    *,
    corpus_limit: int = 2000,
    min_similarity: float = 0.85,
    max_clusters: int = 20,
) -> ConsolidationPlan:
    """Scan the semantic store and propose merge / reconcile / clean clusters.

    Strictly read-only. ``max_clusters`` caps how many merge clusters one pass
    proposes (spec §7 batch cap); the overflow count is recorded in the plan,
    never silently dropped.
    """
    plan = ConsolidationPlan()

    corpus = await semantic.list_recent(limit=corpus_limit)
    plan.scanned = len(corpus)
    by_key = {e.key: e for e in corpus}

    # ── Merge candidates: near-duplicate clustering (dreaming-exempt) ──────
    edges: list[tuple[str, str]] = []
    pair_score: dict[frozenset[str], float] = {}
    if semantic.has_embeddings:
        for entry in corpus:
            if _is_dreaming(entry):
                continue  # spec §6.5 — never propose dreaming output for merge
            try:
                neighbours = await semantic.find_near_duplicates(
                    entry.value,
                    min_similarity=min_similarity,
                    within_days=None,           # whole corpus, not the write-time 7-day window
                    limit=5,
                    exclude_key=entry.key,
                )
            except Exception as exc:  # embedding outage must not abort the pass
                plan.notes.append(f"near-dup lookup failed for {entry.key!r}: {exc}")
                continue
            for neighbour, score in neighbours:
                if _is_dreaming(neighbour) or neighbour.key not in by_key:
                    continue
                edges.append((entry.key, neighbour.key))
                pair = frozenset((entry.key, neighbour.key))
                pair_score[pair] = max(pair_score.get(pair, 0.0), score)
    else:
        plan.notes.append("no embedding provider — merge candidates skipped this pass")

    merge_groups = _union_find_clusters(edges)
    # Stable ordering: highest-similarity clusters first so the batch cap keeps
    # the most confident merges.
    def _group_score(group: set[str]) -> float:
        return max(
            (pair_score.get(frozenset((a, b)), 0.0)
             for a in group for b in group if a != b),
            default=0.0,
        )
    merge_groups.sort(key=_group_score, reverse=True)

    if len(merge_groups) > max_clusters:
        plan.deferred_to_next_pass = len(merge_groups) - max_clusters
        plan.notes.append(
            f"{len(merge_groups)} merge clusters found; capped at {max_clusters} "
            f"this pass, {plan.deferred_to_next_pass} deferred to next pass."
        )
        merge_groups = merge_groups[:max_clusters]

    for group in merge_groups:
        members = [by_key[k] for k in group if k in by_key]
        if len(members) < 2:
            continue
        plan.clusters.append(CandidateCluster(
            cluster_id=f"merge-{uuid.uuid4().hex[:8]}",
            kind=KIND_MERGE,
            members=members,
            similarity=_group_score(group),
            proposed_action="fuse into one refined fact (pending diff-inventory + self-review)",
        ))

    # ── Reconcile candidates: batch contradiction scan (dreaming-exempt) ──
    # P1 reuses ContradictionDetector.detect(entry) over each stored fact; the
    # dedicated detect_pairs() batch + MERGE arm land in P3 (#490).
    from loom.core.memory.contradiction import ContradictionDetector

    detector = ContradictionDetector(semantic)
    seen_pairs: set[frozenset[str]] = set()
    for entry in corpus:
        if _is_dreaming(entry):
            continue
        try:
            contradictions = await detector.detect(entry)
        except Exception as exc:
            plan.notes.append(f"contradiction scan failed for {entry.key!r}: {exc}")
            continue
        for c in contradictions:
            if _is_dreaming(c.existing):
                continue
            pair = frozenset((entry.key, c.existing.key))
            if entry.key == c.existing.key or pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            plan.clusters.append(CandidateCluster(
                cluster_id=f"reconcile-{uuid.uuid4().hex[:8]}",
                kind=KIND_RECONCILE,
                members=[c.existing, entry],
                similarity=c.similarity_score,
                proposed_action=(
                    "arbitrate by trust tier then recency "
                    "(MERGE arm deferred to P3 #490)"
                ),
            ))

    # ── Clean candidates: dreaming allowed-orphans are reported, not cleaned ──
    # Deeper orphan criteria (dangling key references) is open (#490 / spec §11-Q7).
    # P1 only classifies dreaming orphans so the report is honest about them.
    plan.notes.append(
        "clean phase: orphan-reference criteria deferred to P3 (#490); "
        "dreaming-sourced entries treated as allowed orphans (never cleaned)."
    )

    # ── Snapshot source versions for stale detection at execute time (P2) ──
    for cluster in plan.clusters:
        for m in cluster.members:
            plan.source_versions[m.key] = m.updated_at.isoformat()

    logger.info(
        "[convergent-dream] planned: scanned=%d clusters=%s deferred=%d",
        plan.scanned, plan.counts(), plan.deferred_to_next_pass,
    )
    return plan


# ---------------------------------------------------------------------------
# Step 2b — diff inventory gate (spec §6.4)
# ---------------------------------------------------------------------------

_DIFF_SYSTEM = """\
You are Loom's memory consolidation reviewer running a "差異盤點" (difference
inventory) on a cluster of semantically-similar facts that are candidates for
merging into one.

Your job is NOT to decide whether they should merge. Your job is to surface,
for EACH fact, what it says that the OTHERS do not. Two facts may have high
embedding similarity yet be distinct insights (e.g. "user prefers concise
replies" is a preference; "user dislikes verbosity" is a complaint — they
overlap in words but are not the same fact).

Rule for mergeability:
  - If TWO OR MORE facts each carry content the others lack → NOT mergeable
    (they are distinct insights that merely look alike — keep them separate).
  - If only ONE fact carries unique content (the others are subsumed) →
    mergeable.

Return ONLY a JSON object with exactly these keys:
  "unique_by_key" : object mapping each fact's key → a short string describing
                    what that fact uniquely contributes ("" if nothing unique)
  "mergeable"     : boolean
  "rationale"     : one sentence explaining the verdict

Return ONLY the JSON object — no preamble, no markdown fences.
"""


async def diff_inventory(cluster: CandidateCluster, llm_fn: LLMFn) -> DiffInventory:
    """Run the LLM difference-inventory gate on a merge cluster (spec §6.4)."""
    facts_block = "\n".join(
        f'- key="{m.key}"  source="{m.source}"  value="{m.value}"'
        for m in cluster.members
    )
    messages = [
        {"role": "system", "content": _DIFF_SYSTEM},
        {"role": "user", "content": f"Facts in this cluster:\n{facts_block}\n\nReturn the JSON now."},
    ]
    try:
        raw = await llm_fn(messages)
    except Exception as exc:
        logger.warning("[convergent-dream] diff_inventory LLM failed: %s", exc)
        # Fail safe: if we cannot inventory differences, do NOT mark mergeable.
        return DiffInventory(mergeable=False, rationale=f"diff-inventory unavailable: {exc}")

    data = _parse_json_object(raw)
    if data is None:
        return DiffInventory(mergeable=False, rationale="diff-inventory output unparseable")

    # unique_by_key must be an object; a list/string/None would crash .items()
    # — fail safe rather than raise (Codex re-review #493).
    raw_ubk = data.get("unique_by_key")
    if not isinstance(raw_ubk, dict):
        return DiffInventory(
            mergeable=False,
            rationale="diff-inventory unique_by_key was not an object — failing safe",
        )
    unique_by_key = {str(k): str(v) for k, v in raw_ubk.items()}
    # Only an explicit JSON boolean true counts. bool("false") is True, so a
    # stringified verdict must NOT slip through — anything other than real
    # `true` fails safe to not-mergeable (Codex re-review #493).
    mergeable = data.get("mergeable") is True
    rationale = str(data.get("rationale", ""))

    # Fail safe when the inventory shape does not match the cluster (Codex
    # review #493): a response that omits a member — or invents a key that
    # isn't in the cluster — cannot be trusted to claim mergeable. The gate
    # must COVER exactly the cluster members before we honour mergeable=True.
    expected = set(cluster.member_keys)
    if expected and set(unique_by_key) != expected:
        return DiffInventory(
            unique_by_key=unique_by_key,
            mergeable=False,
            rationale=(
                f"diff-inventory keys {sorted(unique_by_key)} did not cover "
                f"cluster members {sorted(expected)} — failing safe"
            ),
        )

    return DiffInventory(unique_by_key=unique_by_key, mergeable=mergeable, rationale=rationale)


# ---------------------------------------------------------------------------
# Step 3 — self review ("說夢話", spec §6)
# ---------------------------------------------------------------------------

_SELF_REVIEW_SYSTEM = """\
You are Loom (絲絲), reviewing how YOUR OWN memory is about to be consolidated.
This is an offline self-review — you are not the object being tidied, you are
the subject protecting the integrity of your own memory while you sleep.

You have STRONG VETO power. Your verdict is a hard boundary, not a suggestion.
For each proposed cluster, return one of:
  - "approve" : safe to act on this cluster
  - "skip"    : do NOT act on this cluster this pass (a real problem with it)
  - "defer"   : not now — re-evaluate in a future pass (uncertain / still fermenting)

Be conservative — prefer skip/defer — when a cluster:
  - touches a user_explicit fact (the user told this to you directly),
  - would flatten wording that itself carries the insight (not a droppable detail),
  - involves two facts that each have unique content (per the diff inventory),
  - or feels like it is still "fermenting" (a recent fact whose deeper
    connections have not surfaced yet).

Return ONLY a JSON array; one object per cluster, each with:
  "cluster_id" : string (echo the id you were given)
  "verdict"    : "approve" | "skip" | "defer"
  "reason"     : one sentence

Return ONLY the JSON array — no preamble, no markdown fences.
"""


def _render_cluster_for_review(cluster: CandidateCluster) -> str:
    lines = [f'cluster_id="{cluster.cluster_id}"  kind={cluster.kind}  similarity={cluster.similarity:.2f}']
    for m in cluster.members:
        tier, _ = classify_source(m.source)
        lines.append(
            f'  - key="{m.key}"  trust={tier}  confidence={m.confidence:.2f}\n'
            f'    value="{m.value}"'
        )
    if cluster.diff is not None:
        lines.append(f"  diff_inventory: mergeable={cluster.diff.mergeable} — {cluster.diff.rationale}")
        for k, v in cluster.diff.unique_by_key.items():
            if v:
                lines.append(f'    unique to "{k}": {v}')
    lines.append(f"  proposed_action: {cluster.proposed_action}")
    return "\n".join(lines)


async def self_review(plan: ConsolidationPlan, llm_fn: LLMFn) -> list[ReviewDecision]:
    """絲絲 reviews each cluster offline and returns hard-boundary verdicts.

    Clusters whose diff-inventory already says ``mergeable=False`` are auto-
    skipped without consulting the LLM (the gate already vetoed them, spec §6.4).
    """
    decisions: list[ReviewDecision] = []
    review_clusters: list[CandidateCluster] = []

    for cluster in plan.clusters:
        if cluster.kind == KIND_MERGE and cluster.diff is not None and not cluster.diff.mergeable:
            decisions.append(ReviewDecision(
                cluster_id=cluster.cluster_id,
                verdict=VERDICT_SKIP,
                reason=f"diff-inventory: not mergeable — {cluster.diff.rationale}",
            ))
        else:
            review_clusters.append(cluster)

    if not review_clusters:
        return decisions

    block = "\n\n".join(_render_cluster_for_review(c) for c in review_clusters)
    messages = [
        {"role": "system", "content": _SELF_REVIEW_SYSTEM},
        {"role": "user", "content": f"Clusters to review:\n\n{block}\n\nReturn the JSON array now."},
    ]
    try:
        raw = await llm_fn(messages)
    except Exception as exc:
        logger.warning("[convergent-dream] self_review LLM failed: %s", exc)
        # Fail safe: if absent self-review, defer everything (never auto-approve).
        for c in review_clusters:
            decisions.append(ReviewDecision(
                cluster_id=c.cluster_id, verdict=VERDICT_DEFER,
                reason=f"self-review unavailable — deferred: {exc}",
            ))
        return decisions

    parsed = _parse_json_array(raw)
    id_counts = Counter(str(d.get("cluster_id")) for d in parsed if isinstance(d, dict))
    by_id = {str(d.get("cluster_id")): d for d in parsed if isinstance(d, dict)}
    for c in review_clusters:
        # A repeated id is malformed output (Codex review #493). Never let a
        # later verdict silently overwrite an earlier one — last-wins would
        # let a bogus "approve" bury a real "skip"/"defer" and weaken the
        # strong-veto contract. Ambiguous → defer.
        if id_counts.get(c.cluster_id, 0) > 1:
            decisions.append(ReviewDecision(
                cluster_id=c.cluster_id, verdict=VERDICT_DEFER,
                reason="duplicate cluster_id in self-review output — deferred",
            ))
            continue
        d = by_id.get(c.cluster_id)
        verdict = str(d.get("verdict", "")).lower() if d else ""
        if verdict not in (VERDICT_APPROVE, VERDICT_SKIP, VERDICT_DEFER):
            # Missing or malformed verdict → defer (never silently approve).
            decisions.append(ReviewDecision(
                cluster_id=c.cluster_id, verdict=VERDICT_DEFER,
                reason="no valid verdict returned — deferred",
            ))
        else:
            decisions.append(ReviewDecision(
                cluster_id=c.cluster_id, verdict=verdict,
                reason=str(d.get("reason", "")) if d else "",
            ))
    return decisions


# ---------------------------------------------------------------------------
# Step 5 — report rendering (spec §8)
# ---------------------------------------------------------------------------

_VERDICT_ICON = {VERDICT_APPROVE: "✅", VERDICT_SKIP: "⚠️", VERDICT_DEFER: "💤"}


def render_report(plan: ConsolidationPlan) -> str:
    """Render the convergent-dream pass as a markdown body (spec §8).

    This is the body of a ``夢境鞏固`` journal entry — narrative enough to read
    back, but every line maps to a concrete cluster + verdict so it can be
    audited against the (future) executed state.
    """
    counts = plan.counts()
    out: list[str] = [
        f"pass_id: `{plan.pass_id}` · status: {plan.execution_status} (read-only / P1)",
        "",
        "### 本輪 pass 摘要",
        f"- 掃描範圍：{plan.scanned} 筆 semantic facts",
        f"- 發現：{counts[KIND_MERGE]} 個 merge 簇、{counts[KIND_RECONCILE]} 個矛盾、{counts[KIND_CLEAN]} 個 clean 候選",
    ]
    if plan.deferred_to_next_pass:
        out.append(f"- ⏭️ 因批次上限，{plan.deferred_to_next_pass} 個簇順延下輪")

    def _section(title: str, kind: str) -> None:
        clusters = [c for c in plan.clusters if c.kind == kind]
        if not clusters:
            return
        out.append("")
        out.append(f"### {title}")
        for c in clusters:
            d = plan.decision_for(c.cluster_id)
            icon = _VERDICT_ICON.get(d.verdict, "·") if d else "·"
            verdict = d.verdict if d else "(未審)"
            keys = "、".join(f"`{k}`" for k in c.member_keys)
            out.append(f"- {icon} **{verdict}** — {keys}")
            if d and d.reason:
                out.append(f"  - 理由：{d.reason}")
            if c.diff is not None and c.diff.rationale:
                out.append(f"  - 差異盤點：{c.diff.rationale}")

    _section("Merge 記錄", KIND_MERGE)
    _section("Reconcile 記錄", KIND_RECONCILE)
    _section("Clean 記錄", KIND_CLEAN)

    if plan.notes:
        out.append("")
        out.append("### 備註")
        for n in plan.notes:
            out.append(f"- {n}")

    out.append("")
    out.append("_P1 read-only：本輪僅規劃與自審，未改寫任何記憶。執行待 P2 (#489)。_")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Orchestrator — index → plan → self_review → report (READ-ONLY)
# ---------------------------------------------------------------------------

async def run_convergent_dream(
    semantic: "SemanticMemory",
    llm_fn: LLMFn,
    *,
    corpus_limit: int = 2000,
    min_similarity: float = 0.85,
    max_clusters: int = 20,
) -> tuple[ConsolidationPlan, str]:
    """Run one read-only convergent-dream pass.

    Returns ``(plan, report_markdown)``. Never writes to the DB: no
    ``consolidate`` / ``upsert`` / ``delete`` is reachable from here (P1).
    """
    plan = await build_plan(
        semantic,
        corpus_limit=corpus_limit,
        min_similarity=min_similarity,
        max_clusters=max_clusters,
    )

    # diff-inventory gate on merge clusters only (spec §6.4)
    for cluster in plan.clusters:
        if cluster.kind == KIND_MERGE:
            cluster.diff = await diff_inventory(cluster, llm_fn)

    plan.decisions = await self_review(plan, llm_fn)
    report = render_report(plan)
    return plan, report


# ---------------------------------------------------------------------------
# Defensive JSON parsing (mirrors dreaming._parse_triples tolerance)
# ---------------------------------------------------------------------------

def _strip_fences(raw: str) -> str:
    return re.sub(r"```(?:json)?\s*", "", raw.strip()).strip().rstrip("`").strip()


def _parse_json_object(raw: str) -> dict | None:
    cleaned = _strip_fences(raw)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
        return None


def _parse_json_array(raw: str) -> list:
    cleaned = _strip_fences(raw)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
                return data if isinstance(data, list) else []
            except json.JSONDecodeError:
                return []
        return []
