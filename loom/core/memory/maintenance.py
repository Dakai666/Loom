"""
Memory maintenance tools — Issue #149.

Houses the ``dream_cycle`` and ``memory_prune`` tool factories that used to
live in ``loom.extensibility.dreaming_plugin``. Both touch private memory
subsystems (``SemanticMemory`` / ``RelationalMemory``) and are conceptually
the read-and-synthesize / decay-management half of ``MemoryGovernor`` —
they belong in core memory, not in the plugin layer.

The core synthesis logic stays in ``loom.core.cognition.dreaming.dream_cycle``;
this module only adapts it to the ``ToolDefinition`` contract and the
``ToolCall`` / ``ToolResult`` types. ``LoomSession.start()`` registers both
factories alongside the other memory tools (``recall``, ``memorize``, ...).
"""

from __future__ import annotations

from typing import Awaitable, Callable, TYPE_CHECKING

from loom.core.harness.middleware import ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolDefinition

if TYPE_CHECKING:
    from loom.core.memory.relational import RelationalMemory
    from loom.core.memory.semantic import SemanticMemory


LLMFn = Callable[[list[dict]], Awaitable[str]]


def make_dream_cycle_tool(
    semantic: "SemanticMemory",
    relational: "RelationalMemory",
    llm_fn: LLMFn,
) -> ToolDefinition:
    """Build the ``dream_cycle`` ToolDefinition.

    Parameters
    ----------
    semantic, relational:
        Already-initialised memory subsystems (typically taken from
        ``LoomSession`` after ``MemoryGovernor`` is set up).
    llm_fn:
        Async callable that takes an OpenAI-style messages list and returns
        the assistant's text. ``LoomSession.start()`` wires this to its
        configured router + model.
    """
    from loom.core.cognition.dreaming import dream_cycle

    async def _executor(call) -> ToolResult:
        sample = int(call.args.get("sample_size", 15))
        dry_run = bool(call.args.get("dry_run", False))

        result = await dream_cycle(
            semantic=semantic,
            relational=relational,
            llm_fn=llm_fn,
            sample_size=sample,
            dry_run=dry_run,
        )

        lines = [
            "Dream cycle complete",
            f"  Facts sampled: {result['facts_sampled']}",
            f"  Triples found: {result['triples_found']}",
            f"  Triples written: {result['triples_written']}",
        ]
        if result["errors"]:
            lines.append(f"  Warnings: {'; '.join(result['errors'])}")
        if dry_run:
            lines.append("  (dry-run — nothing was written)")

        return ToolResult(
            call_id=call.id, tool_name=call.tool_name, success=True,
            output="\n".join(lines),
        )

    return ToolDefinition(
        name="dream_cycle",
        description=(
            "Run an offline dreaming cycle: sample random semantic facts, "
            "discover non-obvious connections via the LLM, and store the "
            "resulting insights as Relational triples (source='dreaming'). "
            "Use this when the autonomy scheduler triggers a background "
            "synthesis task."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sample_size": {
                    "type": "integer",
                    "description": "Number of random facts to sample (default 15, max 30).",
                    "default": 15,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, run the full cycle but skip writing to the DB.",
                    "default": False,
                },
            },
        },
        executor=_executor,
        trust_level=TrustLevel.SAFE,
    )


def make_memory_prune_tool(semantic: "SemanticMemory") -> ToolDefinition:
    """Build the ``memory_prune`` ToolDefinition.

    Decays semantic-memory entries below ``threshold`` effective confidence.
    The default 90-day half-life means a fact written at confidence 0.8
    drops below 0.1 after ~282 days without an update.
    """
    async def _executor(call) -> ToolResult:
        threshold = float(call.args.get("threshold", 0.1))
        dry_run = bool(call.args.get("dry_run", False))

        result = await semantic.prune_decayed(threshold=threshold, dry_run=dry_run)

        lines = [
            "Memory prune complete",
            f"  Examined : {result['examined']} facts",
            f"  Pruned   : {result['pruned']} (effective_confidence < {threshold})",
            f"  Retained : {result['retained']}",
        ]
        if dry_run:
            lines.append("  (dry-run — nothing was deleted)")

        return ToolResult(
            call_id=call.id, tool_name=call.tool_name, success=True,
            output="\n".join(lines),
        )

    return ToolDefinition(
        name="memory_prune",
        description=(
            "Remove semantic memory entries whose effective confidence has "
            "decayed below a threshold. Effective confidence uses a 90-day "
            "half-life: a fact with initial confidence 0.8 drops below 0.1 "
            "after ~282 days of no update. Use dry_run=true to preview what "
            "would be deleted."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "threshold": {
                    "type": "number",
                    "description": "Delete entries with effective_confidence below this value (default 0.1).",
                    "default": 0.1,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, report what would be pruned without deleting anything.",
                    "default": False,
                },
            },
        },
        executor=_executor,
        trust_level=TrustLevel.SAFE,
    )
