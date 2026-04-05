"""
SubAgent — ephemeral child agent that runs a bounded task and returns a result.

Design principles:
  - Shares the parent's already-open DB connection (shared semantic memory).
  - Has its own session_id in episodic so its trace is isolated.
  - Does NOT write to session_log (no persistence — ephemeral by design).
  - Tool scope: SAFE tools by default; parent can extend via allowed_tools whitelist.
  - GUARDED tools are auto-confirmed (no human in the loop for sub-agents).
  - CRITICAL tools are always blocked.
  - Bounded execution: stops at max_turns to prevent infinite loops.
  - Memory writes carry source="agent:<agent_id>" for provenance tracking (5F-1).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.core.harness.registry import ToolDefinition
    from loom.core.memory.episodic import EpisodicMemory
    from loom.core.memory.semantic import SemanticMemory
    from loom.core.memory.procedural import ProceduralMemory
    from loom.core.cognition.router import LLMRouter


@dataclass
class SubAgentConfig:
    """Configuration for a single sub-agent invocation."""
    task: str                              # The task description / prompt
    model: str                             # Model to use (inherits from parent)
    allowed_tools: list[str] | None = None # None = SAFE tools only; list = whitelist
    max_turns: int = 10                    # Hard cap to prevent runaway loops
    agent_id: str = field(default_factory=lambda: f"sub-{str(uuid.uuid4())[:6]}")


@dataclass
class SubAgentResult:
    """Result returned to the parent session after sub-agent completes."""
    success: bool
    output: str          # Final assistant text (or error description)
    agent_id: str
    turns_used: int
    tool_calls: int
    error: str | None = None


async def run_subagent(
    config: SubAgentConfig,
    *,
    router: "LLMRouter",
    episodic: "EpisodicMemory",
    semantic: "SemanticMemory",
    procedural: "ProceduralMemory",
    tool_registry: Any,          # parent's ToolRegistry — we filter from this
    parent_session_id: str,
    workspace: Any,              # pathlib.Path
) -> SubAgentResult:
    """
    Run a sub-agent to completion and return a SubAgentResult.

    The sub-agent:
    - Gets its own ephemeral session_id (written to episodic, not session_log)
    - Filters the parent's tool registry by trust level + allowed_tools whitelist
    - Auto-confirms GUARDED tools; blocks CRITICAL tools
    - Stops at max_turns or when the LLM returns end_turn
    """
    from loom.core.harness.middleware import (
        MiddlewarePipeline, TraceMiddleware, BlastRadiusMiddleware,
        ToolCall, ToolResult,
    )
    from loom.core.harness.validation import SchemaValidationMiddleware
    from loom.core.harness.permissions import PermissionContext, TrustLevel
    from loom.core.memory.episodic import EpisodicEntry
    from loom.core.memory.semantic import SemanticEntry

    agent_id = config.agent_id
    session_id = f"{parent_session_id}:{agent_id}"

    # ── Build filtered tool registry ─────────────────────────────────────────
    from loom.core.harness.registry import ToolRegistry
    child_registry = ToolRegistry()

    for tool in tool_registry._tools.values():
        # CRITICAL tools are always blocked in sub-agents
        if tool.trust_level == TrustLevel.CRITICAL:
            continue
        # If a whitelist is given, only include tools on it
        if config.allowed_tools is not None and tool.name not in config.allowed_tools:
            continue
        # Default: SAFE only (no whitelist = restrict to SAFE)
        if config.allowed_tools is None and tool.trust_level != TrustLevel.SAFE:
            continue
        child_registry.register(tool)

    # Wrap memorize so writes are tagged with this agent's provenance
    _orig_memorize = child_registry.get("memorize")
    if _orig_memorize is not None:
        _orig_executor = _orig_memorize.executor

        async def _tagged_memorize(call: ToolCall) -> ToolResult:
            # Inject agent source into the call args context via a wrapped SemanticEntry
            result = await _orig_executor(call)
            # Re-tag: update the written entry's source to agent:<agent_id>
            key = call.args.get("key", "").strip()
            if key and result.success:
                entry = await semantic.get(key)
                if entry and entry.source == "agent":
                    entry.source = f"agent:{agent_id}"
                    await semantic.upsert(entry)
            return result

        from loom.core.harness.registry import ToolDefinition
        child_registry.register(ToolDefinition(
            name="memorize",
            description=_orig_memorize.description,
            trust_level=_orig_memorize.trust_level,
            input_schema=_orig_memorize.input_schema,
            executor=_tagged_memorize,
            tags=_orig_memorize.tags,
        ))

    # ── Permission context: pre-authorize SAFE + GUARDED (auto-confirm) ──────
    perm = PermissionContext(session_id=session_id)
    for tool in child_registry._tools.values():
        # Sub-agents auto-approve SAFE and GUARDED; CRITICAL is already excluded
        perm.authorize(tool.name)

    # ── Trace callback → episodic (child's own session trace) ────────────────
    async def on_trace(call: ToolCall, result: ToolResult) -> None:
        summary = (
            f"[sub:{agent_id}] Tool '{call.tool_name}' "
            f"({'ok' if result.success else 'fail'}, {result.duration_ms:.0f}ms)"
        )
        if result.error:
            summary += f" — {result.error}"
        meta: dict[str, Any] = {
            "tool_name": call.tool_name,
            "success": result.success,
            "agent_id": agent_id,
        }
        if result.failure_type:
            meta["failure_type"] = result.failure_type
        await episodic.write(EpisodicEntry(
            session_id=session_id,
            event_type="tool_result",
            content=summary,
            metadata=meta,
        ))

    async def auto_confirm(call: ToolCall) -> bool:
        # Sub-agents never prompt humans; GUARDED tools are auto-allowed
        return True

    pipeline = MiddlewarePipeline([
        TraceMiddleware(on_trace=on_trace),
        SchemaValidationMiddleware(registry=child_registry),
        BlastRadiusMiddleware(perm_ctx=perm, confirm_fn=auto_confirm),
    ])

    # ── Build initial messages ────────────────────────────────────────────────
    now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    system_prompt = (
        f"You are a sub-agent (id: {agent_id}) spawned by a parent agent.\n"
        f"Your workspace is: {workspace}\n"
        f"Complete the following task and respond with a clear, concise result.\n"
        f"Do not ask clarifying questions — work with what you have.\n"
        f"Available tools: {', '.join(child_registry._tools.keys()) or 'none'}."
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"[{now_str}]\n{config.task}"},
    ]

    tools_schema = child_registry.to_openai_schema()
    turns_used = 0
    total_tool_calls = 0
    final_output = ""

    # ── Agent loop ────────────────────────────────────────────────────────────
    while turns_used < config.max_turns:
        response: Any = None

        # Collect full response (no streaming needed for sub-agents)
        async for _chunk, final in router.stream_chat(
            model=config.model,
            messages=messages,
            tools=tools_schema,
            max_tokens=4096,
        ):
            if final is not None:
                response = final

        if response is None:
            break

        messages.append(response.raw_message)

        if response.stop_reason == "end_turn":
            turns_used += 1
            raw = response.raw_message
            if isinstance(raw.get("content"), str):
                final_output = raw["content"]
            elif isinstance(raw.get("content"), list):
                final_output = " ".join(
                    b.get("text", "") for b in raw["content"]
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            break

        if response.stop_reason == "tool_use":
            turns_used += 1
            for tu in response.tool_uses:
                total_tool_calls += 1
                tool_def = child_registry.get(tu.name)
                if tool_def is None:
                    tool_output = f"Unknown tool: {tu.name}"
                    result = ToolResult(
                        call_id=tu.id, tool_name=tu.name,
                        success=False, error=tool_output,
                        failure_type="tool_not_found",
                    )
                else:
                    call = ToolCall(
                        id=tu.id,
                        tool_name=tu.name,
                        args=tu.args,
                        trust_level=tool_def.trust_level,
                        session_id=session_id,
                    )
                    ts = time.monotonic()
                    result = await pipeline.execute(call, tool_def.executor)
                    result.duration_ms = (time.monotonic() - ts) * 1000

                tool_output = str(result.output) if result.success else (result.error or "")
                messages.append(
                    router.format_tool_result(config.model, tu.id, tool_output, result.success)
                )
        else:
            break

    if turns_used >= config.max_turns and not final_output:
        return SubAgentResult(
            success=False,
            output="",
            agent_id=agent_id,
            turns_used=turns_used,
            tool_calls=total_tool_calls,
            error=f"Sub-agent reached max_turns limit ({config.max_turns}) without completing.",
        )

    return SubAgentResult(
        success=True,
        output=final_output,
        agent_id=agent_id,
        turns_used=turns_used,
        tool_calls=total_tool_calls,
    )
