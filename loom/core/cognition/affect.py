"""
P1 affect arm — ``environment_friction`` (epic #528, issue #487, spec
docs/designs/60).

**P1 builds metabolism, not emotion.** Under the measured terrain nearly every
settled surprise is "the tool was slower than I bet" — a signal about the
*world*, not about the agent. So this module keeps a decaying arousal reading
whose only injection source is prediction error, and surfaces it through one
deterministic Critic note. Emotion needs a self-referring signal; that is P1.5.

Pieces, separable on purpose:

* :func:`compute_surprise` — pure. Given the reconciled corpus and the window of
  newly settled bets, measures each domain's deviation from its *own* standing
  baseline (S1). Contracts: baselines come from the health verdict over the
  pre-window corpus (C1); sample-insufficient domains are excluded, while
  LOW_INFORMATION domains serve as deviation baselines but never as capability
  evidence (C2 / D7); explicit wagers feed a separate ``model`` track only when
  they are not effectively a monoculture (C3).
* :class:`AffectState` — two tracks, exponential decay, clamp ``[0, 1]``.
* :func:`settle_friction` — load → read → absorb → optionally persist to
  ``memory_meta`` (never semantic memory: state is not a fact).
* :func:`render_friction_note` — **the Critic, the only exit** (I5). Readings,
  sources and confidence; no interpreting prose, no tone instruction.

**Invariants enforced structurally:** I3 — nothing here writes
``prediction_records`` or ``calibration:*``; I5 — no other public function
returns text; I6 — no parameter through which user sentiment could enter.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from loom.core.cognition.calibration import SAMPLE_FLOOR, compute_calibration
from loom.core.cognition.calibration_health import (
    SAMPLE_INSUFFICIENT,
    assess_calibration_health,
)

HALF_LIFE_HOURS = 12.0
_LAMBDA = math.log(2) / HALF_LIFE_HOURS
# Shrinkage prior: a domain needs ~SAMPLE_FLOOR effective observations in the
# window before its deviation counts at full weight (W2).
N0 = SAMPLE_FLOOR
# Consumer-side "effective monoculture" floor (C3). Stricter than — and
# independent of — calibration_health's binary ``explicit == 0`` flag, which
# stays the immune system's alarm semantics and is not touched here.
EXPLICIT_SHARE_FLOOR = 0.01
# A first read (or a long sleep) looks back at most this far; older settles
# would be aged to ~1/64 anyway.
LOOKBACK_HOURS = 72.0
CORPUS_LIMIT = 5000  # same rolling window as the weekly calibration pass (#574)
META_KEY = "affect.state"
_MAX_DRIVERS = 3


@dataclass
class DomainSurprise:
    domain: str
    surprise: float     # shrunk, non-negative contribution to the track
    deviation: float    # time-weighted window mean error − baseline (signed)
    baseline: float     # standing error_score before the window
    n: int              # settled bets in the window
    n_eff: float        # aged weight sum


@dataclass
class TrackReading:
    """One track's injection. ``injection is None`` means *no signal* (n/a),
    which is not the same as a quiet 0.0."""
    injection: float | None
    drivers: list = field(default_factory=list)


@dataclass
class SurpriseReading:
    since: datetime
    now: datetime
    environment: TrackReading
    model: TrackReading
    explicit_count: int
    corpus_count: int


def _is_explicit(record) -> bool:
    return (record.context or "").startswith("explicit:")


def _baselines(corpus) -> dict[str, float]:
    """Standing per-domain error from the health verdict (C1). Thin domains are
    dropped (C2); LOW_INFORMATION stays in as a baseline (D7)."""
    summaries = compute_calibration(corpus)
    health = assess_calibration_health(summaries, corpus)
    usable = {
        d.domain for d in health.domains if d.classification != SAMPLE_INSUFFICIENT
    }
    return {s.domain: s.error_score for s in summaries if s.domain in usable}


def _track(records, baselines: dict[str, float], now: datetime) -> TrackReading:
    per_domain: dict[str, list] = defaultdict(lambda: [0.0, 0.0, 0])
    for r in records:
        if r.domain not in baselines or r.score is None:
            continue
        age_h = max(0.0, (now - r.created_at).total_seconds() / 3600.0)
        w = math.exp(-_LAMBDA * age_h)  # T1: age by event time, not settle time
        acc = per_domain[r.domain]
        acc[0] += w * r.score
        acc[1] += w
        acc[2] += 1

    drivers = []
    for domain, (weighted, n_eff, n) in per_domain.items():
        if n_eff <= 0.0:
            continue
        deviation = weighted / n_eff - baselines[domain]
        surprise = max(0.0, deviation) * n_eff / (n_eff + N0)
        drivers.append(DomainSurprise(
            domain=domain, surprise=surprise, deviation=deviation,
            baseline=baselines[domain], n=n, n_eff=n_eff,
        ))
    drivers.sort(key=lambda d: d.surprise, reverse=True)
    # W1: domains add up on equal footing — volume only enters via shrinkage.
    return TrackReading(injection=sum(d.surprise for d in drivers), drivers=drivers)


def compute_surprise(corpus, window, *, since: datetime, now: datetime) -> SurpriseReading:
    """Pure surprise reading. ``corpus`` is the rolling reconciled corpus;
    ``window`` the subset settled since the last absorb."""
    window_ids = {r.id for r in window}
    standing = [r for r in corpus if r.id not in window_ids]
    baselines = _baselines(standing)

    explicit_count = sum(1 for r in corpus if _is_explicit(r))
    corpus_count = len(corpus)
    environment = _track([r for r in window if not _is_explicit(r)], baselines, now)
    dense = corpus_count > 0 and explicit_count / corpus_count >= EXPLICIT_SHARE_FLOOR
    model = (
        _track([r for r in window if _is_explicit(r)], baselines, now)
        if dense else TrackReading(injection=None)
    )
    return SurpriseReading(
        since=since, now=now, environment=environment, model=model,
        explicit_count=explicit_count, corpus_count=corpus_count,
    )


async def read_surprise(db, *, since: datetime, now: datetime) -> SurpriseReading:
    """Read-only: loads the rolling reconciled corpus and measures the window."""
    from loom.core.memory.prediction import PredictionStore

    corpus = await PredictionStore(db).list_by_status(
        "reconciled", limit=CORPUS_LIMIT, newest_first=True,
    )
    corpus = [r for r in corpus if r.score is not None]
    window = [
        r for r in corpus
        if r.reconciled_at is not None and since < r.reconciled_at <= now
    ]
    return compute_surprise(corpus, window, since=since, now=now)


def _clamp(x: float) -> float:
    return min(1.0, max(0.0, x))


@dataclass
class AffectState:
    environment: float = 0.0
    model: float | None = None  # None = n/a (no self-referring signal yet)
    t0: datetime | None = None

    def decayed(self, now: datetime) -> "AffectState":
        if self.t0 is None:
            return AffectState(self.environment, self.model, None)
        hours = max(0.0, (now - self.t0).total_seconds() / 3600.0)
        factor = math.exp(-_LAMBDA * hours)
        model = None if self.model is None else self.model * factor
        return AffectState(self.environment * factor, model, self.t0)

    def absorb(self, reading: SurpriseReading) -> "AffectState":
        base = self.decayed(reading.now)
        environment = _clamp(base.environment + (reading.environment.injection or 0.0))
        if reading.model.injection is None:
            model = None
        else:
            model = _clamp((base.model or 0.0) + reading.model.injection)
        return AffectState(environment, model, reading.now)

    def since_for(self, now: datetime) -> datetime:
        floor = now - timedelta(hours=LOOKBACK_HOURS)
        return floor if self.t0 is None or self.t0 < floor else self.t0

    def to_json(self) -> str:
        return json.dumps({
            "environment": self.environment,
            "model": self.model,
            "t0": self.t0.isoformat() if self.t0 else None,
        })

    @classmethod
    def from_json(cls, raw: str) -> "AffectState":
        data = json.loads(raw)
        t0 = datetime.fromisoformat(data["t0"]) if data.get("t0") else None
        model = data.get("model")
        return cls(
            environment=float(data.get("environment", 0.0)),
            model=None if model is None else float(model),
            t0=t0,
        )


async def load_state(db) -> AffectState:
    cur = await db.execute("SELECT value FROM memory_meta WHERE key = ?", (META_KEY,))
    row = await cur.fetchone()
    if not row or not row[0]:
        return AffectState()
    try:
        return AffectState.from_json(row[0])
    except (ValueError, TypeError, KeyError):
        return AffectState()  # corrupt → fresh; never block the dawn beat


async def save_state(db, state: AffectState) -> None:
    stamp = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO memory_meta(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (META_KEY, state.to_json(), stamp),
    )
    await db.commit()


async def settle_friction(db, *, now: datetime, commit: bool) -> tuple[AffectState, SurpriseReading]:
    """Absorb everything settled since the last commit. ``commit=False`` returns
    the projected state without advancing it (dry run)."""
    state = await load_state(db)
    reading = await read_surprise(db, since=state.since_for(now), now=now)
    settled = state.absorb(reading)
    if commit:
        await save_state(db, settled)
    return settled, reading


def _stamp(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


def _driver_line(drivers) -> str:
    shown = [d for d in drivers if d.surprise > 0.0][:_MAX_DRIVERS]
    if not shown:
        return "none"
    # Headline = the shrunk contribution (same quantity as the sort key and the
    # track total); raw deviation is background. Showing raw deviation made an
    # n=1 slip read ~6x larger than it counts (PR #578 review P2).
    return " · ".join(
        f"{d.domain} {d.surprise:+.2f} "
        f"(dev {d.deviation:+.2f} vs baseline {d.baseline:.2f}, n={d.n})"
        for d in shown
    )


def render_friction_note(state: AffectState, reading: SurpriseReading) -> str:
    """The Critic (I5): the one place a surprise reading becomes text."""
    ratio = f"explicit {reading.explicit_count}/{reading.corpus_count}"
    if state.model is None:
        model = f"n/a ({reading.explicit_count} explicit bets in corpus)"
        confidence = f"low ({ratio})"
    else:
        model = f"{state.model:.2f}"
        confidence = ratio

    if state.environment <= 0.0 and not state.model:
        attribution = "none"
    elif state.model is None or state.environment >= state.model:
        attribution = "environment"
    else:
        attribution = "model"

    lines = [
        "<environment_friction>",
        f"window: {_stamp(reading.since)} → {_stamp(reading.now)}",
        f"environment: {state.environment:.2f}",
        f"model: {model}",
        f"attribution: {attribution}",
        f"drivers: {_driver_line(reading.environment.drivers)}",
    ]
    if reading.model.injection is not None:
        lines.append(f"model_drivers: {_driver_line(reading.model.drivers)}")
    lines += [f"confidence: {confidence}", "</environment_friction>"]
    return "\n".join(lines)
