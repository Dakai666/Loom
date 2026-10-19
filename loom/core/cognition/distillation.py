"""
Distill dream — refine a topic neighbourhood into one fact (#613 E, P1).

Spec: ``docs/designs/57-記憶蒸餾夢-規格.md``. The convergent dream merges
near-duplicates (cos ≥ 0.85); the distill dream handles the band below it —
facts on the same topic written as separate facets (cos ≈ 0.70–0.85 on the
stored vectors, calibrated 2026-10-19) — and replaces several machine-written
sources with one refined fact.

One pass:

1. **seed** — non-dream machine facts (never hand-written, milestone, stub,
   archived or recently distilled), densest neighbourhood first;
2. **neighbourhood** — the seed plus up to ``max_members - 1`` stored-vector
   neighbours at ≥ ``min_similarity``, disjoint across the pass. Hand-written
   / milestone members ride along as *context* only;
3. **distill** (LLM) — ``distilled`` + ``covers`` (indices it replaces) +
   ``dropped`` (deliberately not kept, incl. claims a hand-written fact
   already carries);
4. **hard boundaries in code** — covers ⊆ neighbourhood, no protected fact,
   ≥ 2 sources, a non-dream survivor exists;
5. **gate** (LLM) — every covered source is "covered" or "dropped", none
   "lost", and no hand-written claim is restated; untrustworthy output is
   retried once, then deferred (never approved);
6. **self-review** — Loom's veto, via consolidation's batched reviewer.

P1 is read-only: it plans and reports. Execution (``consolidate`` + stale
check + backup) is P2.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np

from loom.core.cognition.consolidation import (
    LLMFn,
    VERDICT_APPROVE,
    VERDICT_DEFER,
    ReviewDecision,
    _is_dreaming,
    _is_stub,
    _parse_json_object,
    _review_batch,
    _trust_of,
)
from loom.core.memory.clustering import stack_normalised
from loom.core.memory.ontology import TEMPORAL_ARCHIVED, TEMPORAL_MILESTONE
from loom.core.memory.semantic import SemanticEntry, classify_source

if TYPE_CHECKING:
    from loom.core.memory.semantic import SemanticMemory

logger = logging.getLogger(__name__)

#: Written into the survivor's metadata on execute (P2) so §7 can query
#: distill products without changing ``source`` semantics.
PRODUCED_BY = "distill_dream"

#: Stored-vector cosine floor for a neighbour (spec §3, calibrated on stored
#: vs stored, 2026-10-19). Re-calibrate every 4 weeks / on a model change.
DISTILL_MIN_SIMILARITY = 0.70
DEFAULT_MAX_NEIGHBOURHOODS = 5
DEFAULT_MAX_MEMBERS = 12
MIN_MEMBERS = 3
#: A survivor distilled more recently than this is not re-seeded.
RECENT_DISTILL_DAYS = 30

#: Tiers that are written deliberately and stay authoritative (spec §5).
PROTECTED_TIERS = frozenset({"user_explicit", "agent_memorize", "tool_verified"})

STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"   # a boundary, the gate or the veto said no
STATUS_DEFERRED = "deferred"   # no trustworthy verdict — retry another pass
STATUS_FAILED = "failed"       # the distill call itself failed


def planned_survivor_metadata(*, now: datetime | None = None) -> dict[str, str]:
    """Metadata the survivor receives on execute (fixed in P1, spec §4)."""
    return {
        "produced_by": PRODUCED_BY,
        "distilled_at": (now or datetime.now(UTC)).isoformat(),
    }


def is_protected(entry: SemanticEntry) -> bool:
    """Hand-written / milestone facts: context only, never replaced (spec §5)."""
    return (
        classify_source(entry.source)[0] in PROTECTED_TIERS
        or entry.temporal == TEMPORAL_MILESTONE
    )


def _recently_distilled(entry: SemanticEntry, now: datetime) -> bool:
    ts = entry.metadata.get("distilled_at") if isinstance(entry.metadata, dict) else None
    if not ts:
        return False
    try:
        return now - datetime.fromisoformat(ts) < timedelta(days=RECENT_DISTILL_DAYS)
    except (TypeError, ValueError):
        return False


def _seedable(entry: SemanticEntry, domain: str | None, now: datetime) -> bool:
    return (
        not _is_stub(entry)
        and not _is_dreaming(entry)
        and not is_protected(entry)
        and entry.temporal != TEMPORAL_ARCHIVED
        and (domain is None or entry.domain == domain)
        and not _recently_distilled(entry, now)
    )


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Neighbourhood:
    cluster_id: str                      # named for _review_batch reuse
    seed_key: str
    members: list[SemanticEntry]         # seed first, then by similarity
    protected_keys: set[str] = field(default_factory=set)


@dataclass
class Distillation:
    distilled: str
    covers: list[str]                    # keys the distilled fact replaces
    dropped: list[dict]                  # {"text", "reason", "carried_by"?}
    rationale: str = ""


@dataclass
class DistillResult:
    neighbourhood_id: str
    status: str
    reason: str = ""
    distillation: Distillation | None = None
    survivor_key: str | None = None
    gate_rationale: str = ""


@dataclass
class DistillPlan:
    pass_id: str = field(default_factory=lambda: f"distill-{uuid.uuid4().hex[:12]}")
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    domain: str | None = None
    scanned: int = 0
    neighbourhoods: list[Neighbourhood] = field(default_factory=list)
    results: list[DistillResult] = field(default_factory=list)
    #: key → updated_at, for P2's stale check
    source_versions: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c = {STATUS_APPROVED: 0, STATUS_REJECTED: 0, STATUS_DEFERRED: 0, STATUS_FAILED: 0}
        for r in self.results:
            c[r.status] = c.get(r.status, 0) + 1
        return c


# ---------------------------------------------------------------------------
# Step 1–2: seeds and neighbourhoods
# ---------------------------------------------------------------------------

async def build_distill_plan(
    semantic: "SemanticMemory",
    *,
    domain: str | None = None,
    max_neighbourhoods: int = DEFAULT_MAX_NEIGHBOURHOODS,
    corpus_limit: int = 15000,
    min_similarity: float = DISTILL_MIN_SIMILARITY,
    max_members: int = DEFAULT_MAX_MEMBERS,
    now: datetime | None = None,
) -> DistillPlan:
    """Pick disjoint topic neighbourhoods, densest seeds first. Read-only."""
    now = now or datetime.now(UTC)
    plan = DistillPlan(domain=domain)
    rows = await semantic.list_recent_raw_embeddings(corpus_limit)
    live = [(e, raw) for e, raw in rows if not _is_stub(e)]
    plan.scanned = len(live)
    matrix, kept = stack_normalised([raw for _, raw in live])
    if not kept:
        plan.notes.append("no stored embeddings — nothing to distill")
        return plan
    entries = [live[i][0] for i in kept]
    seedable = np.asarray([_seedable(e, domain, now) for e in entries])
    if not seedable.any():
        return plan

    # Per seed candidate: neighbours ≥ min_similarity, nearest first.
    seed_rows = np.nonzero(seedable)[0]
    k = min(max_members - 1, len(entries) - 1)
    candidates: list[tuple[int, int, list[int]]] = []  # (count, row, neighbours)
    for start in range(0, len(seed_rows), 512):
        rows_i = seed_rows[start:start + 512]
        sims = matrix[rows_i] @ matrix.T
        sims[np.arange(len(rows_i)), rows_i] = -np.inf
        counts = (sims >= min_similarity).sum(axis=1)
        top = np.argsort(-sims, axis=1)[:, :k]
        for r, row in enumerate(rows_i):
            nbrs = [int(j) for j in top[r] if sims[r, j] >= min_similarity]
            if len(nbrs) + 1 >= MIN_MEMBERS:
                candidates.append((int(counts[r]), int(row), nbrs))
    candidates.sort(key=lambda c: (-c[0], c[1]))

    used: set[int] = set()
    for _, row, nbrs in candidates:
        if len(plan.neighbourhoods) >= max_neighbourhoods:
            break
        if row in used:
            continue
        members_rows = [row] + [j for j in nbrs if j not in used]
        if len(members_rows) < MIN_MEMBERS:
            continue
        used.update(members_rows)
        members = [entries[j] for j in members_rows]
        plan.neighbourhoods.append(Neighbourhood(
            cluster_id=f"nb-{uuid.uuid4().hex[:8]}",
            seed_key=entries[row].key,
            members=members,
            protected_keys={m.key for m in members if is_protected(m)},
        ))
        for m in members:
            plan.source_versions[m.key] = m.updated_at.isoformat()
    return plan


# ---------------------------------------------------------------------------
# Step 3: distill
# ---------------------------------------------------------------------------

_DISTILL_SYSTEM = """\
You are Loom, distilling your OWN memory while you sleep. Below is one topic
neighbourhood: facts that are about the same subject but were written
separately. Write ONE refined fact that replaces as many of the machine-written
facts as truly belong together.

Rules:
- "distilled": one clear, self-contained statement covering every still-valid
  claim of the facts you replace. Keep concrete details (names, paths, numbers,
  dates) — distil wording, never facts.
- "covers": the [index] numbers of the facts the distilled fact replaces. Only
  facts marked protected=no may be listed. Leave out facts that are about
  something else — replacing fewer is fine.
- Facts marked protected=yes are hand-written and authoritative. Never restate
  their claims in "distilled". If a fact you replace says the same as a
  protected fact, put that claim in "dropped" with "carried_by" = the protected
  fact's index.
- "dropped": claims from the replaced facts you deliberately do not keep
  (outdated, trivial, or carried by a protected fact), each
  {"text": ..., "reason": ..., "carried_by": index or null}.
- If nothing here should be merged, return "covers": [].

Return ONLY a JSON object:
{"distilled": "...", "covers": [..], "dropped": [..], "rationale": "one sentence"}
No preamble, no markdown fences.
"""


def _render_neighbourhood(nb: Neighbourhood) -> str:
    lines = []
    for i, m in enumerate(nb.members, start=1):
        tier, _ = classify_source(m.source)
        protected = "yes" if m.key in nb.protected_keys else "no"
        lines.append(
            f'[{i}] key="{m.key}"  trust={tier}  protected={protected}  '
            f'value="{m.value}"'
        )
    return "\n".join(lines)


async def _distill(nb: Neighbourhood, llm_fn: LLMFn) -> Distillation | None:
    messages = [
        {"role": "system", "content": _DISTILL_SYSTEM},
        {"role": "user", "content": f"Neighbourhood:\n{_render_neighbourhood(nb)}\n\nReturn the JSON now."},
    ]
    raw = await llm_fn(messages)  # exceptions → caller marks the result failed
    data = _parse_json_object(raw)
    if data is None:
        return None
    distilled = str(data.get("distilled", "")).strip()
    covers_raw = data.get("covers")
    if not isinstance(covers_raw, list):
        return None
    keys: list[str] = []
    for idx in covers_raw:
        try:
            i = int(idx)
        except (TypeError, ValueError):
            return None
        if not 1 <= i <= len(nb.members):
            return None                      # phantom index — untrustworthy
        key = nb.members[i - 1].key
        if key not in keys:
            keys.append(key)
    dropped: list[dict] = []
    for item in data.get("dropped") or []:
        if not isinstance(item, dict):
            continue
        entry = {"text": str(item.get("text", "")).strip(),
                 "reason": str(item.get("reason", "")).strip()}
        carried = item.get("carried_by")
        try:
            ci = int(carried) if carried is not None else None
        except (TypeError, ValueError):
            ci = None
        if ci is not None and 1 <= ci <= len(nb.members) \
                and nb.members[ci - 1].key in nb.protected_keys:
            entry["carried_by"] = nb.members[ci - 1].key
        if entry["text"]:
            dropped.append(entry)
    return Distillation(
        distilled=distilled, covers=keys, dropped=dropped,
        rationale=str(data.get("rationale", "")).strip(),
    )


def _boundary_violation(nb: Neighbourhood, d: Distillation) -> tuple[str | None, str | None]:
    """Code-enforced hard boundaries (spec §5). Returns (reason, survivor_key)."""
    if not d.distilled:
        return "empty distilled value", None
    protected = [k for k in d.covers if k in nb.protected_keys]
    if protected:
        return f"covers a protected (hand-written / milestone) fact: {protected}", None
    if len(d.covers) < 2:
        return "fewer than 2 sources to replace", None
    by_key = {m.key: m for m in nb.members}
    candidates = [by_key[k] for k in d.covers if not _is_dreaming(by_key[k])]
    if not candidates:
        return "no non-dream survivor among the covered facts", None
    survivor = max(candidates, key=lambda e: (_trust_of(e), -e.created_at.timestamp()))
    return None, survivor.key


# ---------------------------------------------------------------------------
# Step 5: gate
# ---------------------------------------------------------------------------

_DISTILL_GATE_SYSTEM = """\
You are checking a proposed memory distillation for information loss.

SOURCES are the facts that would be replaced. For each source answer:
  "covered" — everything it says that is still valid appears in DISTILLED,
  "dropped" — what is missing is listed under DROPPED (deliberately not kept),
  "lost"    — it says something that is neither in DISTILLED nor in DROPPED.
HAND-WRITTEN facts stay as they are. List the [H..] ids of any whose claim
DISTILLED restates (that would duplicate an authoritative fact).

Return ONLY a JSON object:
{"sources": {"1": "covered"|"dropped"|"lost", ...},
 "handwritten_restated": ["H1", ...], "rationale": "one sentence"}
Cover every source number exactly once. No preamble, no markdown fences.
"""

_GATE_VERDICTS = {"covered", "dropped", "lost"}


@dataclass
class _GateResult:
    ok: bool
    trustworthy: bool
    rationale: str


async def _gate_attempt(nb: Neighbourhood, d: Distillation, llm_fn: LLMFn) -> _GateResult:
    by_key = {m.key: m for m in nb.members}
    sources = [by_key[k] for k in d.covers]
    hand = [m for m in nb.members if m.key in nb.protected_keys]
    lines = ["SOURCES:"]
    lines += [f'[S{i}] value="{s.value}"' for i, s in enumerate(sources, start=1)]
    if hand:
        lines.append("HAND-WRITTEN:")
        lines += [f'[H{i}] value="{h.value}"' for i, h in enumerate(hand, start=1)]
    lines.append(f'DISTILLED: "{d.distilled}"')
    lines.append("DROPPED:")
    lines += [f'- {x["text"]} ({x["reason"]})' for x in d.dropped] or ["- (none)"]
    messages = [
        {"role": "system", "content": _DISTILL_GATE_SYSTEM},
        {"role": "user", "content": "\n".join(lines) + "\n\nReturn the JSON now."},
    ]
    try:
        raw = await llm_fn(messages)
    except Exception as exc:
        return _GateResult(False, False, f"gate unavailable: {exc}")
    data = _parse_json_object(raw)
    if data is None or not isinstance(data.get("sources"), dict):
        return _GateResult(False, False, "gate output unparseable")
    verdicts = {str(k).strip(): str(v).strip().lower() for k, v in data["sources"].items()}
    if set(verdicts) != {str(i) for i in range(1, len(sources) + 1)} \
            or not set(verdicts.values()) <= _GATE_VERDICTS:
        return _GateResult(False, False, "gate did not cover every source exactly")
    restated = data.get("handwritten_restated") or []
    if not isinstance(restated, list):
        return _GateResult(False, False, "gate handwritten_restated malformed")
    rationale = str(data.get("rationale", "")).strip()
    lost = sorted(k for k, v in verdicts.items() if v == "lost")
    if lost:
        return _GateResult(False, True, f"sources {lost} would lose information — {rationale}")
    if restated:
        return _GateResult(False, True, f"restates hand-written fact(s) {restated} — {rationale}")
    return _GateResult(True, True, rationale)


async def _gate(nb: Neighbourhood, d: Distillation, llm_fn: LLMFn) -> _GateResult:
    """One retry for untrustworthy output (mirrors diff_inventory, #554)."""
    result = await _gate_attempt(nb, d, llm_fn)
    if result.trustworthy:
        return result
    return await _gate_attempt(nb, d, llm_fn)


# ---------------------------------------------------------------------------
# Step 6: self-review
# ---------------------------------------------------------------------------

_DISTILL_REVIEW_SYSTEM = """\
You are Loom (絲絲), reviewing how YOUR OWN memory is about to be distilled
while you sleep. Several machine-written facts on one topic would be replaced
by a single refined fact; the originals stay recoverable in its metadata.

You have STRONG VETO power. For each neighbourhood return one of:
  - "approve" : the distilled fact is a faithful, better version of its sources
  - "skip"    : do NOT replace them (lossy, wrong grouping, flattened insight)
  - "defer"   : not now — uncertain, or the topic is still forming

Be conservative when the distilled fact blurs details, joins facts that are
really about different things, or restates a hand-written fact.

Return ONLY a JSON array; one object per neighbourhood:
  {"cluster_id": "<neighbourhood_id>", "verdict": "approve"|"skip"|"defer",
   "reason": "one sentence"}
No preamble, no markdown fences.
"""


@dataclass
class _ReviewItem:
    cluster_id: str
    nb: Neighbourhood
    result: DistillResult


def _render_for_review(item: object) -> str:
    assert isinstance(item, _ReviewItem)
    nb, d = item.nb, item.result.distillation
    assert d is not None
    by_key = {m.key: m for m in nb.members}
    lines = [f'neighbourhood_id="{nb.cluster_id}"  seed="{nb.seed_key}"',
             "  would replace:"]
    for k in d.covers:
        tier, _ = classify_source(by_key[k].source)
        lines.append(f'    - key="{k}"  trust={tier}  value="{by_key[k].value}"')
    hand = [m for m in nb.members if m.key in nb.protected_keys]
    if hand:
        lines.append("  hand-written context (kept as is):")
        lines += [f'    - key="{h.key}"  value="{h.value}"' for h in hand]
    lines.append(f'  distilled: "{d.distilled}"')
    for x in d.dropped:
        carried = f" — carried by {x['carried_by']}" if x.get("carried_by") else ""
        lines.append(f"  dropped: {x['text']} ({x['reason']}){carried}")
    lines.append(f"  gate: {item.result.gate_rationale}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration + report
# ---------------------------------------------------------------------------

async def run_distill_dream(
    semantic: "SemanticMemory",
    llm_fn: LLMFn,
    *,
    domain: str | None = None,
    max_neighbourhoods: int = DEFAULT_MAX_NEIGHBOURHOODS,
    corpus_limit: int = 15000,
    min_similarity: float = DISTILL_MIN_SIMILARITY,
    review_batch_size: int = 10,
) -> tuple[DistillPlan, str]:
    """One read-only distill pass (P1). Returns ``(plan, report_markdown)``."""
    plan = await build_distill_plan(
        semantic, domain=domain, max_neighbourhoods=max_neighbourhoods,
        corpus_limit=corpus_limit, min_similarity=min_similarity,
    )
    to_review: list[_ReviewItem] = []
    for nb in plan.neighbourhoods:
        try:
            d = await _distill(nb, llm_fn)
        except Exception as exc:
            logger.warning("[distill-dream] distill failed for %s: %s", nb.cluster_id, exc)
            plan.results.append(DistillResult(nb.cluster_id, STATUS_FAILED, f"distill call failed: {exc}"))
            continue
        if d is None:
            plan.results.append(DistillResult(nb.cluster_id, STATUS_FAILED, "distill output unparseable"))
            continue
        if not d.covers:
            plan.results.append(DistillResult(nb.cluster_id, STATUS_REJECTED,
                                              "distiller found nothing to merge", d))
            continue
        violation, survivor = _boundary_violation(nb, d)
        if violation:
            plan.results.append(DistillResult(nb.cluster_id, STATUS_REJECTED, violation, d))
            continue
        gate = await _gate(nb, d, llm_fn)
        result = DistillResult(nb.cluster_id, STATUS_DEFERRED, "", d, survivor, gate.rationale)
        plan.results.append(result)
        if not gate.trustworthy:
            result.reason = f"gate gave no trustworthy verdict — retry next pass ({gate.rationale})"
        elif not gate.ok:
            result.status, result.reason = STATUS_REJECTED, f"gate: {gate.rationale}"
        else:
            to_review.append(_ReviewItem(nb.cluster_id, nb, result))

    for start in range(0, len(to_review), review_batch_size):
        batch = to_review[start:start + review_batch_size]
        decisions: list[ReviewDecision] = await _review_batch(
            batch, llm_fn, system=_DISTILL_REVIEW_SYSTEM, render=_render_for_review,
        )
        for item, decision in zip(batch, decisions):
            item.result.reason = f"self-review: {decision.reason}"
            if decision.verdict == VERDICT_APPROVE:
                item.result.status = STATUS_APPROVED
            elif decision.verdict == VERDICT_DEFER:
                item.result.status = STATUS_DEFERRED
            else:
                item.result.status = STATUS_REJECTED

    return plan, render_distill_report(plan)


_STATUS_ICON = {STATUS_APPROVED: "✅", STATUS_REJECTED: "⚠️",
                STATUS_DEFERRED: "💤", STATUS_FAILED: "❌"}


def render_distill_report(plan: DistillPlan) -> str:
    """Body of a ``記憶蒸餾`` entry in the dated dreams file."""
    c = plan.counts()
    nbs = {nb.cluster_id: nb for nb in plan.neighbourhoods}
    out = [
        f"pass_id: `{plan.pass_id}` · mode: read-only (P1)",
        "",
        "### 本輪摘要",
        f"- 掃描範圍：{plan.scanned} 筆 semantic facts"
        + (f"（種子限 domain={plan.domain}）" if plan.domain else ""),
        f"- 鄰域：{len(plan.neighbourhoods)} 個",
        f"- 結果：approved {c[STATUS_APPROVED]} · rejected {c[STATUS_REJECTED]} · "
        f"deferred {c[STATUS_DEFERRED]} · failed {c[STATUS_FAILED]}",
    ]
    if plan.results:
        out += ["", "### 蒸餾記錄"]
    for r in plan.results:
        nb = nbs.get(r.neighbourhood_id)
        seed = f"（種子 `{nb.seed_key}`，{len(nb.members)} 筆）" if nb else ""
        out.append(f"- {_STATUS_ICON.get(r.status, '·')} **{r.status}** — `{r.neighbourhood_id}`{seed}")
        d = r.distillation
        if d is not None and d.covers:
            covers = "、".join(f"`{k}`" for k in d.covers)
            survivor = f"（survivor `{r.survivor_key}`）" if r.survivor_key else ""
            out.append(f"  - 取代：{covers}{survivor}")
            out.append(f"  - 精煉：{d.distilled}")
            for x in d.dropped:
                carried = f"；由 `{x['carried_by']}` 承載" if x.get("carried_by") else ""
                out.append(f"  - 不保留：{x['text']}（{x['reason']}{carried}）")
        if r.reason:
            out.append(f"  - 理由：{r.reason}")
    if plan.notes:
        out += ["", "### 備註"] + [f"- {n}" for n in plan.notes]
    out += ["", "_read-only：本輪僅規劃與自審，未改寫任何記憶。_"]
    return "\n".join(out)
