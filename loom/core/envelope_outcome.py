"""Envelope outcome vocabulary and mechanical derivation (#421).

Lives in ``loom.core`` rather than under ``loom.platform`` because the
ledger projector — also a core component — needs to populate
``ExecutionEnvelopeView.outcome`` at projection time. The architecture
boundary (CLAUDE.md: ``Platform → Cognition → Harness → Memory``) means
core cannot import from platform; the outcome data contract therefore
belongs here next to the dataclass it instantiates.

``loom.platform.interaction_language`` re-exports ``EnvelopeOutcome``
so UI surfaces (CLI footer, Discord renderer) keep their existing
import shape. The mechanical helper stays unexported on the platform
side because only producers call it — renderers consume the strings,
they don't classify them.
"""
from __future__ import annotations

from enum import Enum


class EnvelopeOutcome(str, Enum):
    FULFILLED = "fulfilled"
    PARTIAL = "partial"
    UNFULFILLED = "unfulfilled"
    PIVOTED = "pivoted"
    ABORTED = "aborted"


# ``observed`` / ``validated`` / ``committed`` are intermediate
# success-y sub-states; today's ledger projector coarsens to terminal
# states so they rarely appear, but keeping them in the set means the
# helper stays correct if a future producer emits finer granularity.
_SUCCESS_STATES: frozenset[str] = frozenset(
    {"observed", "validated", "committed", "memorialized"}
)
# Mirrors what the renderer treats as "the batch didn't finish cleanly",
# but intentionally EXCLUDES ``"aborted"`` — aborted is its own category
# so the renderer can show 🛑 (intentional bail) instead of the generic
# ⚠ (something went wrong). The projector still counts aborted toward
# envelope ``status=="failed"`` separately; that asymmetry is documented
# at the projector's failure-count branch.
_FAILURE_STATES: frozenset[str] = frozenset(
    {"denied", "timed_out", "reverted", "failed"}
)


def derive_envelope_outcome(states: list[str]) -> str:
    """Map a list of terminal-ish node states to an EnvelopeOutcome value.

    Callers are responsible for only passing *terminal* states. Running
    envelopes should not call this — empty/in-flight inputs that aren't
    failures would otherwise return ``fulfilled`` by fall-through and
    paint a passing glyph on something still in flight. The ledger
    projector gates on ``status in ("completed", "failed")``; non-ledger
    code paths must do the equivalent.

    ``aborted`` is treated as its own category. When aborted mixes with
    real failures the result degrades to ``unfulfilled`` / ``partial``
    — the user mostly cares that the batch didn't finish cleanly, not
    which sub-flavour of "didn't finish" it was.

    PIVOTED is intentionally unreachable: pivoting means the agent
    changed strategy on purpose, which only the agent's own judgement
    can claim. Mechanical derivation never returns it.
    """
    if not states:
        return EnvelopeOutcome.FULFILLED.value
    lowered = [s.lower() for s in states]
    success_count = sum(1 for s in lowered if s in _SUCCESS_STATES)
    failure_count = sum(1 for s in lowered if s in _FAILURE_STATES)
    aborted_count = sum(1 for s in lowered if s == "aborted")
    if aborted_count and not success_count and not failure_count:
        return EnvelopeOutcome.ABORTED.value
    if failure_count or aborted_count:
        return (
            EnvelopeOutcome.PARTIAL.value
            if success_count
            else EnvelopeOutcome.UNFULFILLED.value
        )
    return EnvelopeOutcome.FULFILLED.value
