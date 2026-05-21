"""Envelope interaction vocabulary — outcome derivation + prompt contract.

Hosts core data the agent + producers + renderers share around an
``ExecutionEnvelopeView``:

- ``EnvelopeOutcome`` enum + ``derive_envelope_outcome`` helper for the
  mechanical fallback when no LLM-authored outcome is present (#421).
- ``INTERACTION_LANGUAGE_INSTRUCTIONS`` — the prompt-stack contract that
  asks the agent to author intent + outcome for multi-tool batches (#423).

Lives in ``loom.core`` rather than under ``loom.platform`` because both
the ledger projector AND ``PromptStack`` (cognition layer) need this
vocabulary, and core cannot import from platform per CLAUDE.md
(``Platform → Cognition → Harness → Memory``). The two concepts
co-locate because they're the producer/consumer ends of the same
envelope-language: the prompt asks the agent to write intent/outcome,
and the helpers classify outcomes mechanically when the agent omits
them. Keeping them in one file means future contract changes touch
one place.

``loom.platform.interaction_language`` re-exports the public surface so
UI consumers (Discord renderer, tests) keep their existing import shape.
"""
from __future__ import annotations

from enum import Enum


class EnvelopeOutcome(str, Enum):
    FULFILLED = "fulfilled"
    PARTIAL = "partial"
    UNFULFILLED = "unfulfilled"
    PIVOTED = "pivoted"
    ABORTED = "aborted"


# Prompt-stack contract layer (#423). Slots between the project's Agent.md
# and any active personality so the contract sits with project-level
# context but persona lenses can still adjust phrasing on top of it.
# Phrasing is intentionally compact — the layer appears in every turn's
# system prompt and shouldn't bloat the cache prefix. Future localization
# follows the same registry pattern as the label dicts in
# ``loom.platform.interaction_language``.
INTERACTION_LANGUAGE_INSTRUCTIONS = (
    "When you dispatch a multi-tool batch, provide a one-line intent before "
    "the batch starting with '▸ '. After the batch completes, start your "
    "outcome judgement line with one glyph: '✓ ' fulfilled, '◐ ' partial, "
    "'⚠ ' unfulfilled, '↪ ' pivoted, or '🛑 ' aborted. "
    "Single-tool calls do not need an intent header. Keep both lines short "
    "enough to display in one UI line."
)


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
