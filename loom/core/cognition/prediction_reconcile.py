"""
Prediction Spine — reconciliation pipeline (epic #528, spec docs/designs/58 §5).

The convergent-dream *sibling*: where ``ConvergentDream`` consolidates semantic
facts, this closes the spine's loop by judging matured predictions against
runtime ground truth. It follows the same discipline (spec §5, Codex §14.7):

    pending bet → find settling observation → resolve ref → apply resolver
                → propose (dry-run)  /  mark_reconciled (execute)

**Read-only by construction in dry-run.** ``execute=False`` walks reads only —
``list_by_status`` / ``find_settling_observation`` / ``resolve_observation_ref``
/ ``resolve``. The single write path (``mark_reconciled``) is gated behind
``execute=True``. This is the function-level half of I3: the surprise the spine
emits is a projection of observation, never something the dream writes back
speculatively. The report is inert, auditable data — every proposal names the
prediction, the observation it was judged against, the resolver, and the score.

**Ground truth only (I2).** Observations come solely from
``resolve_observation_ref`` (the action_records / session_log whitelist). A
resolver that cannot see the field it needs leaves the bet *unresolvable* —
skipped, never silently scored 0.0.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from loom.core.memory.observation import (
    find_settling_observation,
    resolve_observation_ref,
)
from loom.core.memory.resolvers import resolve


# ---------------------------------------------------------------------------
# Report shape — the auditable projection 絲絲 reviews
# ---------------------------------------------------------------------------

# Bet provenance — coarse origin of a wager, by ``context`` prefix. Lets the
# report split deliberate ``predict``-tool bets from the involuntary heartbeat so
# the #560 nudge's effect on the ``auto:`` monoculture is observable at a glance.
_KNOWN_PROVENANCE = ("auto", "explicit")
# Fixed display order for the summary split (絲絲 PR #561 review): intentional,
# not alphabetical — a future fourth provenance slots in here deliberately rather
# than landing wherever ``sorted()`` happens to put it. ``other`` always trails.
_PROVENANCE_DISPLAY_ORDER = (*_KNOWN_PROVENANCE, "other")


def bet_provenance(context: str | None) -> str:
    """Classify a bet by its ``context`` prefix: ``auto`` (heartbeat),
    ``explicit`` (predict tool), or ``other`` (free-text / unset). Splitting on
    the first ``:`` keeps it robust to the suffix (``auto:implicit_tool_success``
    vs ``auto:implicit_duration_bucket`` both fold to ``auto``)."""
    prefix = (context or "").split(":", 1)[0]
    return prefix if prefix in _KNOWN_PROVENANCE else "other"


@dataclass
class ReconcileProposal:
    """One matured bet, judged. The unit of audit: *this* prediction was scored
    against *this* observation by *this* resolver, yielding *this* error."""
    prediction_id: str
    domain: str
    observation_ref: str
    resolver_kind: str
    matched: bool
    error_score: float
    detail: str = ""
    provenance: str = "other"  # auto / explicit / other — see bet_provenance


@dataclass
class ReconcileSkip:
    """A candidate that could not be reconciled this pass, and why.

    reason ∈ {"not_settled" (no terminal observation yet),
              "observation_gone" (ref row vanished),
              "unresolvable" (resolver cannot read its field — no silent pass)}

    ``domain`` is carried so slice 4 can attribute skips per-domain (e.g.
    high ``unresolvable`` in one domain = a resolver-spec bug) without re-joining
    prediction_records (絲絲 review).
    """
    prediction_id: str
    reason: str
    domain: str = ""


@dataclass
class ReconcileReport:
    executed: bool
    proposals: list[ReconcileProposal] = field(default_factory=list)
    skipped: list[ReconcileSkip] = field(default_factory=list)
    # #557 drain-loop audit: how many open bets the pass walked, and whether it
    # stopped on the scan budget with more still waiting (no silent cap).
    scanned: int = 0
    truncated: bool = False

    @property
    def settleable(self) -> int:
        return len(self.proposals)

    def provenance_counts(self) -> dict:
        """Settled proposals grouped by bet provenance (auto / explicit / other).
        The #560 nudge is meant to lift ``explicit`` off zero — this is the
        number that says whether it's working. Only non-zero buckets appear."""
        counts: dict[str, int] = {}
        for p in self.proposals:
            counts[p.provenance] = counts.get(p.provenance, 0) + 1
        return counts

    def render(self) -> str:
        """Render as a 夢境鞏固-style body for the dream journal (slice 3.5).

        Both the ``prediction_reconcile`` tool and the daemon's weekly loop
        write this to the circadian dreams file.
        """
        lines = [self.summary(), ""]
        if self.proposals:
            lines.append("Proposals:")
            verb = "reconciled" if self.executed else "would reconcile"
            for p in self.proposals:
                lines.append(
                    f"  - [{p.domain}] {verb} {p.prediction_id} vs "
                    f"{p.observation_ref} ({p.resolver_kind}): "
                    f"error={p.error_score:.3f} — {p.detail}"
                )
        if self.skipped:
            lines.append("Skipped:")
            for s in self.skipped:
                lines.append(f"  - [{s.domain}] {s.prediction_id}: {s.reason}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Serialize for logging by the dream maintenance adapter (slice 3.5)."""
        return {
            "executed": self.executed,
            "counts": {
                "proposed": len(self.proposals),
                "skipped": len(self.skipped),
                "scanned": self.scanned,
                "by_provenance": self.provenance_counts(),
            },
            "truncated": self.truncated,
            "proposals": [asdict(p) for p in self.proposals],
            "skipped": [asdict(s) for s in self.skipped],
        }

    def summary(self) -> str:
        verb = "reconciled" if self.executed else "would reconcile"
        tail = "" if self.executed else " (dry-run)"
        cap = " [TRUNCATED: scan budget hit, backlog remains]" if self.truncated else ""
        # Provenance split rides on the proposed count so the dream journal shows
        # explicit-vs-auto flow inline (omitted when nothing settled).
        prov = self.provenance_counts()
        ordered = [k for k in _PROVENANCE_DISPLAY_ORDER if k in prov]
        split = (
            " (" + ", ".join(f"{k} {prov[k]}" for k in ordered) + ")"
            if ordered else ""
        )
        return (
            f"prediction reconcile: {verb} {len(self.proposals)}{split}, "
            f"skipped {len(self.skipped)} (scanned {self.scanned}){tail}{cap}"
        )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def run_prediction_reconciliation(
    store,
    db,
    *,
    execute: bool = False,
    batch_size: int = 200,
    max_scan: int = 50_000,
) -> ReconcileReport:
    """Reconcile the settleable pending/due backlog. See module docstring.

    ``store`` is a ``PredictionStore``; ``db`` an open connection. With
    ``execute=False`` (default) nothing is written — the report says what *would*
    happen. With ``execute=True`` each settled bet is committed via
    ``mark_reconciled`` as it is judged.

    **Drains the whole backlog, not one batch (#557).** A naive single
    ``LIMIT 200`` pull settles at most 200 pending + 200 due per weekly pass —
    below the bet-generation rate once both implicit heartbeats are on, so the
    open set grows unbounded and the oldest-200 window starves newer bets. This
    loops via ``list_open_after`` keyset pagination over ``(created_at, id)``,
    walking the entire open set once per pass. Keyset (not OFFSET) keeps the
    cursor stable as ``mark_reconciled`` removes settled bets mid-walk. The
    ``max_scan`` budget bounds a pathological backlog; hitting it sets
    ``truncated`` rather than silently capping.
    """
    proposals: list[ReconcileProposal] = []
    skipped: list[ReconcileSkip] = []
    cursor = None  # (created_at, id) keyset cursor; advanced each batch
    scanned = 0  # total open bets *fetched* — includes skipped/unsettleable, NOT a reconciled count
    truncated = False

    while True:
        if scanned >= max_scan:
            # Budget exhausted — probe whether anything still waits past it so the
            # report can flag a partial drain instead of looking complete.
            remainder = await store.list_open_after(after=cursor, limit=1)
            truncated = bool(remainder)
            break

        want = min(batch_size, max_scan - scanned)
        batch = await store.list_open_after(after=cursor, limit=want)
        if not batch:
            break

        for pred in batch:
            ref = await find_settling_observation(db, pred.due_condition)
            if ref is None:
                skipped.append(ReconcileSkip(pred.id, "not_settled", pred.domain))
                continue
            observation = await resolve_observation_ref(db, ref)
            if observation is None:
                skipped.append(ReconcileSkip(pred.id, "observation_gone", pred.domain))
                continue
            try:
                result = resolve(pred.resolver, observation)
            except (KeyError, ValueError):
                # resolver cannot read the field it needs against this observation
                # — unresolvable, not a free 0.0 (no silent pass; pairs with I4).
                skipped.append(ReconcileSkip(pred.id, "unresolvable", pred.domain))
                continue
            proposals.append(
                ReconcileProposal(
                    prediction_id=pred.id,
                    domain=pred.domain,
                    observation_ref=ref,
                    resolver_kind=pred.resolver["kind"],
                    matched=result.matched,
                    error_score=result.error_score,
                    detail=result.detail,
                    provenance=bet_provenance(pred.context),
                )
            )
            if execute:
                # Settle as we go: the bet leaves the open set immediately, which
                # the keyset cursor already steps past — bounding memory and making
                # the drain observable mid-pass.
                await store.mark_reconciled(
                    pred.id, score=result.error_score, observation_ref=ref
                )

        scanned += len(batch)
        cursor = (batch[-1].created_at, batch[-1].id)
        if len(batch) < want:
            break  # short page — open set past the cursor is exhausted

    return ReconcileReport(
        executed=execute,
        proposals=proposals,
        skipped=skipped,
        scanned=scanned,
        truncated=truncated,
    )
