"""
Middleware Pipeline — the spine of Loom's harness layer.

Every tool call flows through this pipeline before and after execution.
Middleware is composable: add, remove, or reorder without touching tool code.

Execution order (outermost → innermost):
    LifecycleMiddleware → TraceMiddleware → SchemaValidationMiddleware
    → BlastRadiusMiddleware → LifecycleGateMiddleware → tool handler
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
    origin: str = "interactive"
    """
    Where this call originated: "interactive" (CLI/TUI/Discord user),
    "mcp" (MCP client), "autonomy" (daemon trigger), "subagent" (child agent).
    BlastRadiusMiddleware uses this to decide whether to prompt or deny.
    """


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

    Issue #45 Phase B: when a tool has a ``scope_resolver``, this middleware
    resolves the scope, writes it to ``call.metadata``, and uses
    ``PermissionContext.evaluate()`` for resource-level authorization.
    Tools without a resolver fall back to the legacy tool-name path.

    `confirm_fn` is injected by the platform layer so the middleware
    stays platform-agnostic (CLI prompt vs. webhook vs. Telegram).

    `exec_escape_fn` is an optional callable injected by the platform layer
    when strict_sandbox is enabled.  It receives a ToolCall and returns True
    if the command would escape the workspace via absolute paths.  When it
    returns True, exec_auto pre-authorization is bypassed and the user is
    prompted even in auto mode.

    `registry` is an optional ToolRegistry used to look up ``scope_resolver``
    per tool.  When absent, all tools take the legacy path.
    """

    def __init__(
        self,
        perm_ctx: Any,
        confirm_fn: "Callable[[ToolCall], Awaitable[bool | ConfirmDecision]]",
        exec_escape_fn: Callable[[ToolCall], bool] | None = None,
        registry: Any | None = None,
    ) -> None:
        self._perm = perm_ctx
        self._confirm = confirm_fn
        self._exec_escape_fn = exec_escape_fn
        self._registry = registry

    def _exec_auto_approved(self, call: ToolCall) -> bool:
        """
        Return True if exec_auto mode can skip confirmation for this call.

        .. deprecated:: Phase D (Issue #45)
            ``enable_exec_auto()`` now injects a scope grant, so tools with
            a ``scope_resolver`` are handled by the scope-aware path.  This
            method only fires for legacy tools (no resolver) with EXEC
            capability.  Remove once all EXEC tools have scope resolvers.

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

    # Origins where no human is available to answer confirmation prompts.
    _UNATTENDED_ORIGINS = frozenset({"autonomy", "subagent"})

    def _is_unattended(self, call: ToolCall) -> bool:
        """Return True if no human is available to answer prompts."""
        return call.origin in self._UNATTENDED_ORIGINS

    def _deny_unattended(self, call: ToolCall, verdict: str) -> ToolResult:
        """Return a denial ToolResult for unattended calls that need confirmation."""
        _log.info(
            "Denying %s (origin=%s, verdict=%s) — no human to confirm",
            call.tool_name, call.origin, verdict,
        )
        self._notify_lifecycle(call, False, f"unattended-deny ({call.origin}, {verdict})")
        call.metadata["user_decision"] = False
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=False,
            error=(
                f"Permission denied: {call.tool_name} requires scope authorization "
                f"not covered by existing grants. Origin '{call.origin}' cannot "
                f"request new permissions interactively."
            ),
            failure_type="permission_denied",
        )

    def _notify_lifecycle(self, call: ToolCall, result: bool, reason: str) -> None:
        """
        Write authorization decision to LifecycleContext (Issue #50).

        If the call carries a LifecycleContext (injected by LifecycleMiddleware),
        record the real-time auth result so the lifecycle state machine can
        transition DECLARED → AUTHORIZED (or → DENIED) at the moment the
        decision is actually made — not retroactively.

        When no LifecycleContext is present (e.g. sub-agent pipeline),
        this is a no-op — full backward compatibility.
        """
        from .lifecycle import LIFECYCLE_CTX_KEY
        ctx = call.metadata.get(LIFECYCLE_CTX_KEY)
        if ctx is not None:
            ctx.authorization_result = result
            ctx.authorization_reason = reason

    def _write_scope_metadata(
        self, call: ToolCall, scope_request: Any,
        diff: Any | None, verdict: Any,
    ) -> None:
        """Write scope resolution results to call.metadata for audit."""
        call.metadata["scope_request"] = scope_request
        call.metadata["scope_diff"] = diff
        call.metadata["scope_verdict"] = verdict

    # Informational constraint keys that should NOT be copied to grants.
    # These describe the *request* but are not actionable authorization limits.
    _INFORMATIONAL_CONSTRAINTS = frozenset({"scope_unknown", "has_absolute_paths"})

    @staticmethod
    def _normalize_decision(raw: "bool | ConfirmDecision") -> "ConfirmDecision":
        """Convert legacy bool confirm result to ConfirmDecision."""
        from .scope import ConfirmDecision
        if isinstance(raw, ConfirmDecision):
            return raw
        return ConfirmDecision.ONCE if raw else ConfirmDecision.DENY

    # Default session-level lease TTL: 30 minutes.
    _SCOPE_LEASE_TTL = 30 * 60

    def _request_to_grants(
        self, scope_request: Any, source: str = "manual_confirm",
        valid_until: float = 0.0,
    ) -> None:
        """Convert a scope request's requirements into grants on the PermissionContext."""
        from .scope import ScopeGrant
        import time
        now = time.time()
        for req in scope_request.requirements:
            # Filter out informational constraints — only preserve actionable
            # ones (remaining_budget, max_calls, etc.)
            grant_constraints = {
                k: v for k, v in req.constraints.items()
                if k not in self._INFORMATIONAL_CONSTRAINTS
            }
            self._perm.grant(ScopeGrant(
                resource=req.resource,
                action=req.action,
                selector=req.selector,
                constraints=grant_constraints,
                source=source,
                granted_at=now,
                valid_until=valid_until,
            ))

    # Sentinel to signal "resolver failed, use legacy path"
    _FALLBACK_TO_LEGACY = object()

    async def _scope_aware_process(
        self, call: ToolCall, next: ToolHandler, scope_resolver: Any,
    ) -> ToolResult | None | object:
        """
        Scope-aware authorization path (Issue #45 Phase B).

        Returns:
            ToolResult  — call was denied, return this result directly
            None        — authorization passed, caller should ``await next(call)``
            _FALLBACK_TO_LEGACY — resolver failed, caller should use legacy path
        """
        from .scope import PermissionVerdict as PV

        try:
            scope_request = scope_resolver(call)
        except Exception as exc:
            _log.warning(
                "scope_resolver for %r raised: %s — falling back to legacy",
                call.tool_name, exc,
            )
            return self._FALLBACK_TO_LEGACY

        diff = self._perm.diff(scope_request)
        verdict = self._perm.evaluate(scope_request, call.trust_level)
        self._write_scope_metadata(call, scope_request, diff, verdict)

        if verdict == PV.ALLOW:
            self._notify_lifecycle(call, True, f"scope-allow: {diff.reason.value}")
            return None  # proceed

        if verdict == PV.DENY:
            self._notify_lifecycle(call, False, "scope-deny")
            call.metadata["user_decision"] = False
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error="Permission denied by scope policy.",
                failure_type="permission_denied",
            )

        # CONFIRM or EXPAND_SCOPE — prompt user (or deny if unattended)
        if self._is_unattended(call):
            return self._deny_unattended(call, verdict.value)

        raw = await self._confirm(call)
        decision = self._normalize_decision(raw)

        from .scope import ConfirmDecision
        if decision == ConfirmDecision.DENY:
            self._notify_lifecycle(call, False, f"user denied ({verdict.value})")
            call.metadata["user_decision"] = False
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error=(
                    "USER ACTION: DENIED. This tool call was explicitly rejected by the "
                    "human user. Do not retry this action or equivalent substitutions "
                    "in this turn. Acknowledge and proceed with other tasks or wait for input."
                ),
                failure_type="permission_denied",
            )

        self._notify_lifecycle(call, True, f"user confirmed ({verdict.value}, {decision.value})")
        call.metadata["user_decision"] = True
        call.metadata["confirm_decision"] = decision.value

        # Convert request → grants based on user's decision.
        import time as _time
        if decision == ConfirmDecision.ONCE:
            # Session-permanent grant with tight scope, no TTL
            self._request_to_grants(scope_request, source="manual_confirm")
        elif decision == ConfirmDecision.SCOPE:
            # Session-scoped lease: grant with TTL
            self._request_to_grants(
                scope_request, source="lease",
                valid_until=_time.time() + self._SCOPE_LEASE_TTL,
            )
        elif decision == ConfirmDecision.AUTO:
            # Permanent grant: same scope as SCOPE but no TTL (never expires)
            self._request_to_grants(scope_request, source="auto_approve")

        return None  # proceed

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        # --- Scope-aware path (Issue #45 Phase B) ---
        # If registry is available and tool has a scope_resolver, use it.
        scope_resolver = None
        if self._registry is not None:
            tool_def = self._registry.get(call.tool_name)
            if tool_def is not None:
                scope_resolver = getattr(tool_def, "scope_resolver", None)

        if scope_resolver is not None:
            result = await self._scope_aware_process(call, next, scope_resolver)
            if result is self._FALLBACK_TO_LEGACY:
                pass  # fall through to legacy path below
            elif result is not None:
                return result  # short-circuited (denied)
            else:
                return await next(call)  # scope-aware ALLOW

        # --- Legacy path (tools without scope_resolver) ---
        if self._perm.is_authorized(call.tool_name, call.trust_level):
            self._notify_lifecycle(call, True, "pre-authorized")
            return await next(call)

        # exec_auto: session-level pre-authorization for sandboxed shell commands
        if self._exec_auto_approved(call):
            self._notify_lifecycle(call, True, "exec_auto")
            return await next(call)

        # Unattended origins cannot request new permissions
        if self._is_unattended(call):
            return self._deny_unattended(call, "legacy-not-authorized")

        raw = await self._confirm(call)
        decision = self._normalize_decision(raw)

        from .scope import ConfirmDecision
        if decision == ConfirmDecision.DENY:
            self._notify_lifecycle(call, False, "user denied")
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error=(
                    "USER ACTION: DENIED. This tool call was explicitly rejected by the "
                    "human user. Do not retry this action or equivalent substitutions "
                    "in this turn. Acknowledge and proceed with other tasks or wait for input."
                ),
                failure_type="permission_denied",
            )

        self._notify_lifecycle(call, True, f"user confirmed ({decision.value})")
        call.metadata["confirm_decision"] = decision.value

        # Legacy path has no scope_resolver — SCOPE/AUTO degrade to session
        # authorization (same as ONCE). Log so the user can see the fallback.
        from .scope import ConfirmDecision as _CD
        if decision in (_CD.SCOPE, _CD.AUTO):
            _log.debug(
                "SCOPE/AUTO chosen but no scope_resolver for %r — "
                "falling back to session authorization",
                call.tool_name,
            )
            call.metadata["legacy_decision_fallback"] = True

        # EXEC and AGENT_SPAN tools re-confirm on every call (like CRITICAL).
        # Other GUARDED tools are pre-authorized for the rest of this session.
        _high_risk = ToolCapability.EXEC | ToolCapability.AGENT_SPAN
        if (call.trust_level == TrustLevel.GUARDED
                and not (call.capabilities & _high_risk)):
            self._perm.authorize(call.tool_name)

        return await next(call)


# ---------------------------------------------------------------------------
# Action Lifecycle Middleware — two-layer architecture (Issue #42 + #50)
#
# The lifecycle is split into TWO cooperating middleware layers:
#
#   Pipeline order:
#     LifecycleMiddleware (outer)          ← DECLARED, post-OBSERVED states
#       → TraceMiddleware
#       → SchemaValidationMiddleware
#       → BlastRadiusMiddleware
#       → LifecycleGateMiddleware (inner)  ← AUTHORIZED → PREPARED → EXECUTING → OBSERVED
#         → handler (tool executor)
#
#   Why two layers?
#     A single middleware cannot intercept both BEFORE and AT the moment
#     the tool executor runs.  ``next(call)`` fires the entire remaining
#     chain as one opaque call — the outermost middleware has no way to
#     inject logic immediately before the handler.
#
#     The outer layer creates the ActionRecord and handles states that
#     occur after execution (validation, rollback, memorialized).
#     The inner layer sits right before the handler and fires EXECUTING
#     at the exact dispatch moment, with abort-signal racing.
#
#   They share state through LifecycleContext in call.metadata.
# ---------------------------------------------------------------------------


class LifecycleGateMiddleware(Middleware):
    """
    Inner lifecycle middleware — real-time control gates.

    Sits at the innermost position in the pipeline, just before the tool
    executor (handler).  When ``process(call, next)`` is called, ``next``
    is the actual tool executor function.

    Responsibilities:
        AUTHORIZED  — read from LifecycleContext (set by BlastRadiusMiddleware)
        PREPARED    — evaluate ToolDefinition.precondition_checks[]
        EXECUTING   — fire at the exact moment ``next(call)`` is invoked
                      race abort_signal for real-time abort
        OBSERVED    — fire when the executor returns

    When no LifecycleContext is present (e.g. sub-agent pipeline without
    LifecycleMiddleware), this middleware is a transparent pass-through.
    """

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        from .lifecycle import LIFECYCLE_CTX_KEY, ActionState

        ctx = call.metadata.get(LIFECYCLE_CTX_KEY)
        if ctx is None:
            # No lifecycle context → pass through (sub-agent, plugin, etc.)
            return await next(call)

        record = ctx.record
        tool_def = self._registry.get(call.tool_name)

        # ── AUTHORIZED ────────────────────────────────────────────────
        # BlastRadiusMiddleware already wrote authorization_result to ctx
        # at the moment the decision was made.  We now fire the state
        # transition.  If it didn't write (SAFE tool, auto-authorized),
        # treat as authorized.
        auth_reason = ctx.authorization_reason or "passed blast radius"
        await ctx.transition(ActionState.AUTHORIZED, reason=auth_reason)

        # ── PREPARED — callable precondition gates ────────────────────
        precondition_checks = (
            getattr(tool_def, "precondition_checks", []) if tool_def else []
        )
        precondition_descs = (
            getattr(tool_def, "preconditions", []) if tool_def else []
        )

        for i, check in enumerate(precondition_checks):
            try:
                passed = await check(call)
            except Exception as exc:
                _log.warning(
                    "precondition_check[%d] for %r raised: %s — treating as failed",
                    i, call.tool_name, exc,
                )
                passed = False

            if not passed:
                desc = (
                    precondition_descs[i]
                    if i < len(precondition_descs)
                    else f"precondition_check[{i}]"
                )
                await ctx.transition(
                    ActionState.ABORTED,
                    reason=f"Precondition failed: {desc}",
                )
                result = ToolResult(
                    call_id=call.id,
                    tool_name=call.tool_name,
                    success=False,
                    error=f"Precondition failed: {desc}",
                    failure_type="execution_error",
                )
                record.result = result
                await ctx.memorialize("precondition_failed")
                return result

        await ctx.transition(ActionState.PREPARED)

        # ── EXECUTING — fire at the exact dispatch moment ─────────────
        await ctx.transition(ActionState.EXECUTING)

        # Race executor against abort signal for real-time cancellation
        if call.abort_signal is not None and call.abort_signal.is_set():
            # Already aborted before we started
            await ctx.transition(
                ActionState.ABORTED, reason="abort signal (pre-execution)",
            )
            result = ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="Aborted before execution.",
                failure_type="execution_error",
            )
            record.result = result
            await ctx.memorialize("aborted")
            return result

        if call.abort_signal is not None:
            exec_task = asyncio.create_task(next(call))
            abort_task = asyncio.create_task(call.abort_signal.wait())
            done, pending = await asyncio.wait(
                {exec_task, abort_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()
                try:
                    await p
                except (asyncio.CancelledError, Exception):
                    pass

            if exec_task in done:
                exc = exec_task.exception()
                if exc is not None:
                    # Tool raised instead of returning ToolResult — convert and
                    # continue through OBSERVED so MEMORIALIZED always fires.
                    _log.warning(
                        "tool %r raised during abort-raced execution: %s",
                        call.tool_name, exc,
                    )
                    result = ToolResult(
                        call_id=call.id, tool_name=call.tool_name,
                        success=False, error=str(exc),
                        failure_type="execution_error",
                    )
                else:
                    result = exec_task.result()
            else:
                # Abort signal fired during execution
                await ctx.transition(
                    ActionState.ABORTED,
                    reason="abort signal during execution",
                )
                result = ToolResult(
                    call_id=call.id, tool_name=call.tool_name,
                    success=False, error="Execution aborted by signal.",
                    failure_type="execution_error",
                )
                record.result = result
                await ctx.memorialize("aborted")
                return result
        else:
            try:
                result = await next(call)
            except Exception as exc:
                # Tool raised instead of returning ToolResult — convert and
                # continue through OBSERVED so MEMORIALIZED always fires.
                _log.warning(
                    "tool %r raised during execution: %s", call.tool_name, exc,
                )
                result = ToolResult(
                    call_id=call.id, tool_name=call.tool_name,
                    success=False, error=str(exc),
                    failure_type="execution_error",
                )

        # ── OBSERVED — executor returned ──────────────────────────────
        # If the handler returned a timeout result, do NOT transition to
        # OBSERVED — leave at EXECUTING so the outer LifecycleMiddleware
        # can correctly drive EXECUTING → TIMED_OUT.
        if result.failure_type == "timeout":
            record.result = result
            return result

        await ctx.transition(ActionState.OBSERVED)
        record.result = result
        return result


class LifecycleMiddleware(Middleware):
    """
    Outer lifecycle middleware — ActionRecord bookends and post-execution flow.

    Sits at the outermost position in the middleware pipeline.  Wraps every
    tool call in an ActionRecord and drives the lifecycle state machine.

    Architecture (Issue #50: Control-first Lifecycle)
    -------------------------------------------------
    Phase 1 (Issue #42) stamped states retroactively after the inner
    pipeline completed.  Phase 2 makes each state a genuine gate by
    splitting the lifecycle into two cooperating middleware layers.

    This outer layer handles:
        DECLARED        — create ActionRecord, inject LifecycleContext
        post-OBSERVED   — VALIDATED, COMMITTED, REVERTING, REVERTED
        MEMORIALIZED    — trace written to audit / episodic
        failure paths   — DENIED, ABORTED (from inner middleware)

    The inner layer (LifecycleGateMiddleware) handles:
        AUTHORIZED → PREPARED → EXECUTING → OBSERVED

    ┌─────────────────────────────────────────────────────────────────┐
    │ LifecycleMiddleware (outer)                                     │
    │   DECLARED — ActionRecord created, LifecycleContext injected   │
    │                                                                 │
    │   ↓ inner pipeline                                             │
    │     TraceMiddleware → SchemaValidation → BlastRadius           │
    │                                                                 │
    │   ↓ LifecycleGateMiddleware (inner)                            │
    │     AUTHORIZED ← BlastRadius wrote to LifecycleContext         │
    │     PREPARED   ← precondition_checks[] evaluated               │
    │     EXECUTING  ← fires at exact dispatch + abort racing        │
    │     OBSERVED   ← executor returned                             │
    │                                                                 │
    │   ↑ back to this outer layer                                   │
    │   VALIDATED → COMMITTED  or  REVERTING → REVERTED              │
    │   MEMORIALIZED                                                  │
    └─────────────────────────────────────────────────────────────────┘

    Terminal failure states: DENIED, ABORTED, TIMED_OUT.

    Backward compatibility
    ----------------------
    Tools with no precondition_checks, post_validator, or rollback_fn
    follow: DECLARED → AUTHORIZED → PREPARED → EXECUTING → OBSERVED
    → COMMITTED → MEMORIALIZED — identical to Phase 1 happy path.
    """

    def __init__(
        self,
        registry: Any,
        on_lifecycle: Callable[["ActionRecord"], Awaitable[None]] | None = None,
        on_state_change: Callable[["ActionRecord", str, str], Awaitable[None]] | None = None,
    ) -> None:
        from .lifecycle import (
            ActionRecord, ActionIntent, ActionState,
            LifecycleContext, LIFECYCLE_CTX_KEY,
        )
        self._registry = registry
        self._on_lifecycle = on_lifecycle
        self._on_state_change = on_state_change
        # Store class references to avoid circular imports at call time
        self._ActionRecord = ActionRecord
        self._ActionIntent = ActionIntent
        self._ActionState = ActionState
        self._LifecycleContext = LifecycleContext
        self._CTX_KEY = LIFECYCLE_CTX_KEY

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        ActionRecord = self._ActionRecord
        ActionIntent = self._ActionIntent
        ActionState = self._ActionState

        # ── Build intent from tool definition ─────────────────────────
        tool_def = self._registry.get(call.tool_name)
        intent = ActionIntent(
            intent_summary=f"{call.tool_name}({', '.join(f'{k}=...' for k in call.args)})",
            scope=getattr(tool_def, "impact_scope", "general") if tool_def else "general",
            preconditions=list(getattr(tool_def, "preconditions", [])) if tool_def else [],
        )

        # ── DECLARED ──────────────────────────────────────────────────
        record = ActionRecord(call=call, intent=intent)

        # Inject LifecycleContext with shared callbacks
        ctx = self._LifecycleContext(
            record=record,
            _on_state_change=self._on_state_change,
            _on_lifecycle=self._on_lifecycle,
        )
        call.metadata[self._CTX_KEY] = ctx

        # ── Run inner pipeline ────────────────────────────────────────
        # The chain: Trace → SchemaValidation → BlastRadius →
        # LifecycleGateMiddleware → handler.
        #
        # LifecycleGateMiddleware drives AUTHORIZED → PREPARED →
        # EXECUTING → OBSERVED as real control gates.
        # BlastRadiusMiddleware writes auth decisions to ctx.
        result = await next(call)

        # ── Post-pipeline analysis ────────────────────────────────────
        # If the record already reached a terminal state (precondition
        # failure, abort during execution), everything is done.
        if record.is_terminal:
            return result

        # ── Handle failures from inner middleware ─────────────────────
        # These come from SchemaValidation or BlastRadius, BEFORE
        # LifecycleGateMiddleware ever ran.

        if not result.success and result.failure_type == "permission_denied":
            if record.state == ActionState.DECLARED:
                await ctx.transition(ActionState.DENIED, reason=result.error)
                record.result = result
                await ctx.memorialize("denied")
                return result

        if not result.success and result.failure_type == "tool_not_found":
            if record.state == ActionState.DECLARED:
                record.result = result
                await ctx.transition(ActionState.DENIED, reason="tool not found")
                await ctx.memorialize("tool_not_found")
                return result

        if not result.success and result.failure_type == "validation_error":
            # Schema validation failed AFTER authorization passed
            auth_reason = ctx.authorization_reason or "passed blast radius"
            await ctx.transition(ActionState.AUTHORIZED, reason=auth_reason)
            await ctx.transition(ActionState.ABORTED, reason=result.error)
            record.result = result
            await ctx.memorialize("validation_error")
            return result

        if not result.success and result.failure_type == "timeout":
            # Timeout from the handler (e.g. run_bash timeout).
            # LifecycleGateMiddleware already transitioned through
            # AUTHORIZED → PREPARED → EXECUTING.
            if record.state == ActionState.EXECUTING:
                await ctx.transition(ActionState.TIMED_OUT, reason=result.error)
                record.result = result
                await ctx.memorialize("timed_out")
                return result
            # Fallback: force through remaining states
            if not record.state.is_terminal:
                if record.state == ActionState.DECLARED:
                    await ctx.transition(ActionState.AUTHORIZED, reason="passed blast radius")
                if record.state == ActionState.AUTHORIZED:
                    await ctx.transition(ActionState.PREPARED)
                if record.state == ActionState.PREPARED:
                    await ctx.transition(ActionState.EXECUTING)
                await ctx.transition(ActionState.TIMED_OUT, reason=result.error)
                record.result = result
                await ctx.memorialize("timed_out")
            return result

        # ── Happy path: OBSERVED already fired by LifecycleGateMiddleware ─
        if record.state != ActionState.OBSERVED:
            # Safety fallback: LifecycleGateMiddleware did not run, which
            # means the pipeline is misconfigured.  Log at ERROR so the gap
            # is immediately visible rather than silently papered over.
            _log.error(
                "LifecycleGateMiddleware did not run for %r (state=%s) — "
                "pipeline may be misconfigured; stamping states retroactively",
                call.tool_name, record.state.value,
            )
            if record.state == ActionState.DECLARED:
                auth_reason = ctx.authorization_reason or "passed blast radius"
                await ctx.transition(ActionState.AUTHORIZED, reason=auth_reason)
            if record.state == ActionState.AUTHORIZED:
                await ctx.transition(ActionState.PREPARED)
            if record.state == ActionState.PREPARED:
                await ctx.transition(ActionState.EXECUTING)
            if record.state == ActionState.EXECUTING:
                await ctx.transition(ActionState.OBSERVED)
            record.result = result

        # ── Post-validation (if post_validator defined) ───────────────
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
                await ctx.transition(ActionState.VALIDATED)
                await ctx.transition(ActionState.COMMITTED)
            else:
                # OBSERVED → VALIDATED (fail) → REVERTING → REVERTED
                await ctx.transition(ActionState.VALIDATED)

                if rollback_fn is not None:
                    await ctx.transition(
                        ActionState.REVERTING, reason="post-validation failed",
                    )
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
                    await ctx.transition(ActionState.REVERTED)
                    # Modify the result to indicate rollback
                    result = ToolResult(
                        call_id=result.call_id,
                        tool_name=result.tool_name,
                        success=False,
                        error="Post-validation failed; action rolled back.",
                        failure_type="execution_error",
                        duration_ms=result.duration_ms,
                        metadata={**result.metadata, "rolled_back": True},
                    )
                    record.result = result
                else:
                    # No rollback_fn: can't revert → commit anyway
                    await ctx.transition(ActionState.COMMITTED)
        else:
            # No post_validator → skip VALIDATED, go directly to COMMITTED
            await ctx.transition(ActionState.COMMITTED)

        # ── MEMORIALIZED ──────────────────────────────────────────────
        await ctx.memorialize(record.state.value)

        return result
