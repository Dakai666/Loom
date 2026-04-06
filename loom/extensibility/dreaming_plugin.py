"""
DreamingPlugin — LoomPlugin wrapper for the offline dreaming cycle.

Belongs in the extensibility layer; the core cognition logic lives in
``loom.core.cognition.dreaming``.

Register via ``~/.loom/plugins/dreaming.py``::

    from loom.extensibility.dreaming_plugin import DreamingPlugin
    import loom
    loom.register_plugin(DreamingPlugin())

Or add an autonomy schedule in ``loom.toml`` and let the daemon call
the ``dream_cycle`` tool directly.
"""

from __future__ import annotations

from loom.extensibility.plugin import LoomPlugin


def _make_dream_cycle_tool(session):
    """Build a dream_cycle ToolDefinition wired to *session*."""
    from loom.core.cognition.dreaming import dream_cycle
    from loom.core.harness.registry import ToolDefinition
    from loom.core.harness.middleware import ToolResult
    from loom.core.harness.permissions import TrustLevel

    async def _dream_cycle_executor(call) -> ToolResult:
        sample = int(call.args.get("sample_size", 15))
        dry_run = bool(call.args.get("dry_run", False))

        async def llm_fn(messages):
            response = await session.router.chat(
                model=session.model,
                messages=messages,
                max_tokens=2048,
            )
            return response.text or ""

        sem = getattr(session, "_semantic", None)
        rel = getattr(session, "_relational", None)
        if sem is None or rel is None:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=False,
                error="Dreaming skipped — memory subsystems not available.",
            )

        result = await dream_cycle(
            semantic=sem,
            relational=rel,
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
            "discover non-obvious connections via the LLM, and store the resulting "
            "insights as Relational triples (source='dreaming'). "
            "Use this when the autonomy scheduler triggers a background synthesis task."
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
        executor=_dream_cycle_executor,
        trust_level=TrustLevel.SAFE,
    )


def _make_memory_prune_tool(session):
    """Build a memory_prune ToolDefinition wired to *session*."""
    from loom.core.harness.registry import ToolDefinition
    from loom.core.harness.middleware import ToolResult
    from loom.core.harness.permissions import TrustLevel

    async def _memory_prune_executor(call) -> ToolResult:
        threshold = float(call.args.get("threshold", 0.1))
        dry_run = bool(call.args.get("dry_run", False))

        sem = getattr(session, "_semantic", None)
        if sem is None:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=False,
                error="Memory prune skipped — semantic memory not available.",
            )

        result = await sem.prune_decayed(threshold=threshold, dry_run=dry_run)

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
            "Remove semantic memory entries whose effective confidence has decayed "
            "below a threshold. Effective confidence uses a 90-day half-life: a fact "
            "with initial confidence 0.8 drops below 0.1 after ~282 days of no update. "
            "Use dry_run=true to preview what would be deleted."
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
        executor=_memory_prune_executor,
        trust_level=TrustLevel.SAFE,
    )


class DreamingPlugin(LoomPlugin):
    """LoomPlugin that registers the ``dream_cycle`` tool."""

    name = "dreaming"
    version = "1.0"

    def __init__(self) -> None:
        self._tool_def = None

    def tools(self) -> list:
        # Tool is wired dynamically in on_session_start.
        return []

    def middleware(self) -> list:
        return []

    def lenses(self) -> list:
        return []

    def notifiers(self) -> list:
        return []

    def on_session_start(self, session) -> None:
        """Wire dream_cycle and memory_prune tools into the session's tool registry."""
        self._tool_def = _make_dream_cycle_tool(session)
        session.registry.register(self._tool_def)
        self._prune_tool_def = _make_memory_prune_tool(session)
        session.registry.register(self._prune_tool_def)

    def on_session_stop(self, session) -> None:
        pass
