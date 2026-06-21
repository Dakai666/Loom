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
            },
            "truncated": self.truncated,
            "proposals": [asdict(p) for p in self.proposals],
            "skipped": [asdict(s) for s in self.skipped],
        }

    def summary(self) -> str:
        verb = "reconciled" if self.executed else "would reconcile"
        tail = "" if self.executed else " (dry-run)"
        cap = " [TRUNCATED: scan budget hit, backlog remains]" if self.truncated else ""
        return (
            f"prediction reconcile: {verb} {len(self.proposals)}, "
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
    scanned = 0
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
