"""
Skill Outcome Tracking — quality-gradient evaluation for skill usage.

Unlike tool-level success/failure, skills don't "fail" — agents always find
a way to complete the task.  What matters is quality, efficiency, and goal
alignment.  This module captures those dimensions through agent self-assessment
and updates SkillGenome confidence accordingly.

Architecture
------------
``SkillOutcomeTracker`` is instantiated per session and attached to
``LoomSession``.  It records skill activations (when ``load_skill`` is called)
and, on turn completion, triggers an async LLM self-assessment for skills
used in the turn.

The assessment result feeds into ``SkillGenome.confidence`` through a
normalised EMA (exponential moving average), replacing the old binary
success/failure model.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loom.core.cognition.router import LLMRouter
    from loom.core.memory.procedural import ProceduralMemory
    from loom.core.memory.semantic import SemanticMemory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SkillOutcome:
    """Quality-gradient assessment of a single skill usage."""
    skill_name: str
    session_id: str
    agent_score: int          # 1–5 from agent self-assessment
    efficiency_ratio: float   # tool_calls in the turn
    goal_summary: str         # natural language summary of goal alignment
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Self-assessment prompt
# ---------------------------------------------------------------------------

_SELF_ASSESS_PROMPT = """\
You just completed a task using the skill "{skill_name}".

Context of what you did:
{turn_summary}

Rate your execution on a scale of 1 to 5:
  1 = Poor: missed the goal, inefficient, many unnecessary steps
  2 = Below average: partially achieved the goal, significant room for improvement
  3 = Adequate: achieved the goal but with notable inefficiencies
  4 = Good: achieved the goal efficiently with minor improvements possible
  5 = Excellent: achieved the goal efficiently, following the skill's workflow closely

Respond with ONLY a JSON object (no markdown fencing, no explanation):
{{"score": <1-5>, "summary": "<one sentence describing what was achieved and how well>"}}
"""


# ---------------------------------------------------------------------------
# SkillOutcomeTracker
# ---------------------------------------------------------------------------

class SkillOutcomeTracker:
    """
    Tracks which skills are activated in a session and evaluates their
    usage quality through agent self-assessment.

    Lifecycle
    ---------
    1. ``record_activation(name, turn)`` — called when ``load_skill`` executes
    2. ``record_tool_usage()`` — called on each tool result in the turn
    3. ``maybe_evaluate()`` — called at end_turn; fires self-assessment if
       a skill was activated in this turn

    The tracker is designed to be lightweight: the LLM self-assessment call
    is fire-and-forget (background task) and never blocks the conversation.
    """

    # EMA smoothing factor for confidence updates
    ALPHA = 0.15

    def __init__(
        self,
        procedural: ProceduralMemory,
        semantic: SemanticMemory,
        session_id: str,
    ) -> None:
        self._procedural = procedural
        self._semantic = semantic
        self._session_id = session_id

        # Track {skill_name: activation_turn_index} for dedup and timing
        self._activated: dict[str, int] = {}
        # Per-turn tool usage counter
        self._turn_tool_count: int = 0

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_activation(self, skill_name: str, turn_index: int) -> None:
        """Record that a skill was activated (load_skill called)."""
        self._activated[skill_name] = turn_index

    def record_tool_usage(self) -> None:
        """Increment tool call counter for the current turn."""
        self._turn_tool_count += 1

    @property
    def activated_skills(self) -> list[str]:
        """Return names of skills activated in this session."""
        return list(self._activated.keys())

    def has_active_skills(self) -> bool:
        """True if any skill was activated this session."""
        return bool(self._activated)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def maybe_evaluate(
        self,
        router: LLMRouter,
        model: str,
        turn_index: int,
        turn_summary: str,
    ) -> None:
        """
        Fire-and-forget: schedule self-assessment if a skill was used.

        Only evaluates skills that were activated during this session.
        Each skill is evaluated once per turn-end after activation.
        """
        # Find skills activated in or before this turn that haven't been
        # evaluated yet (we mark them after scheduling).
        skills_to_eval = [
            name for name, act_turn in self._activated.items()
            if act_turn <= turn_index
        ]
        if not skills_to_eval:
            self._turn_tool_count = 0
            return

        tool_count = self._turn_tool_count
        for skill_name in skills_to_eval:
            task = asyncio.create_task(
                self._evaluate_skill(
                    router, model, skill_name, turn_summary, tool_count,
                ),
                name=f"skill_eval:{skill_name}:{turn_index}",
            )
            task.add_done_callback(_on_eval_done)

        # Clear activated skills so they aren't re-evaluated next turn
        for name in skills_to_eval:
            del self._activated[name]

        # Reset per-turn counter
        self._turn_tool_count = 0

    async def _evaluate_skill(
        self,
        router: LLMRouter,
        model: str,
        skill_name: str,
        turn_summary: str,
        tool_count: int,
    ) -> SkillOutcome | None:
        """Run LLM self-assessment and update SkillGenome confidence."""
        prompt = _SELF_ASSESS_PROMPT.format(
            skill_name=skill_name,
            turn_summary=turn_summary[:1500],  # cap to avoid huge prompts
        )

        try:
            response = await router.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
            )
            raw = (response.text or "").strip()
        except Exception as exc:
            logger.debug("Skill self-assessment LLM call failed: %s", exc)
            return None

        # Parse the JSON response
        score, summary = _parse_assessment(raw)
        if score is None:
            logger.debug("Could not parse self-assessment: %s", raw[:200])
            return None

        # Build outcome
        outcome = SkillOutcome(
            skill_name=skill_name,
            session_id=self._session_id,
            agent_score=score,
            efficiency_ratio=float(tool_count),
            goal_summary=summary,
        )

        # Update SkillGenome confidence with normalised score EMA
        skill = await self._procedural.get(skill_name)
        if skill is not None:
            normalised = score / 5.0  # → 0.0–1.0
            skill.confidence = (
                (1 - self.ALPHA) * skill.confidence + self.ALPHA * normalised
            )
            skill.usage_count += 1
            skill.success_rate = (
                (1 - self.ALPHA) * skill.success_rate + self.ALPHA * normalised
            )
            await self._procedural.upsert(skill)
            logger.debug(
                "Skill '%s' confidence updated to %.3f (score=%d/5)",
                skill_name, skill.confidence, score,
            )

        # Write outcome to semantic memory for future reference
        from loom.core.memory.semantic import SemanticEntry
        ts = datetime.now(UTC).isoformat(timespec="seconds")
        await self._semantic.upsert(SemanticEntry(
            key=f"skill:{skill_name}:outcome:{ts}",
            value=f"Score: {score}/5. {summary}",
            source=f"skill_eval:{self._session_id}",
            metadata={
                "skill_name": skill_name,
                "agent_score": score,
                "tool_count": tool_count,
            },
        ))

        return outcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_assessment(raw: str) -> tuple[int | None, str]:
    """Parse the LLM's JSON response into (score, summary).

    Handles three cases in order:
    1. Pure JSON (ideal path)
    2. Markdown-fenced JSON (```json ... ```)
    3. Natural language with embedded JSON object (MiniMax fallback)
    """
    cleaned = raw.strip()

    # Strip markdown code fencing if present
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    # Attempt 1: direct JSON parse
    try:
        data = _json.loads(cleaned)
        score = int(data.get("score", 0))
        summary = str(data.get("summary", ""))
        if 1 <= score <= 5:
            return score, summary
    except (ValueError, TypeError, _json.JSONDecodeError):
        pass

    # Attempt 2: extract first {...} block from mixed natural-language output
    # Handles models that ignore "ONLY a JSON object" instructions
    match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
    if match:
        try:
            data = _json.loads(match.group())
            score = int(data.get("score", 0))
            summary = str(data.get("summary", ""))
            if 1 <= score <= 5:
                return score, summary
        except (ValueError, TypeError, _json.JSONDecodeError):
            pass

    # Attempt 3: extract score integer and any quoted summary from raw text
    score_match = re.search(r'"score"\s*:\s*([1-5])', raw)
    summary_match = re.search(r'"summary"\s*:\s*"([^"]*)"', raw)
    if score_match:
        score = int(score_match.group(1))
        summary = summary_match.group(1) if summary_match else ""
        return score, summary

    return None, ""


def _on_eval_done(task: asyncio.Task) -> None:
    """Log but never re-raise — evaluation failure is non-fatal."""
    exc = task.exception() if not task.cancelled() else None
    if exc is not None:
        logger.debug(
            "Skill self-assessment failed silently: %s: %s",
            type(exc).__name__, exc,
        )
