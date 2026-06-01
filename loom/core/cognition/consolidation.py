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


def _is_stub(entry: SemanticEntry) -> bool:
    """True if the entry is a consolidation redirect stub (#490 P3)."""
    return bool(entry.metadata.get("redirected_to"))


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
            if _is_dreaming(entry) or _is_stub(entry):
                continue  # §6.5 dreaming exempt; stubs aren't real facts (#490)
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
                if _is_dreaming(neighbour) or _is_stub(neighbour) or neighbour.key not in by_key:
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
        if _is_dreaming(entry) or _is_stub(entry):
            continue
        try:
            contradictions = await detector.detect(entry)
        except Exception as exc:
            plan.notes.append(f"contradiction scan failed for {entry.key!r}: {exc}")
            continue
        for c in contradictions:
            if _is_dreaming(c.existing) or _is_stub(c.existing):
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

    # ── Clean candidates: dangling redirect stubs (#490 P3) ──────────────
    # A stub whose terminal survivor is gone (resolve_redirect → None) is a
    # dangling orphan. dreaming-sourced entries are allowed orphans (絲絲's
    # principle — never cleaned). Live stubs (target still resolves) are kept.
    for entry in corpus:
        if not _is_stub(entry) or _is_dreaming(entry):
            continue
        if await semantic.resolve_redirect(entry.key) is None:
            plan.clusters.append(CandidateCluster(
                cluster_id=f"clean-{uuid.uuid4().hex[:8]}",
                kind=KIND_CLEAN,
                members=[entry],
                proposed_action="delete dangling redirect stub (terminal survivor gone)",
            ))

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


def render_report(plan: ConsolidationPlan, execute_result: "ExecuteResult | None" = None) -> str:
    """Render the convergent-dream pass as a markdown body (spec §8).

    This is the body of a ``夢境鞏固`` journal entry — narrative enough to read
    back, but every line maps to a concrete cluster + verdict so it can be
    audited against the executed state. When ``execute_result`` is given
    (P2 execute=True), an execution-outcome section is appended.
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

    if execute_result is None:
        out.append("")
        out.append("_read-only：本輪僅規劃與自審，未改寫任何記憶。_")
    else:
        out.append("")
        out.append("### 執行結果")
        out.append(f"- ✅ 已合併：{len(execute_result.executed)} 簇 "
                   f"(survivors: {'、'.join(f'`{k}`' for k in execute_result.executed) or '—'})")
        if execute_result.cleaned:
            out.append(f"- 🧹 已清理孤兒 stub：{len(execute_result.cleaned)}")
        if execute_result.skipped_stale:
            out.append(f"- ⏭️ stale 跳過：{len(execute_result.skipped_stale)}（來源在自審後已變動）")
        if execute_result.skipped_other:
            out.append(f"- ⏸️ 非 merge 暫不執行（reconcile/clean → P3）：{len(execute_result.skipped_other)}")
        if execute_result.skipped_error:
            out.append(f"- ⚠️ 合成/執行失敗跳過：{len(execute_result.skipped_error)}")
        for n in execute_result.notes:
            out.append(f"  - {n}")
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
    execute: bool = False,
) -> tuple[ConsolidationPlan, str]:
    """Run one convergent-dream pass.

    Returns ``(plan, report_markdown)``. With ``execute=False`` (default) this
    is strictly read-only: index → plan → diff-gate → self-review → report, no
    write path reachable. With ``execute=True`` (P2 #489) the approved,
    non-stale merge clusters are then consolidated via ``execute_plan`` and the
    report gains an execution-outcome section.
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

    execute_result = None
    if execute:
        execute_result = await execute_plan(semantic, plan, llm_fn)

    report = render_report(plan, execute_result=execute_result)
    return plan, report


# ---------------------------------------------------------------------------
# Step 4 — synthesize + execute (P2 #489)
# ---------------------------------------------------------------------------

@dataclass
class MergeSynthesis:
    """The fused result for one approved merge cluster (P2)."""
    refined_value: str
    survivor_key: str
    sacrificed_content: str       # verbatim originals — built in code, not the LLM
    merge_rationale: str


@dataclass
class ExecuteResult:
    """Outcome of executing an approved, non-stale plan (P2)."""
    executed: list[str] = field(default_factory=list)        # survivor keys consolidated
    cleaned: list[str] = field(default_factory=list)         # dangling stubs deleted
    skipped_stale: list[str] = field(default_factory=list)   # source changed after snapshot
    skipped_verdict: list[str] = field(default_factory=list) # not approved
    skipped_other: list[str] = field(default_factory=list)   # reconcile/clean → P3
    skipped_error: list[str] = field(default_factory=list)   # synthesis/consolidate failed
    notes: list[str] = field(default_factory=list)


_SYNTH_SYSTEM = """\
You are Loom's memory consolidation synthesizer. You are given a cluster of
near-duplicate facts that has ALREADY been approved for merging. One fact is
marked as the SURVIVOR — it is the highest-trust source (e.g. the user told it
to us directly). Your job is to write the single refined fact they fuse into.

Rules:
  - Anchor on the SURVIVOR's wording and stance. Lower-trust facts may only
    ADD detail the survivor lacks — they must not override it.
  - Do not invent anything not present in the cluster.
  - Keep it one tight fact, not a summary of "these facts say...".

Return ONLY a JSON object with exactly these keys:
  "refined_value" : the single fused fact (string)
  "rationale"     : one sentence on why they are the same fact (string)

Return ONLY the JSON object — no preamble, no markdown fences.
"""


def _trust_of(entry: SemanticEntry) -> float:
    return classify_source(entry.source)[1]


async def synthesize_merge(cluster: CandidateCluster, llm_fn: LLMFn) -> MergeSynthesis | None:
    """Pick the survivor (deterministic, trust-aware) and fuse the cluster.

    Survivor selection is **not** delegated to the LLM: highest trust tier
    wins, ties break to the earliest ``created_at`` (the most established
    statement). ``sacrificed_content`` is assembled verbatim in code so the
    full originals are preserved regardless of what the LLM returns (spec §5.4
    + 絲絲's no-flatten requirement). The LLM only writes the refined value.

    Returns None when the cluster is malformed or the LLM cannot synthesize a
    non-empty refined value (caller skips — never a silent bad merge).
    """
    members = cluster.members
    if len(members) < 2:
        return None

    # highest trust, then earliest created_at
    survivor = max(members, key=lambda e: (_trust_of(e), -e.created_at.timestamp()))
    sacrificed = [e for e in members if e.key != survivor.key]
    sacrificed_content = "\n\n".join(f"[{e.key}] {e.value}" for e in sacrificed)

    fact_lines = []
    for e in members:
        tier, _ = classify_source(e.source)
        marker = " ← SURVIVOR (anchor)" if e.key == survivor.key else ""
        fact_lines.append(f'- key="{e.key}"  trust={tier}{marker}\n  value="{e.value}"')
    messages = [
        {"role": "system", "content": _SYNTH_SYSTEM},
        {"role": "user", "content": "Cluster:\n" + "\n".join(fact_lines) + "\n\nReturn the JSON now."},
    ]

    try:
        raw = await llm_fn(messages)
    except Exception as exc:
        logger.warning("[convergent-dream] synthesize_merge LLM failed: %s", exc)
        return None

    data = _parse_json_object(raw)
    if data is None:
        return None
    refined = str(data.get("refined_value", "")).strip()
    if not refined:
        return None
    rationale = str(data.get("rationale", "")).strip() or "(no rationale provided)"

    return MergeSynthesis(
        refined_value=refined,
        survivor_key=survivor.key,
        sacrificed_content=sacrificed_content,
        merge_rationale=rationale,
    )


_RECONCILE_CLASSIFY_SYSTEM = """\
Two facts were flagged as contradicting. Decide which kind of conflict this is:
  - "merge": they are actually the SAME fact in different words — no real
    conflict, they should be fused into one.
  - "arbitrate": they are genuinely OPPOSING (one says something the other
    denies) — the more trustworthy / more recent one should win and the other
    becomes a superseded belief.

Do NOT guess. If you cannot tell, that is itself important — say so.

Return ONLY a JSON object: {"kind": "merge" | "arbitrate", "reason": "..."}.
Return ONLY the JSON object — no preamble, no markdown fences.
"""


async def classify_reconcile(cluster: CandidateCluster, llm_fn: LLMFn) -> str:
    """Classify a reconcile cluster as "merge", "arbitrate", or "skip" (P3).

    "skip" is the fail-safe: an unparseable / failed / unrecognised verdict
    never resolves a contradiction blindly.
    """
    fact_lines = []
    for e in cluster.members:
        tier, _ = classify_source(e.source)
        fact_lines.append(f'- key="{e.key}"  trust={tier}\n  value="{e.value}"')
    messages = [
        {"role": "system", "content": _RECONCILE_CLASSIFY_SYSTEM},
        {"role": "user", "content": "The two facts:\n" + "\n".join(fact_lines) + "\n\nReturn the JSON now."},
    ]
    try:
        raw = await llm_fn(messages)
    except Exception as exc:
        logger.warning("[convergent-dream] classify_reconcile LLM failed: %s", exc)
        return "skip"
    data = _parse_json_object(raw)
    if data is None:
        return "skip"
    kind = str(data.get("kind", "")).strip().lower()
    return kind if kind in ("merge", "arbitrate") else "skip"


def _arbitrate(members: list[SemanticEntry]) -> tuple[SemanticEntry, list[SemanticEntry], str]:
    """Pick the winner of an opposing contradiction: higher trust, tie → newer.

    Returns (winner, losers, reason). Mirrors ContradictionDetector.resolve's
    trust-then-recency order, but recency is by actual updated_at (batch
    context) rather than write-time "proposed is newer".
    """
    winner = max(members, key=lambda e: (_trust_of(e), e.updated_at.timestamp()))
    losers = [e for e in members if e.key != winner.key]
    wt, _ = classify_source(winner.source)
    reason = (
        f"{winner.key!r} (trust={wt}) wins over "
        f"{[l.key for l in losers]} on trust then recency"
    )
    return winner, losers, reason


async def execute_plan(
    semantic: "SemanticMemory",
    plan: ConsolidationPlan,
    llm_fn: LLMFn,
) -> ExecuteResult:
    """Execute the approved, non-stale merge clusters of a plan (P2 #489).

    Boundaries (all enforced, none silent):
      - only ``approve`` verdicts run — ``skip``/``defer``/unreviewed are left;
      - only ``merge`` clusters run — reconcile/clean execution is P3 (#490);
      - a cluster whose any member changed since the plan's ``source_versions``
        snapshot is **stale** and is skipped (acts on a stale plan otherwise);
      - synthesis/consolidate failure skips that cluster, never half-writes.

    Mutates the DB via ``semantic.consolidate`` for the clusters that pass all
    gates. Sets ``plan.execution_status = "executed"``.
    """
    result = ExecuteResult()

    for cluster in plan.clusters:
        decision = plan.decision_for(cluster.cluster_id)
        if decision is None or decision.verdict != VERDICT_APPROVE:
            result.skipped_verdict.append(cluster.cluster_id)
            continue
        if cluster.kind not in (KIND_MERGE, KIND_RECONCILE, KIND_CLEAN):
            result.skipped_other.append(cluster.cluster_id)
            continue

        # stale check — source must be byte-identical to the planned snapshot
        stale = False
        for m in cluster.members:
            current = await semantic.get(m.key)
            snap = plan.source_versions.get(m.key)
            if current is None or snap is None or current.updated_at.isoformat() != snap:
                stale = True
                break
        if stale:
            result.skipped_stale.append(cluster.cluster_id)
            continue

        # Clean arm — delete the dangling orphan stub(s). Re-validate orphan
        # status at execute time (Codex #496 — TOCTTOU): the stub's own row is
        # unchanged so the stale check passes, but the survivor may have been
        # restored after planning. If the key resolves again it is a live
        # fallback now — never delete it.
        if cluster.kind == KIND_CLEAN:
            for m in cluster.members:
                if await semantic.resolve_redirect(m.key) is not None:
                    result.notes.append(
                        f"{cluster.cluster_id}: {m.key} resolves again at execute — kept")
                    continue
                if await semantic.delete(m.key):
                    result.cleaned.append(m.key)
            continue

        # Resolve the cluster into a (entries, refined_value, survivor_key,
        # sacrificed_content, rationale) consolidate() call. Merge clusters and
        # the merge arm of reconcile both fuse via synthesize_merge; the
        # arbitrate arm keeps the trust/recency winner's value verbatim.
        if cluster.kind == KIND_MERGE:
            mode = "merge"
        else:  # KIND_RECONCILE — classify same-fact (merge) vs opposing (arbitrate)
            mode = await classify_reconcile(cluster, llm_fn)
            if mode not in ("merge", "arbitrate"):
                result.skipped_error.append(cluster.cluster_id)
                result.notes.append(f"{cluster.cluster_id}: reconcile classify → skip")
                continue

        if mode == "merge":
            syn = await synthesize_merge(cluster, llm_fn)
            if syn is None:
                result.skipped_error.append(cluster.cluster_id)
                result.notes.append(f"{cluster.cluster_id}: synthesis failed — skipped")
                continue
            entries, refined, survivor = cluster.members, syn.refined_value, syn.survivor_key
            sacrificed_content, rationale = syn.sacrificed_content, syn.merge_rationale
        else:  # arbitrate
            winner, losers, reason = _arbitrate(cluster.members)
            entries, refined, survivor = cluster.members, winner.value, winner.key
            sacrificed_content = "\n\n".join(f"[{l.key}] {l.value}" for l in losers)
            rationale = f"superseded (opposing): {reason}"

        try:
            res = await semantic.consolidate(
                entries,
                refined_value=refined,
                survivor_key=survivor,
                sacrificed_content=sacrificed_content,
                merge_rationale=rationale,
            )
        except Exception as exc:
            result.skipped_error.append(cluster.cluster_id)
            result.notes.append(f"{cluster.cluster_id}: consolidate failed: {exc}")
            continue

        result.executed.append(res.survivor_key)

    plan.execution_status = "executed"
    logger.info(
        "[convergent-dream] executed=%d stale=%d verdict-skip=%d other=%d error=%d",
        len(result.executed), len(result.skipped_stale), len(result.skipped_verdict),
        len(result.skipped_other), len(result.skipped_error),
    )
    return result


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
