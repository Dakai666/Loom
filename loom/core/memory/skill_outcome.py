"""
Skill activation tracking (Issue #56, re-scoped in Issue #120).

History
-------
Originally this module owned both:

1. Recording which skills were activated in a session (via ``load_skill``)
2. Triggering scalar 1-5 self-assessment at each ``TurnDone`` and folding
   the score into ``SkillGenome.confidence`` via EMA.

As of Issue #120 / PR 1, responsibility (2) moves to ``TaskReflector`` which
produces full structured ``TaskDiagnostic`` objects and still drives the
confidence EMA via ``quality_score``.  What remains here is responsibility
(1): the lightweight per-session tracker of *which* skills were activated
and *how many* tool calls happened in each turn.  ``TaskReflector`` consumes
this state through the ``drain_for_reflection`` / ``pop_turn_tool_count``
helpers exposed below.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loom.core.memory.procedural import ProceduralMemory
    from loom.core.memory.semantic import SemanticMemory

logger = logging.getLogger(__name__)


class SkillOutcomeTracker:
    """Per-session activation + tool-usage bookkeeping for skills.

    The tracker only records state.  ``TaskReflector`` (in
    ``loom.core.cognition.task_reflector``) reads this state at each
    ``TurnDone`` and produces structured diagnostics — it no longer
    lives here.
    """

    def __init__(
        self,
        procedural: "ProceduralMemory",
        semantic: "SemanticMemory",
        session_id: str,
    ) -> None:
        # ``procedural`` / ``semantic`` are retained for backward
        # compatibility with any callers that passed them through
        # ``LoomSession`` — they're unused by the tracker itself now,
        # but removing them would be a surface-breaking change for
        # platform code constructing this manually in tests.
        self._procedural = procedural
        self._semantic = semantic
        self._session_id = session_id

        # {skill_name: activation_turn_index}
        self._activated: dict[str, int] = {}
        self._turn_tool_count: int = 0

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_activation(self, skill_name: str, turn_index: int) -> None:
        """Record that a skill was activated (``load_skill`` called)."""
        self._activated[skill_name] = turn_index

    def record_tool_usage(self) -> None:
        """Increment the per-turn tool call counter."""
        self._turn_tool_count += 1

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    @property
    def activated_skills(self) -> list[str]:
        """Names of skills activated in this session (not yet drained)."""
        return list(self._activated.keys())

    def has_active_skills(self) -> bool:
        """True if any skill is currently pending reflection."""
        return bool(self._activated)

    # ------------------------------------------------------------------
    # Consumed by TaskReflector
    # ------------------------------------------------------------------

    def drain_for_reflection(self, turn_index: int) -> list[str]:
        """Return — and clear — skills activated in or before ``turn_index``.

        The drain semantics mirror the legacy ``maybe_evaluate`` path:
        each skill is reflected on once per activation, and the counter
        resets so the next activation starts a fresh evaluation window.
        """
        skills_to_eval = [
            name for name, act_turn in self._activated.items()
            if act_turn <= turn_index
        ]
        for name in skills_to_eval:
            del self._activated[name]
        return skills_to_eval

    def pop_turn_tool_count(self) -> int:
        """Return the current per-turn tool count and reset it to zero."""
        count = self._turn_tool_count
        self._turn_tool_count = 0
        return count
