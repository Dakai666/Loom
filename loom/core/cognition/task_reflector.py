"""
Task Reflector — structured post-turn diagnosis for skill usage (Issue #120).

Replaces the scalar self-assessment path (``SkillOutcomeTracker.maybe_evaluate``)
with a full ``TaskDiagnostic`` that captures *what* went right/wrong and *how*
the SKILL.md could be mutated to improve.

Layering
--------
- ``SkillOutcomeTracker`` still tracks activations and per-turn tool usage.
- ``TaskReflector`` replaces the evaluation step: it consumes the tracker's
  state at each ``TurnDone``, reads the recent ``ExecutionEnvelopeView``
  history, and emits a structured diagnostic.
- The scalar ``quality_score`` (1.0-5.0) is preserved so the existing EMA
  update on ``SkillGenome.confidence`` continues to drive deprecation.

Fire-and-forget, same as the old path.  Subscribers receive the completed
diagnostic via async callbacks — the CLI/TUI/Discord each register one.

See Issue #120 for the full architecture plan (three-PR rollout).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, UTC
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from loom.core.cognition.router import LLMRouter
    from loom.core.cognition.skill_mutator import MutationProposal, SkillMutator
    from loom.core.events import ExecutionEnvelopeView
    from loom.core.memory.episodic import EpisodicMemory
    from loom.core.memory.procedural import ProceduralMemory
    from loom.core.memory.relational import RelationalMemory
    from loom.core.memory.semantic import SemanticMemory
    from loom.core.memory.skill_outcome import SkillOutcomeTracker

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Closed label set for task_type (avoids classification drift)
# ---------------------------------------------------------------------------

TASK_TYPES: tuple[str, ...] = (
    "long_analysis",
    "quick_qa",
    "code_review",
    "creative",
    "workflow_composite",
    "other",
)

# EMA smoothing factor for confidence updates — mirrors SkillOutcomeTracker.
_ALPHA: float = 0.15


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TaskDiagnostic:
    """Structured diagnosis of a single skill-driven turn.

    Produced by ``TaskReflector`` from the execution envelope + assistant
    turn summary + the current SKILL.md body.  Written to SemanticMemory
    under ``skill:{name}:diagnostic:{timestamp}``.
    """

    skill_name: str
    session_id: str
    turn_index: int
    task_type: str
    task_type_confidence: float
    instructions_followed: list[str]
    instructions_violated: list[str]
    failure_patterns: list[str]
    success_patterns: list[str]
    mutation_suggestions: list[str]
    quality_score: float  # 1.0 - 5.0
    envelope_ids: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_json(self) -> str:
        """Serialize to a canonical JSON string for SemanticMemory storage."""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "TaskDiagnostic":
        """Inverse of ``to_json`` — hydrate a diagnostic from storage."""
        data = json.loads(raw)
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        return cls(**data)

    def one_line_summary(self) -> str:
        """Compact summary for terminal / status-bar display."""
        suggestion = ""
        if self.mutation_suggestions:
            suggestion = self.mutation_suggestions[0][:60]
            if len(self.mutation_suggestions[0]) > 60:
                suggestion += "…"
        return (
            f"{self.skill_name} · {self.task_type} · {self.quality_score:.1f}/5"
            + (f" · {suggestion}" if suggestion else "")
        )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_REFLECT_PROMPT = """\
You just completed a task using the skill "{skill_name}".

SKILL.md (the instructions you were supposed to follow):
---
{skill_body}
---

Your final response on this turn:
---
{turn_summary}
---

Recent execution trajectory (tool calls, results, state transitions):
---
{envelope_summary}
---

Analyse your own execution against the SKILL.md instructions and output a \
structured diagnostic.  Be specific — reference actual phrases from the \
SKILL.md and actual steps from the trajectory, not generic claims.

Output ONLY a JSON object (no markdown fencing, no explanation):
{{
  "task_type": one of {task_type_options},
  "task_type_confidence": float 0.0-1.0,
  "instructions_followed": [strings, each citing a specific SKILL.md instruction you actually followed],
  "instructions_violated": [strings, each citing a specific SKILL.md instruction you ignored or misinterpreted],
  "failure_patterns": [strings, recurring failure modes observed in this execution],
  "success_patterns": [strings, effective approaches worth distilling],
  "mutation_suggestions": [strings, each a concrete SKILL.md edit like "add a step X after Y" or "clarify phrase Z to mean W"],
  "quality_score": float 1.0-5.0 (1=poor, 3=adequate, 5=excellent)
}}

Rules:
- Each list item must be under 200 characters.
- Empty list [] is allowed if nothing applies.
- "mutation_suggestions" must be directly applicable to the SKILL.md text, not abstract advice.
"""


# ---------------------------------------------------------------------------
# TaskReflector
# ---------------------------------------------------------------------------

DiagnosticSubscriber = Callable[[TaskDiagnostic], Awaitable[None]]
MutationSubscriber = Callable[["MutationProposal"], Awaitable[None]]


class TaskReflector:
    """Produces structured ``TaskDiagnostic`` objects at each TurnDone.

    Usage
    -----
    1. Instantiate once per session.
    2. Register subscribers with ``subscribe(cb)`` (TUI, Discord, CLI).
    3. At each TurnDone, call ``maybe_reflect(...)`` — fire-and-forget.

    Parameters
    ----------
    router, model:
        LLM router + model id, used for the diagnostic call.
    procedural:
        For updating ``SkillGenome.confidence`` via quality_score EMA.
    semantic:
        Destination for the serialized ``TaskDiagnostic`` JSON.
    relational, episodic:
        Optional — when present, a post-hook runs ``run_self_reflection``
        to preserve behavioural triple writes (subject=loom-self).
    session_id:
        Session identifier carried through to SemanticEntry.source.
    enabled:
        Master toggle (``loom.toml`` ``[reflection].auto_reflect``).
    visibility:
        ``off`` | ``summary`` | ``verbose`` — determines how much detail
        is broadcast to subscribers.  SemanticMemory writes happen in
        all modes (except ``off``, which disables everything).
    """

    MAX_ENVELOPE_NODES: int = 20       # cap nodes included in the prompt
    MAX_SKILL_BODY_CHARS: int = 3000   # avoid sending full SKILL.md if long
    MAX_TURN_SUMMARY_CHARS: int = 2000

    def __init__(
        self,
        router: "LLMRouter",
        model: str,
        procedural: "ProceduralMemory",
        semantic: "SemanticMemory",
        session_id: str,
        relational: "RelationalMemory | None" = None,
        episodic: "EpisodicMemory | None" = None,
        enabled: bool = True,
        visibility: str = "summary",
        mutator: "SkillMutator | None" = None,
    ) -> None:
        self._router = router
        self._model = model
        self._procedural = procedural
        self._semantic = semantic
        self._relational = relational
        self._episodic = episodic
        self._session_id = session_id
        self._enabled = enabled
        self._visibility = visibility if visibility in ("off", "summary", "verbose") else "summary"
        self._mutator = mutator
        self._subscribers: list[DiagnosticSubscriber] = []
        self._mutation_subscribers: list[MutationSubscriber] = []

    # ------------------------------------------------------------------
    # Subscriber API
    # ------------------------------------------------------------------

    def subscribe(self, callback: DiagnosticSubscriber) -> None:
        """Register a coroutine that receives each completed diagnostic."""
        self._subscribers.append(callback)

    def subscribe_mutation(self, callback: MutationSubscriber) -> None:
        """Register a coroutine that receives each generated ``MutationProposal``.

        Only fired when a ``SkillMutator`` is configured and its quality gate
        allows the candidate through.  Subscribers should treat it as a
        "candidate generated" audit hook, not a UI-blocking event.
        """
        self._mutation_subscribers.append(callback)

    @property
    def enabled(self) -> bool:
        return self._enabled and self._visibility != "off"

    @property
    def visibility(self) -> str:
        return self._visibility

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def maybe_reflect(
        self,
        tracker: "SkillOutcomeTracker",
        turn_index: int,
        turn_summary: str,
        envelopes: list["ExecutionEnvelopeView"],
    ) -> None:
        """Fire-and-forget — schedules one reflection task per active skill.

        Mirrors the semantics of the old ``SkillOutcomeTracker.maybe_evaluate``:
        one evaluation per (skill, turn) pair, after which the skill is
        cleared from the tracker's activation map.
        """
        if not self.enabled:
            tracker.drain_for_reflection(turn_index)
            return

        skills = tracker.drain_for_reflection(turn_index)
        if not skills:
            return

        tool_count = tracker.pop_turn_tool_count()
        for skill_name in skills:
            task = asyncio.create_task(
                self._reflect_one(
                    skill_name=skill_name,
                    turn_index=turn_index,
                    turn_summary=turn_summary,
                    envelopes=envelopes,
                    tool_count=tool_count,
                ),
                name=f"task_reflect:{skill_name}:{turn_index}",
            )
            task.add_done_callback(_on_reflect_done)

    # ------------------------------------------------------------------
    # Core pipeline
    # ------------------------------------------------------------------

    async def _reflect_one(
        self,
        skill_name: str,
        turn_index: int,
        turn_summary: str,
        envelopes: list["ExecutionEnvelopeView"],
        tool_count: int,
    ) -> TaskDiagnostic | None:
        """Run the LLM diagnostic, persist it, update confidence, notify."""
        skill = await self._procedural.get(skill_name)
        if skill is None:
            return None

        prompt = _REFLECT_PROMPT.format(
            skill_name=skill_name,
            skill_body=(skill.body or "")[: self.MAX_SKILL_BODY_CHARS],
            turn_summary=(turn_summary or "")[: self.MAX_TURN_SUMMARY_CHARS],
            envelope_summary=self._format_envelopes(envelopes),
            task_type_options=json.dumps(list(TASK_TYPES)),
        )

        raw = ""
        try:
            response = await self._router.chat(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=800,
            )
            raw = (response.text or "").strip()
        except Exception as exc:
            logger.debug("TaskReflector LLM call failed: %s", exc)
            return None

        parsed = _parse_diagnostic(raw)
        if parsed is None:
            logger.debug("TaskReflector could not parse: %s", raw[:200])
            return None

        diagnostic = TaskDiagnostic(
            skill_name=skill_name,
            session_id=self._session_id,
            turn_index=turn_index,
            task_type=parsed["task_type"],
            task_type_confidence=parsed["task_type_confidence"],
            instructions_followed=parsed["instructions_followed"],
            instructions_violated=parsed["instructions_violated"],
            failure_patterns=parsed["failure_patterns"],
            success_patterns=parsed["success_patterns"],
            mutation_suggestions=parsed["mutation_suggestions"],
            quality_score=parsed["quality_score"],
            envelope_ids=[e.envelope_id for e in envelopes[-3:]],
        )

        # Persist diagnostic + update confidence EMA.
        await self._persist(diagnostic, tool_count)

        # Post-hook: preserve loom-self behavioural triples (Issue #26).
        # Fire-and-forget so a slow behavioural pass doesn't block the
        # diagnostic callback chain.
        if self._relational is not None and self._episodic is not None:
            self._schedule_behavioural_triples()

        # Post-hook: skill mutation proposal (Issue #120 PR 2).  Fire-and-forget
        # so the candidate rewrite doesn't block the turn.  The scheduled task
        # re-fetches the parent skill so it picks up the EMA-updated version/row.
        if self._mutator is not None and self._mutator.should_propose(diagnostic):
            self._schedule_mutation_proposal(diagnostic)

        # Notify subscribers (TUI / Discord / CLI).
        await self._notify_subscribers(diagnostic)

        return diagnostic

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _persist(self, diagnostic: TaskDiagnostic, tool_count: int) -> None:
        """Write diagnostic to SemanticMemory and update SkillGenome EMA."""
        from loom.core.memory.semantic import SemanticEntry

        ts = diagnostic.timestamp.isoformat(timespec="seconds")
        await self._semantic.upsert(SemanticEntry(
            key=f"skill:{diagnostic.skill_name}:diagnostic:{ts}",
            value=diagnostic.to_json(),
            source=f"task_reflector:{self._session_id}",
            metadata={
                "skill_name": diagnostic.skill_name,
                "quality_score": diagnostic.quality_score,
                "task_type": diagnostic.task_type,
                "tool_count": tool_count,
                "turn_index": diagnostic.turn_index,
            },
        ))

        # EMA update on SkillGenome.confidence — identical semantics to the
        # old SkillOutcomeTracker path so deprecation behaviour is preserved.
        skill = await self._procedural.get(diagnostic.skill_name)
        if skill is not None:
            normalised = max(0.0, min(1.0, diagnostic.quality_score / 5.0))
            skill.confidence = (1 - _ALPHA) * skill.confidence + _ALPHA * normalised
            skill.success_rate = (1 - _ALPHA) * skill.success_rate + _ALPHA * normalised
            skill.usage_count += 1
            await self._procedural.upsert(skill)
            logger.debug(
                "Skill '%s' confidence updated to %.3f (quality=%.1f/5)",
                diagnostic.skill_name, skill.confidence, diagnostic.quality_score,
            )

    # ------------------------------------------------------------------
    # Behavioural post-hook
    # ------------------------------------------------------------------

    def _schedule_behavioural_triples(self) -> None:
        """Schedule ``run_self_reflection`` to write loom-self triples."""
        try:
            from loom.autonomy.self_reflection import run_self_reflection
        except Exception:
            return

        async def _llm(prompt: str) -> str:
            try:
                resp = await self._router.chat(
                    model=self._model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=512,
                )
                return resp.text or ""
            except Exception:
                return ""

        async def _run() -> None:
            try:
                await run_self_reflection(
                    episodic=self._episodic,  # type: ignore[arg-type]
                    relational=self._relational,  # type: ignore[arg-type]
                    llm_fn=_llm,
                    session_id=self._session_id,
                )
            except Exception as exc:
                logger.debug("Behavioural triples post-hook failed: %s", exc)

        task = asyncio.create_task(_run(), name=f"behavioural_triples:{self._session_id}")
        task.add_done_callback(_on_reflect_done)

    # ------------------------------------------------------------------
    # Mutation post-hook (Issue #120 PR 2)
    # ------------------------------------------------------------------

    def _schedule_mutation_proposal(self, diagnostic: TaskDiagnostic) -> None:
        """Schedule ``SkillMutator.propose_candidate`` + persist the candidate.

        The scheduled task re-reads the parent skill so its ``version``
        (and body) reflect the post-EMA row, then asks the mutator for a
        rewrite and calls ``ProceduralMemory.insert_candidate`` on success.
        Failures are swallowed to keep reflection non-fatal.
        """
        async def _run() -> None:
            try:
                parent = await self._procedural.get(diagnostic.skill_name)
                if parent is None:
                    return
                proposal = await self._mutator.propose_candidate(  # type: ignore[union-attr]
                    parent=parent,
                    diagnostic=diagnostic,
                    session_id=self._session_id,
                )
                if proposal is None:
                    return
                await self._procedural.insert_candidate(proposal.candidate)
                logger.debug(
                    "SkillMutator candidate generated: skill=%s parent_version=%d id=%s",
                    proposal.candidate.parent_skill_name,
                    proposal.candidate.parent_version,
                    proposal.candidate.id,
                )
                await self._notify_mutation_subscribers(proposal)
            except Exception as exc:
                logger.debug("Mutation post-hook failed: %s", exc)

        task = asyncio.create_task(
            _run(),
            name=f"mutation_proposal:{diagnostic.skill_name}:{diagnostic.turn_index}",
        )
        task.add_done_callback(_on_reflect_done)

    # ------------------------------------------------------------------
    # Subscriber dispatch
    # ------------------------------------------------------------------

    async def _notify_subscribers(self, diagnostic: TaskDiagnostic) -> None:
        if self._visibility == "off" or not self._subscribers:
            return
        for cb in list(self._subscribers):
            try:
                await cb(diagnostic)
            except Exception as exc:
                logger.debug("Diagnostic subscriber failed: %s", exc)

    async def _notify_mutation_subscribers(self, proposal: "MutationProposal") -> None:
        if self._visibility == "off" or not self._mutation_subscribers:
            return
        for cb in list(self._mutation_subscribers):
            try:
                await cb(proposal)
            except Exception as exc:
                logger.debug("Mutation subscriber failed: %s", exc)

    # ------------------------------------------------------------------
    # Envelope formatting
    # ------------------------------------------------------------------

    def _format_envelopes(self, envelopes: list["ExecutionEnvelopeView"]) -> str:
        """Compact, prompt-safe summary of recent envelopes."""
        if not envelopes:
            return "(no tool calls on this turn)"

        # Keep only the most recent ones; truncate node list per envelope.
        lines: list[str] = []
        for env in envelopes[-3:]:
            lines.append(
                f"[envelope {env.envelope_id}] status={env.status} "
                f"nodes={env.node_count} elapsed={env.elapsed_ms:.0f}ms"
            )
            for node in env.nodes[: self.MAX_ENVELOPE_NODES]:
                state = node.state or "?"
                err = f" err={node.error_snippet[:80]}" if node.error_snippet else ""
                args_prev = node.args_preview[:80] if node.args_preview else ""
                lines.append(
                    f"  · {node.tool_name} state={state} "
                    f"dur={node.duration_ms:.0f}ms{err}"
                    + (f" args={args_prev}" if args_prev else "")
                )
            if len(env.nodes) > self.MAX_ENVELOPE_NODES:
                lines.append(f"  … ({len(env.nodes) - self.MAX_ENVELOPE_NODES} more nodes)")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON parsing (defensive — mirrors other reflection modules)
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS: tuple[str, ...] = (
    "task_type",
    "task_type_confidence",
    "instructions_followed",
    "instructions_violated",
    "failure_patterns",
    "success_patterns",
    "mutation_suggestions",
    "quality_score",
)


def _parse_diagnostic(raw: str) -> dict[str, Any] | None:
    """Extract the JSON object, coerce fields, return None on failure."""
    text = raw.strip()

    # Strip markdown fencing if present.
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(l for l in lines if not l.strip().startswith("```")).strip()

    # Attempt 1: direct parse.
    data = _try_load(text)

    # Attempt 2: locate outermost {...}.
    if data is None:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            data = _try_load(match.group())

    if data is None or not isinstance(data, dict):
        return None

    # Coerce + validate required fields.
    try:
        task_type = str(data.get("task_type", "other")).strip()
        if task_type not in TASK_TYPES:
            task_type = "other"

        def _list(v: Any) -> list[str]:
            if not isinstance(v, list):
                return []
            return [str(x)[:200] for x in v if isinstance(x, (str, int, float)) and str(x).strip()]

        return {
            "task_type": task_type,
            "task_type_confidence": _clamp(
                float(data.get("task_type_confidence", 0.5)), 0.0, 1.0
            ),
            "instructions_followed": _list(data.get("instructions_followed")),
            "instructions_violated": _list(data.get("instructions_violated")),
            "failure_patterns": _list(data.get("failure_patterns")),
            "success_patterns": _list(data.get("success_patterns")),
            "mutation_suggestions": _list(data.get("mutation_suggestions")),
            "quality_score": _clamp(float(data.get("quality_score", 3.0)), 1.0, 5.0),
        }
    except (TypeError, ValueError):
        return None


def _try_load(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return None


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _on_reflect_done(task: asyncio.Task) -> None:
    """Log but never re-raise — reflection failure is non-fatal."""
    exc = task.exception() if not task.cancelled() else None
    if exc is not None:
        logger.debug(
            "TaskReflector background task failed silently: %s: %s",
            type(exc).__name__, exc,
        )
