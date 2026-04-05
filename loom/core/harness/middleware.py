"""
Middleware Pipeline — the spine of Loom's harness layer.

Every tool call flows through this pipeline before and after execution.
Middleware is composable: add, remove, or reorder without touching tool code.

Execution order (outermost → innermost):
    LogMiddleware → TraceMiddleware → BlastRadiusMiddleware → tool handler
"""

import time
import uuid
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
