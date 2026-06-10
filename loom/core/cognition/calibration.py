"""
Prediction Spine — calibration residue + surprise three quantities
(epic #528, spec docs/designs/58 §3.2/§6; decay tier #530 = Option A).

This is where the spine's loop closes into *residue*. Reconciliation (slice 3)
scores individual bets against runtime observation; those episodic records decay
like any other memory. What **persists** is the roll-up: a per-domain
``calibration:<domain>`` semantic summary — "I'm well-calibrated about CLI
behaviour, badly calibrated about how DK reacts." That summary is the world-model
residue (§3.2), and it is the P0 acceptance signal: a frozen-weights agent
accumulating calibration in the framework layer, no reward, no weight update.

Two halves, deliberately separable so the write can be gated on its own:

* :func:`compute_calibration` — a *pure* aggregation over reconciled records.
  No I/O, no side effects. Read-only by construction.
* :func:`write_calibration` — persists the summaries as semantic facts, **only**
  when ``execute=True``. This gate is the slice-4 half of I3/I5: calibration is
  written and *nothing else* — no mood change, no Critic call, no surprise push.
  It is intentionally NOT folded into reconcile's execute gate.

The three surprise quantities (§6) ride along on each summary as inert,
read-only data. P0 has no consumer for them (Affect arm is P1, Exploration arm
is P2); they are computed and broadcast, never acted on here.

**Invariants enforced structurally:**

* **I4** — only ``status == "reconciled"`` records with a non-``None`` ``score``
  contribute. A pending/due/stale bet, or a malformed reconciled-but-unscored
  one, never counts as a free perfect outcome.
* **I6** — the only input is prediction records carrying observation-derived
  scores. There is no parameter through which user sentiment can enter.
* **#530 (Option A)** — the summary is a *normal* semantic fact
  (``domain=knowledge``, ``temporal=recent``). Its rolling re-write keeps the
  decay anchor fresh for any domain still being bet on; an abandoned domain
  fades through the ordinary half-life. No milestone-permanence hack.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from loom.core.memory.ontology import DOMAIN_KNOWLEDGE, TEMPORAL_RECENT
from loom.core.memory.semantic import SemanticEntry

# Below this many reconciled bets, a domain is "sample-insufficient": you have
# not exercised it enough to trust its calibration, regardless of accuracy.
SAMPLE_FLOOR = 5

# Resolver kinds with a mechanically unambiguous success pole, so a *surprising*
# outcome can be signed (positive vs negative). P0 keeps this conservative — a
# resolver without an intrinsic good/bad pole (row_count, duration_bucket) gets
# valence 0 rather than letting the spine invent a value judgment (I5).
_POLAR_RESOLVERS = {"tool_success"}


@dataclass
class CalibrationSummary:
    """Rolled-up calibration for one prediction domain, plus the §6 quantities.

    ``error_score`` / ``calibration_score`` describe *how well* the domain is
    predicted. ``uncertainty`` describes *how much exploration should be drawn*
    there (poor calibration OR thin sampling). ``appraisal_valence`` describes
    whether the domain's surprises have leaned positive or negative.
    """
    domain: str
    n: int
    error_score: float          # mean error, 0.0 = perfect, 1.0 = always wrong
    uncertainty: float          # max(mean_error, sample_insufficiency), [0, 1]
    appraisal_valence: float    # mean signed surprise, [-1, 1]
    calibration_score: float    # 1 - error_score, [0, 1]

    @property
    def key(self) -> str:
        return f"calibration:{self.domain}"

    def to_metadata(self) -> dict:
        return {
            "n": self.n,
            "error_score": round(self.error_score, 4),
            "uncertainty": round(self.uncertainty, 4),
            "appraisal_valence": round(self.appraisal_valence, 4),
            "calibration_score": round(self.calibration_score, 4),
        }

    def to_dict(self) -> dict:
        """Explicit serialization — every field named, so a future
        datetime/UUID field can't silently leak through ``asdict`` (絲絲 #532
        OQ3 carryover). The dream adapter logs this."""
        return {
            "domain": self.domain,
            "key": self.key,
            "n": self.n,
            "error_score": round(self.error_score, 4),
            "uncertainty": round(self.uncertainty, 4),
            "appraisal_valence": round(self.appraisal_valence, 4),
            "calibration_score": round(self.calibration_score, 4),
        }


@dataclass
class CalibrationReport:
    """Auditable projection of one calibration pass — the dream-journal view.

    ``written`` records whether the residue was actually persisted (execute) or
    merely computed (dry-run). The per-domain summaries carry the §6 quantities.
    """
    written: bool
    summaries: list = field(default_factory=list)
    # P0.5-b (#539): the read-only health verdict over this snapshot. Optional so
    # existing constructions stay back-compatible; populated by run_calibration_pass.
    health: object | None = None

    def summary(self) -> str:
        verb = "wrote" if self.written else "would write (dry-run)"
        return f"calibration: {verb} {len(self.summaries)} domain residue(s)"

    def render(self) -> str:
        lines = [self.summary()]
        if self.summaries:
            lines.append("")
            verb = "wrote" if self.written else "would write"
            for s in sorted(self.summaries, key=lambda x: x.domain):
                lines.append(
                    f"  - {verb} {s.key}: score={s.calibration_score:.2f} "
                    f"(n={s.n}, error={s.error_score:.2f}, "
                    f"uncertainty={s.uncertainty:.2f}, valence={s.appraisal_valence:+.2f})"
                )
        if self.health is not None:
            lines.append("")
            lines.append(self.health.render())
        return "\n".join(lines)

    def to_dict(self) -> dict:
        out = {
            "written": self.written,
            "counts": {"domains": len(self.summaries)},
            "summaries": [s.to_dict() for s in self.summaries],
        }
        if self.health is not None:
            out["health"] = self.health.to_dict()
        return out


def _record_valence(record) -> float:
    """Signed surprise for one reconciled bet, computed from the record alone.

    ``valence = polarity × error_score``. Polarity is the factual outcome pole
    (did the tool actually succeed?), derived from the resolver's ``expect`` and
    whether the bet ``matched`` (error 0). This is a *factual* polarity, not a
    value judgment — an unexpected success is +, an unexpected failure is −, and
    a correctly-predicted outcome is ~0 (no surprise to appraise). The Critic
    re-appraises in context downstream (I5); the spine only tags direction.
    """
    if record.resolver.get("kind") not in _POLAR_RESOLVERS:
        return 0.0
    score = record.score
    expect = record.resolver.get("expect", True)
    matched = score == 0.0
    observed_success = expect if matched else (not expect)
    polarity = 1.0 if observed_success else -1.0
    return polarity * score


def compute_calibration(records) -> list[CalibrationSummary]:
    """Aggregate reconciled predictions into per-domain calibration summaries.

    Pure and read-only. ``records`` is any iterable of ``PredictionRecord``;
    only ``reconciled`` records with a real ``score`` contribute (I4). The sole
    input is prediction-vs-observation scores — no sentiment channel (I6).
    """
    scored: dict[str, list[float]] = defaultdict(list)
    valences: dict[str, list[float]] = defaultdict(list)

    for r in records:
        if r.status != "reconciled" or r.score is None:
            continue  # I4 — unreconciled ≠ verified; no silent perfect bet
        scored[r.domain].append(r.score)
        valences[r.domain].append(_record_valence(r))

    summaries: list[CalibrationSummary] = []
    for domain, errs in scored.items():
        n = len(errs)
        mean_error = sum(errs) / n
        sample_insufficiency = max(0.0, (SAMPLE_FLOOR - n) / SAMPLE_FLOOR)
        uncertainty = max(mean_error, sample_insufficiency)
        valence = sum(valences[domain]) / n
        summaries.append(
            CalibrationSummary(
                domain=domain,
                n=n,
                error_score=mean_error,
                uncertainty=uncertainty,
                appraisal_valence=valence,
                calibration_score=1.0 - mean_error,
            )
        )
    return summaries


async def write_calibration(
    semantic, summaries, *, execute: bool = False
) -> list[str]:
    """Persist calibration summaries as semantic facts. Gated on ``execute``.

    With ``execute=False`` (default) nothing is written — the caller still has
    the computed summaries for its report (I3: read-only by default). With
    ``execute=True`` each summary is upserted under its stable
    ``calibration:<domain>`` key as a **normal** semantic fact (``knowledge`` /
    ``recent`` — #530 Option A). The upsert is what makes the residue *rolling*:
    re-writing the same key each dream refreshes the decay anchor for any domain
    still in play, so it never reaches the half-life; an abandoned domain stops
    being touched and fades through the ordinary path.

    Returns the list of keys written (empty in dry-run). This function writes
    calibration and nothing else — no mood, no Critic, no surprise push (I5).
    """
    if not execute:
        return []

    written: list[str] = []
    for s in summaries:
        value = (
            f"calibration[{s.domain}]: score={s.calibration_score:.2f} "
            f"(n={s.n}, error={s.error_score:.2f}, uncertainty={s.uncertainty:.2f}, "
            f"valence={s.appraisal_valence:+.2f})"
        )
        await semantic.upsert(
            SemanticEntry(
                key=s.key,
                value=value,
                confidence=s.calibration_score,
                source="prediction_spine",
                metadata=s.to_metadata(),
                domain=DOMAIN_KNOWLEDGE,
                temporal=TEMPORAL_RECENT,
            )
        )
        written.append(s.key)
    return written


# Roll the whole reconciled corpus, not just one pass — calibration is a
# rolling aggregate. Generous so a low-volume P0 store gets everything; ASC by
# created_at means oldest-first, which is irrelevant when we take them all.
_CALIBRATION_CORPUS_LIMIT = 5000


async def run_calibration_pass(
    store, semantic, *, execute: bool = False
) -> CalibrationReport:
    """Roll every reconciled bet into per-domain calibration residue.

    The slice-4.5 orchestration that rides the convergent dream's cadence. It is
    independent of whether *this* dream's reconcile scored anything new (絲絲 PR
    #534): it aggregates the standing reconciled corpus. ``execute=False`` (the
    default) computes the summaries for the journal but writes nothing (I3);
    ``execute=True`` persists them via :func:`write_calibration`.
    """
    # Local import avoids a module-level cycle: calibration_health imports
    # SAMPLE_FLOOR from here. The health pass is pure/read-only (spec §12.4) and
    # computed unconditionally — it rides the report regardless of the write gate,
    # so the immune verdict shows up in dry-run journals too.
    from loom.core.cognition.calibration_health import assess_calibration_health

    records = await store.list_by_status(
        "reconciled", limit=_CALIBRATION_CORPUS_LIMIT
    )
    summaries = compute_calibration(records)
    await write_calibration(semantic, summaries, execute=execute)
    health = assess_calibration_health(summaries, records)
    return CalibrationReport(
        written=execute and bool(summaries), summaries=summaries, health=health,
    )
