"""
Middleware Pipeline — the spine of Loom's harness layer.

Every tool call flows through this pipeline before and after execution.
Middleware is composable: add, remove, or reorder without touching tool code.

Execution order (outermost → innermost):
    LogMiddleware → TraceMiddleware → BlastRadiusMiddleware → tool handler
"""

import asyncio
import logging
import time
import uuid

_log = logging.getLogger(__name__)
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any, Awaitable, Callable

from .permissions import ToolCapability, TrustLevel

# ---------------------------------------------------------------------------
# Core data types
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """A single invocation of a registered tool, before execution."""
    tool_name: str
    args: dict[str, Any]
    trust_level: TrustLevel
    session_id: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)
    capabilities: ToolCapability = field(default_factory=lambda: ToolCapability.NONE)
    abort_signal: asyncio.Event | None = field(default=None, compare=False, repr=False)


# Structured failure categories — used for reflexive learning and failure analysis.
# Set on ToolResult.failure_type when success=False.
FAILURE_TYPES = {
    "tool_not_found",    # tool name not in registry
    "permission_denied", # trust level insufficient / user denied
    "timeout",           # execution exceeded time limit
    "execution_error",   # tool raised an exception at runtime
    "validation_error",  # bad/missing arguments
    "model_error",       # LLM API error during tool-related call
}


@dataclass
class ToolResult:
    """The outcome of a tool invocation, after execution."""
    call_id: str
    tool_name: str
    success: bool
    output: Any = None
    error: str | None = None
    failure_type: str | None = None   # one of FAILURE_TYPES when success=False
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


ToolHandler = Callable[[ToolCall], Awaitable[ToolResult]]

# ---------------------------------------------------------------------------
# Middleware base
# ---------------------------------------------------------------------------

class Middleware(ABC):
    """
    Base class for all Loom middleware.

    Implement `process(call, next)` to intercept tool calls.
    Call `await next(call)` to continue down the pipeline.
    """
    @abstractmethod
    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        ...

# ---------------------------------------------------------------------------
# Pipeline engine
# ---------------------------------------------------------------------------

class MiddlewarePipeline:
    """
    Builds and executes a composable middleware chain.

    Usage:
        pipeline = MiddlewarePipeline()
        pipeline.use(LogMiddleware(console))
        pipeline.use(TraceMiddleware(on_trace=memory.write_trace))
        result = await pipeline.execute(call, my_tool_handler)
    """

    def __init__(self, middlewares: list[Middleware] | None = None) -> None:
        self._middlewares: list[Middleware] = list(middlewares or [])

    def use(self, middleware: Middleware) -> "MiddlewarePipeline":
        self._middlewares.append(middleware)
        return self

    async def execute(self, call: ToolCall, handler: ToolHandler) -> ToolResult:
        def build_chain(index: int) -> ToolHandler:
            if index >= len(self._middlewares):
                return handler

            mw = self._middlewares[index]
            next_fn = build_chain(index + 1)

            async def wrapped(tc: ToolCall) -> ToolResult:
                return await mw.process(tc, next_fn)

            return wrapped

        return await build_chain(0)(call)

# ---------------------------------------------------------------------------
# Built-in middleware
# ---------------------------------------------------------------------------

class LogMiddleware(Middleware):
    """Logs every tool call and its outcome to a Rich console."""

    def __init__(self, console: Any) -> None:
        self._console = console

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        self._console.print(
            f"  [dim]~> tool[/dim] [bold]{call.tool_name}[/bold] "
            f"{call.trust_level.label}"
        )
        result = await next(call)
        status = "[green]ok[/green]" if result.success else "[red]!![/red]"
        self._console.print(
            f"  {status} [dim]{call.tool_name}[/dim] "
            f"[dim]{result.duration_ms:.0f}ms[/dim]"
        )
        if not result.success and result.error:
            self._console.print(f"  [red]  {result.error}[/red]")
        return result


class TraceMiddleware(Middleware):
    """
    Measures wall-clock execution time and fires an async callback after
    each tool call completes.  The callback is what connects the harness
    to the memory layer — every tool result is automatically recorded
    without the tool author needing to think about it.
    """

    def __init__(
        self,
        on_trace: Callable[[ToolCall, ToolResult], Awaitable[None]] | None = None,
    ) -> None:
        self._on_trace = on_trace

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        t0 = time.monotonic()
        result = await next(call)
        result.duration_ms = (time.monotonic() - t0) * 1000.0

        if self._on_trace is not None:
            await self._on_trace(call, result)

        return result


class BlastRadiusMiddleware(Middleware):
    """
    Guards GUARDED and CRITICAL tools by consulting a PermissionContext.
    If a tool is not pre-authorized, prompts the user for confirmation.

    `confirm_fn` is injected by the platform layer so the middleware
    stays platform-agnostic (CLI prompt vs. webhook vs. Telegram).

    `exec_escape_fn` is an optional callable injected by the platform layer
    when strict_sandbox is enabled.  It receives a ToolCall and returns True
    if the command would escape the workspace via absolute paths.  When it
    returns True, exec_auto pre-authorization is bypassed and the user is
    prompted even in auto mode.
    """

    def __init__(
        self,
        perm_ctx: Any,
        confirm_fn: Callable[[ToolCall], Awaitable[bool]],
        exec_escape_fn: Callable[[ToolCall], bool] | None = None,
    ) -> None:
        self._perm = perm_ctx
        self._confirm = confirm_fn
        self._exec_escape_fn = exec_escape_fn

    def _exec_auto_approved(self, call: ToolCall) -> bool:
        """
        Return True if exec_auto mode can skip confirmation for this call.

        Conditions (all must hold):
        1. User has toggled exec_auto on this session.
        2. The tool has EXEC capability (currently: run_bash).
        3. Either no escape-detector is wired, OR the command does not escape
           the workspace via absolute paths.
        """
        if not self._perm.exec_auto:
            return False
        if not (call.capabilities & ToolCapability.EXEC):
            return False
        if self._exec_escape_fn is not None and self._exec_escape_fn(call):
            return False   # escape detected — fall through to confirmation
        return True

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        if self._perm.is_authorized(call.tool_name, call.trust_level):
            return await next(call)

        # exec_auto: session-level pre-authorization for sandboxed shell commands
        if self._exec_auto_approved(call):
            return await next(call)

        allowed = await self._confirm(call)
        if not allowed:
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error="User denied tool execution.",
                failure_type="permission_denied",
            )

        # EXEC and AGENT_SPAN tools re-confirm on every call (like CRITICAL).
        # Other GUARDED tools are pre-authorized for the rest of this session.
        _high_risk = ToolCapability.EXEC | ToolCapability.AGENT_SPAN
        if (call.trust_level == TrustLevel.GUARDED
                and not (call.capabilities & _high_risk)):
            self._perm.authorize(call.tool_name)

        return await next(call)


# ---------------------------------------------------------------------------
# Action Lifecycle Middleware (Issue #42)
# ---------------------------------------------------------------------------

class LifecycleMiddleware(Middleware):
    """
    Outermost middleware that wraps every tool call in an ActionRecord
    and drives the full lifecycle state machine.

    Lifecycle flow:
    1. DECLARED — ActionRecord created from incoming ToolCall
    2. Inner pipeline runs (Authorization → Schema validation → Execution)
    3. OBSERVED — Raw result captured
    4. VALIDATED — post_validator called (if defined on ToolDefinition)
    5. COMMITTED or REVERTING → REVERTED (if validation fails + rollback_fn exists)
    6. MEMORIALIZED — on_lifecycle callback fires

    When no post_validator or rollback_fn is defined on the ToolDefinition,
    the lifecycle collapses to: DECLARED → OBSERVED → COMMITTED → MEMORIALIZED,
    preserving backward-compatible behavior.

    The ``on_state_change`` callback fires on each state transition for UI
    updates (e.g. TUI tool block state visualization).
    """

    def __init__(
        self,
        registry: Any,
        on_lifecycle: Callable[["ActionRecord"], Awaitable[None]] | None = None,
        on_state_change: Callable[["ActionRecord", str, str], Awaitable[None]] | None = None,
    ) -> None:
        from .lifecycle import ActionRecord, ActionIntent, ActionState
        self._registry = registry
        self._on_lifecycle = on_lifecycle
        self._on_state_change = on_state_change
        # Store references to avoid circular imports at call time
        self._ActionRecord = ActionRecord
        self._ActionIntent = ActionIntent
        self._ActionState = ActionState

    async def _fire_state_change(
        self, record: "ActionRecord", old_state: str, new_state: str
    ) -> None:
        if self._on_state_change is not None:
            try:
                await self._on_state_change(record, old_state, new_state)
            except Exception:
                pass  # state change notifications must never crash the pipeline

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        ActionRecord = self._ActionRecord
        ActionIntent = self._ActionIntent
        ActionState = self._ActionState

        # --- Build intent from tool definition ---
        tool_def = self._registry.get(call.tool_name)
        intent = ActionIntent(
            intent_summary=f"{call.tool_name}({', '.join(f'{k}=...' for k in call.args)})",
            scope=getattr(tool_def, "scope", "general") if tool_def else "general",
            preconditions=list(getattr(tool_def, "preconditions", [])) if tool_def else [],
        )

        # --- DECLARED ---
        record = ActionRecord(call=call, intent=intent)

        # --- Run inner pipeline (handles Authorization, Validation, Execution) ---
        result = await next(call)

        # Determine if it was denied (permission_denied) or failed
        if not result.success and result.failure_type == "permission_denied":
            old = record.state.value
            record.transition(ActionState.DENIED, reason=result.error)
            await self._fire_state_change(record, old, record.state.value)
            record.result = result
            # Memorialize denied actions
            old = record.state.value
            record.transition(ActionState.MEMORIALIZED, reason="denied")
            await self._fire_state_change(record, old, record.state.value)
            if self._on_lifecycle is not None:
                await self._on_lifecycle(record)
            return result

        if not result.success and result.failure_type == "validation_error":
            old = record.state.value
            record.transition(ActionState.AUTHORIZED, reason="passed blast radius")
            await self._fire_state_change(record, old, record.state.value)
            old = record.state.value
            record.transition(ActionState.ABORTED, reason=result.error)
            await self._fire_state_change(record, old, record.state.value)
            record.result = result
            old = record.state.value
            record.transition(ActionState.MEMORIALIZED, reason="validation_error")
            await self._fire_state_change(record, old, record.state.value)
            if self._on_lifecycle is not None:
                await self._on_lifecycle(record)
            return result

        if not result.success and result.failure_type == "tool_not_found":
            record.result = result
            old = record.state.value
            record.transition(ActionState.DENIED, reason="tool not found")
            await self._fire_state_change(record, old, record.state.value)
            old = record.state.value
            record.transition(ActionState.MEMORIALIZED, reason="tool_not_found")
            await self._fire_state_change(record, old, record.state.value)
            if self._on_lifecycle is not None:
                await self._on_lifecycle(record)
            return result

        if not result.success and result.failure_type == "timeout":
            old = record.state.value
            record.transition(ActionState.AUTHORIZED, reason="passed blast radius")
            await self._fire_state_change(record, old, record.state.value)
            old = record.state.value
            record.transition(ActionState.PREPARED)
            await self._fire_state_change(record, old, record.state.value)
            old = record.state.value
            record.transition(ActionState.EXECUTING)
            await self._fire_state_change(record, old, record.state.value)
            old = record.state.value
            record.transition(ActionState.TIMED_OUT, reason=result.error)
            await self._fire_state_change(record, old, record.state.value)
            record.result = result
            old = record.state.value
            record.transition(ActionState.MEMORIALIZED, reason="timed_out")
            await self._fire_state_change(record, old, record.state.value)
            if self._on_lifecycle is not None:
                await self._on_lifecycle(record)
            return result

        # --- Happy path: tool executed (success or execution_error) ---
        # Reconstruct lifecycle states retroactively (inner pipeline has
        # already executed by the time we get here).

        # DECLARED → AUTHORIZED
        old = record.state.value
        record.transition(ActionState.AUTHORIZED, reason="passed blast radius")
        await self._fire_state_change(record, old, record.state.value)

        # AUTHORIZED → PREPARED
        old = record.state.value
        record.transition(ActionState.PREPARED)
        await self._fire_state_change(record, old, record.state.value)

        # PREPARED → EXECUTING
        old = record.state.value
        record.transition(ActionState.EXECUTING)
        await self._fire_state_change(record, old, record.state.value)

        # EXECUTING → OBSERVED
        old = record.state.value
        record.transition(ActionState.OBSERVED)
        await self._fire_state_change(record, old, record.state.value)
        record.result = result

        # --- Post-validation (if post_validator is defined) ---
        post_validator = getattr(tool_def, "post_validator", None) if tool_def else None
        rollback_fn = getattr(tool_def, "rollback_fn", None) if tool_def else None

        if post_validator is not None and result.success:
            try:
                validated = await post_validator(call, result)
            except Exception as _val_exc:
                _log.warning(
                    "post_validator for %r raised unexpectedly: %s — treating as passed",
                    call.tool_name, _val_exc,
                )
                validated = True

            if validated:
                # OBSERVED → VALIDATED → COMMITTED
                old = record.state.value
                record.transition(ActionState.VALIDATED)
                await self._fire_state_change(record, old, record.state.value)
                old = record.state.value
                record.transition(ActionState.COMMITTED)
                await self._fire_state_change(record, old, record.state.value)
            else:
                # OBSERVED → VALIDATED (fail) → REVERTING → REVERTED
                old = record.state.value
                record.transition(ActionState.VALIDATED)
                await self._fire_state_change(record, old, record.state.value)

                if rollback_fn is not None:
                    old = record.state.value
                    record.transition(ActionState.REVERTING, reason="post-validation failed")
                    await self._fire_state_change(record, old, record.state.value)
                    try:
                        rb_result = await rollback_fn(call, result)
                        record.rollback_result = rb_result
                    except Exception as exc:
                        record.rollback_result = ToolResult(
                            call_id=call.id,
                            tool_name=call.tool_name,
                            success=False,
                            error=f"Rollback failed: {exc}",
                        )
                    old = record.state.value
                    record.transition(ActionState.REVERTED)
                    await self._fire_state_change(record, old, record.state.value)
                    # Modify the result to indicate rollback
                    result = ToolResult(
                        call_id=result.call_id,
                        tool_name=result.tool_name,
                        success=False,
                        error=f"Post-validation failed; action rolled back.",
                        failure_type="execution_error",
                        duration_ms=result.duration_ms,
                        metadata={**result.metadata, "rolled_back": True},
                    )
                    record.result = result
                else:
                    # No rollback_fn: validation failed but can't revert → commit anyway
                    old = record.state.value
                    record.transition(ActionState.COMMITTED)
                    await self._fire_state_change(record, old, record.state.value)
        else:
            # No post_validator → skip VALIDATED, go directly to COMMITTED
            old = record.state.value
            record.transition(ActionState.COMMITTED)
            await self._fire_state_change(record, old, record.state.value)

        # --- MEMORIALIZED ---
        old = record.state.value
        record.transition(ActionState.MEMORIALIZED)
        await self._fire_state_change(record, old, record.state.value)

        if self._on_lifecycle is not None:
            try:
                await self._on_lifecycle(record)
            except Exception:
                pass  # lifecycle callback must never crash the pipeline

        return result

