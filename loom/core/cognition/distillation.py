"""
Distill dream — refine a topic neighbourhood into one fact (#613 E).

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

With ``execute=True`` (P2) the approved distillations are then written:
7. **backup** — a fresh snapshot first; no snapshot → the pass stays read-only;
8. **re-check** — any member changed since planning → stale, skipped; covered
   facts are re-validated as replaceable (lifecycle archives without touching
   ``updated_at``, so the snapshot alone can't see it);
9. **write** — ``semantic.consolidate``: the survivor takes the distilled
   value plus ``produced_by`` / ``distilled_at``, every original stays in its
   metadata, the other covered keys become redirect stubs.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from loom.core.cognition.consolidation import (
    LLMFn,
    _cluster_signature,
    load_suppressed_signatures,
    record_suppressed_signatures,
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
from loom.core.memory.relational_bridge import REL_KEY_PREFIX
from loom.core.memory.semantic import SemanticEntry, StaleWriteError, classify_source

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

#: Prefixes of a failed result's reason, so the report can tell an LLM that
#: answered garbage from one that never answered (#629).
REASON_UNPARSEABLE = "distill output unparseable"
REASON_CALL_FAILED = "distill call failed"
#: Unparseable distill replies are kept here — the only way to tell a
#: truncated answer from format drift (#629).
DEFAULT_DEBUG_DIR = Path.home() / ".loom" / "debug"

#: Execute outcomes of an approved result (P2).
OUTCOME_EXECUTED = "executed"
OUTCOME_STALE = "stale"            # a member changed or vanished since planning
OUTCOME_INELIGIBLE = "ineligible"  # a covered fact is no longer replaceable
OUTCOME_ERROR = "error"            # consolidate raised; nothing written

BACKUP_PREFIX = "memory-pre-distill"
#: Rejected neighbourhoods (by member key@updated_at) aren't re-planned while
#: unchanged, so repeated passes move on instead of re-reviewing them.
SUPPRESS_META_KEY = "distill_dream.suppressed_rejections.v1"
SUPPRESS_RETENTION_DAYS = 30.0
#: Neighbourhoods per self-review call. Each item carries up to 12 facts plus
#: the distillation; with 10 per call (the convergent size), and still at
#: times with 4, a reasoning model spent all 8192 tokens thinking and answered
#: nothing; 2 per call always answered in 250–1350 tokens (rehearsal
#: 2026-10-19). _review_batch also halves an empty answer as a backstop.
DISTILL_REVIEW_BATCH_SIZE = 2
MODE_READ_ONLY = "read-only"
MODE_EXECUTE = "execute"


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
        return True  # can't tell → assume recent; re-distilling too soon is the risk


def _replaceable(entry: SemanticEntry, domain: str | None, now: datetime) -> bool:
    """May this fact be covered (replaced)? Everything else is context only."""
    return (
        not _is_stub(entry)
        and not is_protected(entry)
        and entry.temporal != TEMPORAL_ARCHIVED
        and (domain is None or entry.domain == domain)
        and not _recently_distilled(entry, now)
    )


def _seedable(entry: SemanticEntry, domain: str | None, now: datetime) -> bool:
    # Dream triples may be covered but never seed (spec §4.1 / §5).
    return _replaceable(entry, domain, now) and not _is_dreaming(entry)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Neighbourhood:
    cluster_id: str                      # named for _review_batch reuse
    seed_key: str
    members: list[SemanticEntry]         # seed first, then by similarity
    protected_keys: set[str] = field(default_factory=set)   # hand-written / milestone
    #: members that may not be covered: protected, plus archived, recently
    #: distilled or out-of-domain neighbours
    context_keys: set[str] = field(default_factory=set)


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
    outcome: str = ""                    # execute only; "error: …" carries the cause


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
    mode: str = MODE_READ_ONLY
    backup_failed: bool = False          # execute asked, snapshot failed → read-only

    def outcome_counts(self) -> dict[str, int]:
        c = {OUTCOME_EXECUTED: 0, OUTCOME_STALE: 0, OUTCOME_INELIGIBLE: 0, OUTCOME_ERROR: 0}
        for r in self.results:
            if r.outcome:
                key = r.outcome.split(":", 1)[0]
                c[key] = c.get(key, 0) + 1
        return c

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
    suppress: set[str] | None = None,
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
        part = np.argpartition(-sims, k - 1, axis=1)[:, :k]
        for r, row in enumerate(rows_i):
            top = part[r][np.argsort(-sims[r, part[r]])]
            nbrs = [int(j) for j in top if sims[r, j] >= min_similarity]
            if len(nbrs) + 1 >= MIN_MEMBERS:
                candidates.append((int(counts[r]), int(row), nbrs))
    candidates.sort(key=lambda c: (-c[0], c[1]))

    replaceable = [_replaceable(e, domain, now) for e in entries]
    # Only replaceable facts are consumed by a neighbourhood; context facts
    # (hand-written above all) stay visible to every neighbourhood on their
    # topic, so the no-restating check can't miss them (#622 review).
    used: set[int] = set()
    suppressed = 0
    for _, row, nbrs in candidates:
        if len(plan.neighbourhoods) >= max_neighbourhoods:
            break
        if row in used:
            continue
        members_rows = [row] + [j for j in nbrs if not (replaceable[j] and j in used)]
        n_replaceable = sum(1 for j in members_rows if replaceable[j])
        if len(members_rows) < MIN_MEMBERS or n_replaceable < 2:
            continue
        members = [entries[j] for j in members_rows]
        # Rejected neighbourhoods and the facts in them are both off the
        # table: a neighbouring seed would otherwise rebuild nearly the same
        # set under a new signature and get re-reviewed.
        is_suppressed = bool(suppress) and _cluster_signature(members) in suppress
        used.update(j for j in members_rows if replaceable[j])
        if is_suppressed:
            suppressed += 1
            continue
        plan.neighbourhoods.append(Neighbourhood(
            cluster_id=f"nb-{uuid.uuid4().hex[:8]}",
            seed_key=entries[row].key,
            members=members,
            protected_keys={m.key for m in members if is_protected(m)},
            context_keys={entries[j].key for j in members_rows if not replaceable[j]},
        ))
        for m in members:
            plan.source_versions[m.key] = m.updated_at.isoformat()
    if suppressed:
        plan.notes.append(f"{suppressed} neighbourhood(s) suppressed: rejected earlier, unchanged since")
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
- Facts marked protected=yes must stay as they are (hand-written, permanent,
  or already distilled). Never restate their claims in "distilled". If a fact
  you replace says the same as a protected fact, put that claim in "dropped"
  with "carried_by" = the protected fact's index.
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
        protected = "yes" if m.key in nb.context_keys else "no"
        lines.append(
            f'[{i}] key="{m.key}"  trust={tier}  protected={protected}  '
            f'value="{m.value}"'
        )
    return "\n".join(lines)


def _parse_distillation(nb: Neighbourhood, raw: str) -> tuple[Distillation | None, str]:
    """``(distillation, "")`` or ``(None, why it is unusable)``."""
    data = _parse_json_object(raw)
    if data is None:
        return None, "no JSON object" if raw.strip() else "empty reply"
    distilled = str(data.get("distilled", "")).strip()
    covers_raw = data.get("covers")
    if not isinstance(covers_raw, list):
        return None, "covers is not a list"
    keys: list[str] = []
    for idx in covers_raw:
        try:
            i = int(idx)
        except (TypeError, ValueError):
            return None, f"covers index {idx!r} is not an integer"
        if not 1 <= i <= len(nb.members):
            return None, f"covers index {i} out of range"   # phantom — untrustworthy
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
                and nb.members[ci - 1].key in nb.context_keys:
            entry["carried_by"] = nb.members[ci - 1].key
        if entry["text"]:
            dropped.append(entry)
    return Distillation(
        distilled=distilled, covers=keys, dropped=dropped,
        rationale=str(data.get("rationale", "")).strip(),
    ), ""


async def _distill(nb: Neighbourhood, llm_fn: LLMFn) -> tuple[Distillation | None, list[dict]]:
    """One retry for unparseable output (mirrors ``_gate`` / diff_inventory, #629).

    Returns the distillation (or None) and every unusable attempt as
    ``{"raw", "why"}``. An empty ``covers`` parses fine — "nothing to merge"
    is an answer, not a fault, so it is never retried.
    """
    messages = [
        {"role": "system", "content": _DISTILL_SYSTEM},
        {"role": "user", "content": f"Neighbourhood:\n{_render_neighbourhood(nb)}\n\nReturn the JSON now."},
    ]
    bad: list[dict] = []
    for _ in range(2):
        try:
            raw = await llm_fn(messages)  # exceptions → caller marks the result failed
        except Exception:
            if bad:                       # keep the unusable first reply all the same
                _dump_unparseable(nb, bad)
            raise
        d, why = _parse_distillation(nb, raw)
        if d is not None:
            return d, bad
        bad.append({"raw": raw, "why": why})
    return None, bad


def _dump_unparseable(nb: Neighbourhood, bad: list[dict]) -> Path | None:
    """Keep the raw replies; the report only has room for one line."""
    try:
        DEFAULT_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
        path = DEFAULT_DEBUG_DIR / f"distill_unparseable_{ts}_{nb.cluster_id}.json"
        payload = {
            "ts": datetime.now().isoformat(),
            "neighbourhood_id": nb.cluster_id,
            "seed_key": nb.seed_key,
            "members": [{"key": m.key, "value": m.value} for m in nb.members],
            "attempts": [{"raw": b["raw"], "raw_chars": len(b["raw"]), "why": b["why"]}
                         for b in bad],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        return path
    except Exception as exc:
        logger.warning("[distill-dream] could not keep unparseable output: %s", exc)
        return None


def _boundary_violation(nb: Neighbourhood, d: Distillation) -> tuple[str | None, str | None]:
    """Code-enforced hard boundaries (spec §5). Returns (reason, survivor_key)."""
    if not d.distilled:
        return "empty distilled value", None
    protected = [k for k in d.covers if k in nb.protected_keys]
    if protected:
        return f"covers a protected (hand-written / milestone) fact: {protected}", None
    context = [k for k in d.covers if k in nb.context_keys]
    if context:
        return f"covers a context-only fact (archived / recently distilled / other domain): {context}", None
    if len(d.covers) < 2:
        return "fewer than 2 sources to replace", None
    by_key = {m.key: m for m in nb.members}
    candidates = [by_key[k] for k in d.covers if not _is_dreaming(by_key[k])]
    if not candidates:
        return "no non-dream survivor among the covered facts", None
    # Trust first (the distilled fact inherits the survivor's source), then a
    # key nothing will rewrite: rel:{S}::{P} is derived from the triple, so a
    # later triple with the same subject and predicate lands on it.
    survivor = max(candidates, key=lambda e: (
        _trust_of(e), not e.key.startswith(REL_KEY_PREFIX), -e.created_at.timestamp()))
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
KEPT facts stay as they are (hand-written, permanent or already distilled).
List the [H..] ids of any whose claim DISTILLED restates (that would leave the
same claim in memory twice).

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
    hand = [m for m in nb.members if m.key in nb.context_keys]
    lines = ["SOURCES:"]
    lines += [f'[S{i}] value="{s.value}"' for i, s in enumerate(sources, start=1)]
    if hand:
        lines.append("KEPT:")
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
    # Accept "1" and the "S1" label the prompt shows (#622 review).
    verdicts = {str(k).strip().strip("[]").upper().removeprefix("S"): str(v).strip().lower()
                for k, v in data["sources"].items()}
    if set(verdicts) != {str(i) for i in range(1, len(sources) + 1)} \
            or not set(verdicts.values()) <= _GATE_VERDICTS:
        return _GateResult(False, False, "gate did not cover every source exactly")
    restated_raw = data.get("handwritten_restated") or []
    if not isinstance(restated_raw, list):
        return _GateResult(False, False, "gate handwritten_restated malformed")
    # Only ids actually shown count; "none" / "" / invented ids are noise.
    shown = {f"H{i}" for i in range(1, len(hand) + 1)}
    restated = sorted({str(x).strip().strip("[]").upper() for x in restated_raw} & shown)
    rationale = str(data.get("rationale", "")).strip()
    lost = sorted(k for k, v in verdicts.items() if v == "lost")
    if lost:
        return _GateResult(False, True, f"sources {lost} would lose information — {rationale}")
    if restated:
        return _GateResult(False, True, f"restates kept / hand-written fact(s) {restated} — {rationale}")
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
You are Loom, reviewing how YOUR OWN memory is about to be distilled
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
    review_batch_size: int = DISTILL_REVIEW_BATCH_SIZE,
    execute: bool = False,
    skip_signatures: set[str] | None = None,
) -> tuple[DistillPlan, str]:
    """One distill pass. Returns ``(plan, report_markdown)``.

    Read-only unless ``execute``; then approved results are written after a
    fresh backup, and a failed backup keeps the pass read-only (noted).
    An executing caller holds ``consolidation.memory_rewrite_lock()`` around
    the whole pass (not re-entrant, so this function doesn't take it).
    ``skip_signatures`` adds neighbourhoods to leave alone this pass (the big
    dream passes the ones it already tried).
    """
    plan = await build_distill_plan(
        semantic, domain=domain, max_neighbourhoods=max_neighbourhoods,
        corpus_limit=corpus_limit, min_similarity=min_similarity,
        suppress=(await load_suppressed_signatures(semantic._db, meta_key=SUPPRESS_META_KEY)
                  | (skip_signatures or set())),
    )
    to_review: list[_ReviewItem] = []
    for nb in plan.neighbourhoods:
        try:
            d, bad = await _distill(nb, llm_fn)
        except Exception as exc:
            logger.warning("[distill-dream] distill failed for %s: %s", nb.cluster_id, exc)
            plan.results.append(DistillResult(nb.cluster_id, STATUS_FAILED, f"{REASON_CALL_FAILED}: {exc}"))
            continue
        if d is None:
            path = _dump_unparseable(nb, bad)
            logger.warning("[distill-dream] unparseable distill output for %s (%s) — raw kept at %s",
                           nb.cluster_id, bad[-1]["why"], path)
            plan.results.append(DistillResult(
                nb.cluster_id, STATUS_FAILED,
                f"{REASON_UNPARSEABLE} ({bad[-1]['why']}, attempts={len(bad)})"))
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
            log_label="distill-dream",
        )
        for item, decision in zip(batch, decisions):
            item.result.reason = f"self-review: {decision.reason}"
            if decision.verdict == VERDICT_APPROVE:
                item.result.status = STATUS_APPROVED
            elif decision.verdict == VERDICT_DEFER:
                item.result.status = STATUS_DEFERRED
            else:
                item.result.status = STATUS_REJECTED

    # A rejection is a judgment on unchanged content; deferred / failed are
    # not and stay free to retry. Only executing passes remember it: a
    # read-only pass (rehearsal, the gate-off daemon loop) writes nothing,
    # so it can never change what a later real pass does.
    nbs = {nb.cluster_id: nb for nb in plan.neighbourhoods}
    rejected = {_cluster_signature(nbs[r.neighbourhood_id].members)
                for r in plan.results if r.status == STATUS_REJECTED}
    if rejected and execute:
        await record_suppressed_signatures(
            semantic._db, rejected, meta_key=SUPPRESS_META_KEY,
            retention_days=SUPPRESS_RETENTION_DAYS)

    if execute and plan.counts()[STATUS_APPROVED]:
        # Resolved at call time so tests can stand in for the snapshot.
        from loom.core.memory import backup
        if await backup.snapshot_before_write(semantic._db, prefix=BACKUP_PREFIX) is None:
            logger.warning("[distill-dream] pre-execute backup failed — read-only this pass")
            plan.backup_failed = True
            plan.notes.append("設定為 execute，但執行前備份失敗，本輪降級為 read-only（未改寫任何記憶）。")
        else:
            await execute_distill_plan(semantic, plan)

    return plan, render_distill_report(plan)


async def execute_distill_plan(
    semantic: "SemanticMemory", plan: DistillPlan, *, now: datetime | None = None,
) -> None:
    """Write the approved results of *plan*; sets each one's ``outcome``.

    Trusts that a backup exists — ``run_distill_dream`` takes it first.
    """
    plan.mode = MODE_EXECUTE
    now = now or datetime.now(UTC)
    nbs = {nb.cluster_id: nb for nb in plan.neighbourhoods}
    for r in plan.results:
        if r.status != STATUS_APPROVED:
            continue
        nb, d = nbs[r.neighbourhood_id], r.distillation
        # Context members count too: an edited hand-written fact changes what
        # "already carried" meant when the gate judged the distillation.
        current = {m.key: await semantic.get(m.key) for m in nb.members}
        if any(e is None or e.updated_at.isoformat() != plan.source_versions.get(k)
               for k, e in current.items()):
            r.outcome = OUTCOME_STALE
            continue
        covered = [current[k] for k in d.covers]
        if not all(_replaceable(e, plan.domain, now) for e in covered):
            r.outcome = OUTCOME_INELIGIBLE
            continue
        try:
            await semantic.consolidate(
                covered,
                refined_value=d.distilled,
                survivor_key=r.survivor_key,
                sacrificed_content="\n\n".join(
                    f"[{e.key}] {e.value}" for e in covered if e.key != r.survivor_key),
                merge_rationale=_merge_rationale(plan, d),
                extra_metadata=planned_survivor_metadata(now=now),
                # re-checked inside the write: a concurrent memorize between
                # the reads above and the UPDATE must not be overwritten.
                expected_versions={k: plan.source_versions[k] for k in d.covers},
            )
        except StaleWriteError:
            r.outcome = OUTCOME_STALE
            continue
        except Exception as exc:
            logger.warning("[distill-dream] consolidate failed for %s: %s", nb.cluster_id, exc)
            r.outcome = f"{OUTCOME_ERROR}: {exc}"
            continue
        r.outcome = OUTCOME_EXECUTED


def _merge_rationale(plan: DistillPlan, d: Distillation) -> str:
    lines = [f"distill_dream {plan.pass_id}: {d.rationale or 'distilled'}"]
    for x in d.dropped:
        carried = f"; carried by {x['carried_by']}" if x.get("carried_by") else ""
        lines.append(f"dropped: {x['text']} ({x['reason']}{carried})")
    return "\n".join(lines)


_OUTCOME_TEXT = {
    OUTCOME_EXECUTED: "已寫入",
    OUTCOME_STALE: "stale — 規劃後有來源變動，跳過",
    OUTCOME_INELIGIBLE: "ineligible — 有被取代的記憶已不可取代（例如已 archived），跳過",
}

_STATUS_ICON = {STATUS_APPROVED: "✅", STATUS_REJECTED: "⚠️",
                STATUS_DEFERRED: "💤", STATUS_FAILED: "❌"}


def _failure_split(plan: DistillPlan) -> str:
    """``（unparseable n · exception m）`` — an LLM answering garbage and an
    LLM not answering need different fixes (#629)."""
    failed = [r.reason for r in plan.results if r.status == STATUS_FAILED]
    if not failed:
        return ""
    unparseable = sum(r.startswith(REASON_UNPARSEABLE) for r in failed)
    return f"（unparseable {unparseable} · exception {len(failed) - unparseable}）"


def render_distill_report(plan: DistillPlan) -> str:
    """Body of a ``記憶蒸餾`` entry in the dated dreams file."""
    c = plan.counts()
    nbs = {nb.cluster_id: nb for nb in plan.neighbourhoods}
    out = [
        f"pass_id: `{plan.pass_id}` · mode: {plan.mode}",
        "",
        "### 本輪摘要",
        f"- 掃描範圍：{plan.scanned} 筆 semantic facts"
        + (f"（種子限 domain={plan.domain}）" if plan.domain else ""),
        f"- 鄰域：{len(plan.neighbourhoods)} 個",
        f"- 結果：approved {c[STATUS_APPROVED]} · rejected {c[STATUS_REJECTED]} · "
        f"deferred {c[STATUS_DEFERRED]} · failed {c[STATUS_FAILED]}"
        + _failure_split(plan),
    ]
    if plan.mode == MODE_EXECUTE:
        o = plan.outcome_counts()
        out.append(f"- 執行：executed {o[OUTCOME_EXECUTED]} · stale {o[OUTCOME_STALE]} · "
                   f"ineligible {o[OUTCOME_INELIGIBLE]} · error {o[OUTCOME_ERROR]}")
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
        if r.outcome:
            out.append(f"  - 執行：{_OUTCOME_TEXT.get(r.outcome, r.outcome)}")
    if plan.notes:
        out += ["", "### 備註"] + [f"- {n}" for n in plan.notes]
    if plan.mode == MODE_EXECUTE and plan.outcome_counts()[OUTCOME_EXECUTED]:
        out += ["", "_execute：已寫入的蒸餾，原文都保存在 survivor 的 metadata.consolidated_into，"
                    "被取代的 key 留有重導向 stub。_"]
    elif plan.mode == MODE_EXECUTE:
        out += ["", "_execute：本輪沒有任何蒸餾寫入（全部跳過或失敗），記憶未改寫。_"]
    else:
        out += ["", "_read-only：本輪僅規劃與自審，未改寫任何記憶。_"]
    return "\n".join(out)
