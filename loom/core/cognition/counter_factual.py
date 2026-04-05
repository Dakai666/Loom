"""
Counter-Factual Reflector — distills execution failures into anti-patterns.

When a tool with a corresponding SkillGenome entry fails with
``failure_type == "execution_error"``, this module fires an async LLM
reflection that asks "what should be avoided next time?" and writes the
answer back into memory as a durable anti-pattern.

Storage
-------
Two writes happen per reflection:

1. **SemanticMemory** — key ``skill:<name>:anti_pattern:<iso_timestamp>``
   Value is the raw anti-pattern text.  Survives session compression and
   is surfaced by ``recall()``.

2. **RelationalMemory** — two triples:
   - ``(skill:<name>, has_anti_pattern, <pattern>)``
   - ``(loom-self, should_avoid, <pattern>)``

   The ``loom-self`` triple is the one that feeds back into PromptStack /
   MemoryIndex and shapes the agent's future behaviour.

Fire-and-forget safety
-----------------------
``CounterFactualReflector.maybe_reflect()`` schedules reflection as a
background ``asyncio.Task``.  It never awaits the result and never
propagates exceptions — a reflection failure must not affect the tool
call that triggered it.

Integration
-----------
Called from ``LoomSession._on_trace()`` after the existing skill-genome
EMA update::

    if not result.success and result.failure_type == "execution_error":
        self._reflector.maybe_reflect(call, result, self.session_id)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, UTC
from typing import TYPE_CHECKING

from loom.core.memory.relational import RelationalEntry, RelationalMemory
from loom.core.memory.semantic import SemanticEntry, SemanticMemory

if TYPE_CHECKING:
    from loom.core.cognition.router import LLMRouter
    from loom.core.harness.middleware import ToolCall, ToolResult
    from loom.core.memory.procedural import ProceduralMemory

logger = logging.getLogger(__name__)

_REFLECTION_PROMPT = """\
A tool named "{tool_name}" failed during agent execution.

Error:
{error}

Arguments that were passed:
{args}

In one or two sentences, describe the specific pattern or assumption that \
led to this failure and should be avoided in future attempts. \
Be concrete — name the exact mistake, not a general principle.
Do NOT start with "I" or refer to yourself. \
Write in plain English as a direct instruction, e.g. \
"Avoid passing relative paths to X when Y is expected."
"""


class CounterFactualReflector:
    """
    Listens for ``execution_error`` failures on tracked skills and
    asynchronously generates anti-pattern entries written to memory.

    Parameters
    ----------
    router:
        The session LLMRouter — used to fire the reflection prompt.
    model:
        Model identifier passed to the router.
    procedural:
        ProceduralMemory — checked to confirm the failing tool has a
        SkillGenome entry (only named skills are reflected on).
    semantic:
        SemanticMemory — destination for the anti-pattern text.
    relational:
        RelationalMemory — destination for ``loom-self / skill`` triples.
    """

    def __init__(
        self,
        router: LLMRouter,
        model: str,
        procedural: ProceduralMemory,
        semantic: SemanticMemory,
        relational: RelationalMemory,
    ) -> None:
        self._router = router
        self._model = model
        self._procedural = procedural
        self._semantic = semantic
        self._relational = relational

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def maybe_reflect(
        self,
        call: ToolCall,
        result: ToolResult,
        session_id: str,
    ) -> None:
        """
        Fire-and-forget: schedule a background reflection task if the
        failure qualifies.

        Only ``execution_error`` failures on tools that have a
        corresponding SkillGenome entry are reflected on.
        ``permission_denied`` and ``timeout`` failures are excluded —
        those are environmental, not skill logic errors.
        """
        if result.failure_type != "execution_error":
            return

        # Schedule the async work without blocking the trace callback.
        task = asyncio.create_task(
            self._reflect(call, result, session_id),
            name=f"cfr:{call.tool_name}:{call.id[:8]}",
        )
        # Attach a done-callback to log unexpected failures silently.
        task.add_done_callback(self._on_task_done)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _on_task_done(task: asyncio.Task) -> None:
        """Log but never re-raise — reflection failure is non-fatal."""
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            logger.debug(
                "Counter-factual reflection failed silently: %s: %s",
                type(exc).__name__, exc,
            )

    async def _reflect(
        self,
        call: ToolCall,
        result: ToolResult,
        session_id: str,
    ) -> None:
        """Full async reflection pipeline — DB lookup → LLM → write."""
        # Only reflect on tools that have a SkillGenome entry.
        skill = await self._procedural.get(call.tool_name)
        if skill is None:
            return

        # Build and send the reflection prompt.
        prompt = _REFLECTION_PROMPT.format(
            tool_name=call.tool_name,
            error=result.error or "(no error message)",
            args=_fmt_args(call.args),
        )
        try:
            llm_response = await self._router.chat(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=120,
            )
            pattern = llm_response.content.strip()
        except Exception as exc:
            logger.debug("Reflection LLM call failed: %s", exc)
            return

        if not pattern:
            return

        # --- Write to SemanticMemory ---------------------------------
        ts = datetime.now(UTC).isoformat(timespec="seconds")
        sem_key = f"skill:{call.tool_name}:anti_pattern:{ts}"
        await self._semantic.upsert(
            SemanticEntry(
                key=sem_key,
                value=pattern,
                source=f"counter_factual:{session_id}",
                metadata={
                    "tool_name": call.tool_name,
                    "failure_type": result.failure_type,
                    "session_id": session_id,
                },
            )
        )

        # --- Write to RelationalMemory -------------------------------
        # (subject, predicate) pairs are unique — truncate pattern to
        # keep the predicate/object combo concise for the triples store.
        short = pattern[:200]

        await self._relational.upsert(
            RelationalEntry(
                subject=f"skill:{call.tool_name}",
                predicate="has_anti_pattern",
                object=short,
                source=f"counter_factual:{session_id}",
                metadata={"full_pattern": pattern, "session_id": session_id},
            )
        )
        # One triple per skill — predicate encodes the tool name so upsert
        # doesn't collapse all skills onto a single (loom-self, should_avoid) row.
        await self._relational.upsert(
            RelationalEntry(
                subject="loom-self",
                predicate=f"should_avoid:{call.tool_name}",
                object=short,
                source=f"counter_factual:{session_id}:{call.tool_name}",
                metadata={"tool_name": call.tool_name, "session_id": session_id},
            )
        )

        logger.debug(
            "Counter-factual reflection written for skill '%s': %s",
            call.tool_name, short[:80],
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_args(args: dict) -> str:
    """Format tool args as a compact, single-line summary."""
    if not args:
        return "(none)"
    parts = []
    for k, v in args.items():
        v_str = str(v)
        if len(v_str) > 80:
            v_str = v_str[:77] + "..."
        parts.append(f"{k}={v_str!r}")
    return ", ".join(parts)
