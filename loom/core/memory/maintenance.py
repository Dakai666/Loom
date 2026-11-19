"""
Memory maintenance tools — Issue #149.

Houses the ``memory_prune`` tool factory (formerly in
``loom.extensibility.dreaming_plugin``) and the ``[memory.distill_dream]
execute`` gate reader shared by the autonomy daemon and
``loom memory big-dream``.

The ``dream_cycle`` / ``convergent_dream`` / ``distill_dream`` agent tools that
used to live here were retired in #639: dreaming runs on the daemon schedules
(``[memory.consolidation_dream]`` / ``[memory.distill_dream]``) and the one-off
``loom memory big-dream`` CLI. Archived under
``_archive/dream-entries-retired-2026-11-19/``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loom.core.harness.middleware import ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolDefinition

if TYPE_CHECKING:
    from loom.core.memory.semantic import SemanticMemory


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


def distill_execute_enabled(loom_config: dict) -> bool:
    """``[memory.distill_dream] execute`` — only a literal ``true`` enables writes."""
    cfg = (loom_config.get("memory") or {}).get("distill_dream") or {}
    return cfg.get("execute") is True
