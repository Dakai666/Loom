"""
SubAgent — ephemeral child agent that runs a bounded task and returns a result.

Design principles:
  - Shares the parent's already-open DB connection (shared semantic memory).
  - Has its own session_id in session_log so its trace is isolated.
  - Persists child task, assistant messages and tool previews in session_log.
  - Tool scope: SAFE tools by default; parent can extend via allowed_tools whitelist.
  - GUARDED tools are auto-confirmed (no human in the loop for sub-agents).
  - CRITICAL tools are always blocked.
  - Bounded execution: stops at max_turns to prevent infinite loops.
  - Memory writes retain source="memorize" with child provenance in metadata.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.timezone import user_timestamp
from loom.core.ledger import current_turn_id

if TYPE_CHECKING:
    from loom.core.memory.session_log import SessionLog
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
    max_turns: int = 10                    # Cap to prevent runaway loops (ceiling 50); sub-agent may exit early via end_turn
    agent_id: str = field(default_factory=lambda: f"sub-{str(uuid.uuid4())[:6]}")


@dataclass
class SubAgentResult:
    """Result returned to the parent session after sub-agent completes.

    One exit rule (#648 P4): ``success`` ⇔ the loop reached ``end_turn`` and
    the final reply has text; ``output`` is that text. Any other exit
    (max_turns, max_tokens, an empty reply, no response) is a failure whose
    ``output`` is the most recent assistant text and whose ``error`` says why.
    """
    success: bool
    output: str
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
    parent_grants: "list[Any] | None" = None,
    # parent session's LedgerEmitter (#334). The subagent inherits the
    # parent's ``turn_id`` via contextvar, so its ``model_event`` events
    # file under the parent turn during replay. Caveat (#334 review S2):
    # contextvar propagation only holds within the same async task tree
    # (asyncio.create_task / gather / await chains). If a future iteration
    # spawns the subagent in a separate process or thread pool, the caller
    # must explicitly thread ``turn_id`` instead of relying on this kwarg.
    ledger_emitter: Any = None,
    session_log: "SessionLog | None" = None,
) -> SubAgentResult:
    """
    Run a sub-agent to completion and return a SubAgentResult.

    The sub-agent:
    - Gets its own session_id, with durable experience when session_log is wired
    - Filters the parent's tool registry by trust level + allowed_tools whitelist
    - Auto-confirms GUARDED tools; blocks CRITICAL tools
    - Stops at max_turns or when the LLM returns end_turn
    """
    from loom.core.harness.middleware import (
        MiddlewarePipeline, TraceMiddleware, BlastRadiusMiddleware,
        LifecycleMiddleware, LifecycleGateMiddleware,
        ToolCall, ToolResult,
    )
    from loom.core.harness.validation import SchemaValidationMiddleware
    from loom.core.harness.permissions import PermissionContext, TrustLevel

    agent_id = config.agent_id
    session_id = f"{parent_session_id}:{agent_id}"

    # ── Build filtered tool registry ─────────────────────────────────────────
    child_registry = ToolRegistry()

    for tool in tool_registry._tools.values():
        # CRITICAL tools are always blocked in sub-agents
        if tool.trust_level == TrustLevel.CRITICAL:
            continue
        # MCP tools (stdio_client subprocess) must be excluded — they are
        # not safe to run in a sub-agent's event-loop context (Issue #124+,
        # async generator cleanup race with stdio_client).
        # See: "an error occurred during closing of async generator stdio_client"
        if "mcp" in (tool.tags or []):
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
            # Trusted execution context, never an LLM argument. Preserve the
            # inherited executor and tag the FIRST governed write only.
            tagged = replace(call, metadata={
                **call.metadata,
                "memory_provenance": {"agent_id": agent_id, "parent_session_id": parent_session_id},
            })
            return await _orig_executor(tagged)

        child_registry.register(ToolDefinition(
            name="memorize",
            description=_orig_memorize.description,
            trust_level=_orig_memorize.trust_level,
            input_schema=_orig_memorize.input_schema,
            executor=_tagged_memorize,
            tags=_orig_memorize.tags,
        ))

    # ── Permission context: inherit parent's scope grants ─────────────────────
    # Sub-agents can only "spend" existing grants — they cannot request new
    # ones interactively (origin="subagent" → unattended deny).
    # Legacy-path tools (no scope_resolver) still need tool-name authorization,
    # so we authorize all tools in the child registry (already filtered above).
    perm = PermissionContext(session_id=session_id)
    for tool in child_registry._tools.values():
        perm.authorize(tool.name)
    if parent_grants:
        for g in parent_grants:
            perm.grant(g)

    async def _subagent_confirm(call: ToolCall) -> bool:
        # Should never be reached — _is_unattended catches origin="subagent"
        # before confirm_fn is called. Deny as safety net.
        return False

    pipeline = MiddlewarePipeline([
        LifecycleMiddleware(registry=child_registry),  # outer: lifecycle bookends
        TraceMiddleware(),
        SchemaValidationMiddleware(registry=child_registry),
        BlastRadiusMiddleware(
            perm_ctx=perm, confirm_fn=_subagent_confirm, registry=child_registry,
        ),
        LifecycleGateMiddleware(registry=child_registry),  # inner: lifecycle gating
    ])

    # ── Build initial messages ────────────────────────────────────────────────
    now_str = user_timestamp()
    available_tools = ", ".join(sorted(child_registry._tools.keys())) or "none"
    system_prompt = (
        f"You are a sub-agent (id: {agent_id}) spawned by a parent agent.\n"
        f"Your workspace is: {workspace}\n"
        f"You have a budget of {config.max_turns} turn(s).\n"
        f"Complete the following task and respond with a clear, concise result.\n"
        f"Do not ask clarifying questions — work with what you have.\n"
        f"\n"
        f"AVAILABLE TOOLS (this list is exhaustive — no other tools exist in "
        f"your environment):\n"
        f"  {available_tools}\n"
        f"If the task description names a tool not in this list (for example "
        f"`write_file` or `run_bash`), DO NOT attempt to call it. Treat the "
        f"task's wording as intent, not literal instruction: put "
        f"anything you would have written to a file or printed to the shell "
        f"into your final reply instead.\n"
        f"\n"
        f"Your final reply is the result handed to the parent agent: put "
        f"everything the parent needs in it."
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"[{now_str}]\n{config.task}"},
    ]

    tools_schema = child_registry.to_openai_schema()
    turns_used = 0
    total_tool_calls = 0
    final_output: str | None = None
    exit_reason = ""
    # The most recent non-empty assistant text (tool-use preambles included);
    # it is what the parent gets when there is no final reply.
    last_text = ""

    provenance: dict[str, Any] = {
        "agent_id": agent_id,
        "parent_session_id": parent_session_id,
    }
    if ledger_turn := current_turn_id():
        provenance["ledger_turn_id"] = ledger_turn

    async def _log_child(
        turn: int, role: str, content: str, *,
        metadata: dict[str, Any] | None = None, raw_json: str | None = None,
    ) -> None:
        if session_log is not None:
            await session_log.log_message(
                session_id, turn, role, content,
                {**provenance, **(metadata or {})}, raw_json=raw_json,
            )

    # Child provenance belongs to log rows, not the resumable main-session
    # registry. Keeping sessions untouched also preserves pulse's previous
    # main-session selection. Await each source write before continuing.
    await _log_child(1, "user", messages[1]["content"])

    # ── Agent loop ────────────────────────────────────────────────────────────
    while turns_used < config.max_turns:
        response: Any = None

        # Collect full response (no streaming needed for sub-agents)
        try:
            async for _chunk, final in router.stream_chat(
                model=config.model,
                messages=messages,
                tools=tools_schema,
                max_tokens=4096,
            ):
                if final is not None:
                    response = final
        except Exception as exc:  # CancelledError is a BaseException: it propagates
            detail = " ".join(str(exc).split())[:200]
            exit_reason = f"the model call raised {type(exc).__name__}" + (f" ({detail})" if detail else "")
            break

        if response is None:
            exit_reason = "the model returned no response"
            break

        # #334 — emit model_event for the subagent's LLM call. Subagent
        # inherits the parent turn_id via contextvar, so the call shows
        # up under the parent's turn during replay.
        if ledger_emitter is not None:
            try:
                from loom.core.ledger import ModelEventPayload
                _usage = {
                    "input_tokens": int(getattr(response, "input_tokens", 0) or 0),
                    "output_tokens": int(getattr(response, "output_tokens", 0) or 0),
                }
                await ledger_emitter.emit_model_event(
                    payload=ModelEventPayload(
                        model=config.model, tier=0, token_usage=_usage,
                    ),
                )
            except Exception:
                # Best-effort — never break the subagent loop on ledger emit.
                pass

        messages.append(response.raw_message)
        # Capture the model turn before executing tools. All tool rows from
        # this response share this index, even if the loop is later cancelled.
        child_turn = turns_used + 1
        raw = response.raw_message
        raw_content = raw.get("content")
        assistant_text = response.text
        if not assistant_text:
            if isinstance(raw_content, str):
                assistant_text = raw_content
            elif isinstance(raw_content, list):
                assistant_text = "\n".join(
                    block.get("text", "") for block in raw_content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
        await _log_child(
            child_turn, "assistant", assistant_text or ("[tool_use]" if response.tool_uses else ""),
            raw_json=json.dumps(raw, ensure_ascii=False),
        )

        if assistant_text.strip():
            last_text = assistant_text.strip()

        if response.stop_reason == "end_turn":
            turns_used += 1
            if assistant_text.strip():
                final_output = assistant_text.strip()
            else:
                exit_reason = "the final reply was empty"
            break

        if response.stop_reason == "tool_use":
            turns_used += 1
            for tu in response.tool_uses:
                tool_started = time.monotonic()
                total_tool_calls += 1
                tool_def = child_registry.get(tu.name)
                if tool_def is None:
                    available = sorted(child_registry._tools.keys())
                    tool_output = (
                        f"Unknown tool: {tu.name!r}. This tool is not in your "
                        f"available set; do not retry it. Available tools: "
                        f"{', '.join(available) or 'none'}."
                    )
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
                        capabilities=tool_def.capabilities,
                        session_id=session_id,
                        origin="subagent",
                    )
                    ts = time.monotonic()
                    result = await pipeline.execute(call, tool_def.executor)
                    result.duration_ms = (time.monotonic() - ts) * 1000

                tool_output = str(result.output) if result.success else (result.error or "")
                await _log_child(child_turn, "tool", tool_output[:500], metadata={
                    "tool_call_id": tu.id,
                    "tool_name": tu.name,
                    "success": result.success,
                    "failure_type": result.failure_type,
                    "duration_ms": (time.monotonic() - tool_started) * 1000,
                })
                messages.append(
                    router.format_tool_result(config.model, tu.id, tool_output, result.success)
                )
        else:
            turns_used += 1
            exit_reason = f"the model stopped with stop_reason={response.stop_reason!r}"
            break

    if final_output is not None:
        return SubAgentResult(
            success=True,
            output=final_output,
            agent_id=agent_id,
            turns_used=turns_used,
            tool_calls=total_tool_calls,
        )

    exit_reason = exit_reason or f"reached max_turns ({config.max_turns})"
    return SubAgentResult(
        success=False,
        output=last_text[-2000:],
        agent_id=agent_id,
        turns_used=turns_used,
        tool_calls=total_tool_calls,
        error=f"Sub-agent exited without a final reply: {exit_reason} after {turns_used} turn(s).",
    )
