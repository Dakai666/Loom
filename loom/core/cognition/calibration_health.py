"""
Prediction Spine — calibration self-monitoring (epic #528, P0.5-b, spec §12.4,
issue #539).

The spine's **immune system**. P0's structural defences (I2 only-observation,
I6 no-sentiment, I4 unverified-doesn't-count, rolling overwrite, namespaced
decay) keep "*what can be written*" clean. This layer watches "*was something
meaningless written*" — so the integrity guarantee lives in the mechanism, not
in DK reading audit reports by hand. That is the prerequisite for the full-auto
calibration writes DK wants.

It is **read-only**: it reads the per-domain :class:`CalibrationSummary` roll-up
and the reconciled records that produced it, and emits a report. It writes
nothing, drives no behaviour, and needs no gate (report-only is safe — any
*self-correcting* action would be a separate, gated slice). I5/I6 do not regress:
the only inputs are observation-derived calibration and the bets' own
provenance; there is no channel through which a value judgment or user sentiment
could enter.

What it flags (stateless, this slice):

* ``low_information`` — the headline (絲絲 #538). A domain whose calibration sits
  ≈1.0 with flat valence and enough sample is *high but uninformative*: under
  flat implicit betting most tools just succeed, and "the tool succeeded" is not
  the insight "I predict well here". Told apart from genuine signal so a
  consumer (P1/P2) isn't lured by a near-perfect score that means nothing.
* ``sample_insufficient`` — ``n < SAMPLE_FLOOR``: the score is statistically
  unstable; say so rather than let a noisy 1.0 be read as mastery.
* ``genuine_signal`` — calibration meaningfully off 1.0, or a directional
  valence lean: the domains worth attending to.
* ``monoculture`` (corpus-level) — every reconciled bet is implicit (``auto:``),
  none explicit. Calibration is then measuring tool reliability, not prediction
  skill — which is *why* a slice-B-only phase flatlines. Self-clears once the
  explicit ``predict`` tool's wagers flow.

Deferred to a post-observation cut (thresholds tuned on real 1–2 week data):
``anomalous_jump`` (needs a calibration history snapshot, which the rolling
overwrite destroys) and ``fragmentation`` (synonym-split domain labels — too
fuzzy to threshold without real data). The thresholds below are conservative
a-priori values, not yet tuned.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from loom.core.cognition.calibration import SAMPLE_FLOOR

# At/above this calibration_score a domain is "near-perfect". Combined with flat
# valence and adequate sample, near-perfect reads as uninformative, not skilful.
HIGH_CAL = 0.95
# |appraisal_valence| at/below this = no directional surprise to appraise.
FLAT_VALENCE = 0.05

# Classifications (mutually exclusive per domain).
LOW_INFORMATION = "low_information"
SAMPLE_INSUFFICIENT = "sample_insufficient"
GENUINE_SIGNAL = "genuine_signal"


@dataclass
class DomainHealth:
    """One domain's calibration health verdict, with the numbers behind it."""
    domain: str
    classification: str
    reason: str
    n: int
    calibration_score: float
    uncertainty: float
    appraisal_valence: float

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "classification": self.classification,
            "reason": self.reason,
            "n": self.n,
            "calibration_score": round(self.calibration_score, 4),
            "uncertainty": round(self.uncertainty, 4),
            "appraisal_valence": round(self.appraisal_valence, 4),
        }


@dataclass
class CalibrationHealthReport:
    """Read-only health projection over one calibration snapshot."""
    domains: list = field(default_factory=list)
    monoculture: bool = False
    monoculture_detail: str = ""

    def low_information(self) -> list:
        return [d for d in self.domains if d.classification == LOW_INFORMATION]

    def sample_insufficient(self) -> list:
        return [d for d in self.domains if d.classification == SAMPLE_INSUFFICIENT]

    def genuine(self) -> list:
        return [d for d in self.domains if d.classification == GENUINE_SIGNAL]

    def summary(self) -> str:
        return (
            f"calibration health: {len(self.genuine())} genuine, "
            f"{len(self.low_information())} low-info, "
            f"{len(self.sample_insufficient())} thin-sample"
            + (" — MONOCULTURE" if self.monoculture else "")
        )

    def render(self) -> str:
        lines = [self.summary()]
        if self.monoculture:
            lines.append(f"  ⚠ monoculture: {self.monoculture_detail}")
        for d in sorted(self.domains, key=lambda x: (x.classification, x.domain)):
            lines.append(
                f"  - [{d.classification}] {d.domain}: "
                f"score={d.calibration_score:.2f} (n={d.n}, "
                f"uncertainty={d.uncertainty:.2f}, valence={d.appraisal_valence:+.2f}) "
                f"— {d.reason}"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "monoculture": self.monoculture,
            "monoculture_detail": self.monoculture_detail,
            "counts": {
                GENUINE_SIGNAL: len(self.genuine()),
                LOW_INFORMATION: len(self.low_information()),
                SAMPLE_INSUFFICIENT: len(self.sample_insufficient()),
            },
            "domains": [d.to_dict() for d in self.domains],
        }


def _classify(summary) -> tuple[str, str]:
    """Verdict + human reason for one domain. Sample-sufficiency is checked
    first: you cannot call a barely-sampled domain 'uninformative' (you haven't
    looked enough), only 'unstable'."""
    if summary.n < SAMPLE_FLOOR:
        return SAMPLE_INSUFFICIENT, (
            f"n={summary.n} < {SAMPLE_FLOOR}: calibration_score statistically "
            "unstable — don't read it as mastery"
        )
    if summary.calibration_score >= HIGH_CAL and abs(summary.appraisal_valence) <= FLAT_VALENCE:
        return LOW_INFORMATION, (
            f"score≈{summary.calibration_score:.2f}, valence≈0: flatlined — high "
            "but uninformative (the tool just succeeds; not an insight)"
        )
    return GENUINE_SIGNAL, (
        "calibration meaningfully off 1.0 or non-flat valence — worth attention"
    )


def _detect_monoculture(records) -> tuple[bool, str]:
    """True when every reconciled bet is implicit (``auto:``) with none explicit.

    Only *reconciled* bets count (I4-adjacent): a pile of still-pending explicit
    wagers must not 'clear' the flag before they actually settle into signal.
    Below the sample floor, diversity is unjudgeable — don't cry wolf.
    """
    reconciled = [r for r in records if r.status == "reconciled" and r.score is not None]
    n = len(reconciled)
    if n < SAMPLE_FLOOR:
        return False, f"too few reconciled bets ({n}) to judge diversity"
    explicit = sum(1 for r in reconciled if (r.context or "").startswith("explicit:"))
    if explicit == 0:
        return True, (
            f"all {n} reconciled bets are implicit (auto:) — calibration measures "
            "tool reliability, not prediction skill; flat-near-1.0 is expected "
            "until explicit wagers flow"
        )
    return False, f"{explicit}/{n} reconciled bets are explicit wagers"


def assess_calibration_health(summaries, records) -> CalibrationHealthReport:
    """Assess one calibration snapshot. Pure, read-only (spec §12.4).

    ``summaries`` is the per-domain :class:`CalibrationSummary` roll-up;
    ``records`` the reconciled :class:`PredictionRecord` corpus they came from
    (used only for the corpus-level monoculture check). The two-argument
    signature is the structural I6 guarantee — no sentiment channel exists.
    """
    domains = []
    for s in summaries:
        cls, reason = _classify(s)
        domains.append(DomainHealth(
            domain=s.domain,
            classification=cls,
            reason=reason,
            n=s.n,
            calibration_score=s.calibration_score,
            uncertainty=s.uncertainty,
            appraisal_valence=s.appraisal_valence,
        ))
    mono, detail = _detect_monoculture(records)
    return CalibrationHealthReport(
        domains=domains, monoculture=mono, monoculture_detail=detail,
    )
