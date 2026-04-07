"""
Tests for Control-first Action Lifecycle (Issue #50).

Verifies that lifecycle state transitions fire as real-time control gates,
not retroactive labels.  Covers both the outer LifecycleMiddleware and
inner LifecycleGateMiddleware.

References
----------
- loom/core/harness/middleware.py (LifecycleMiddleware, LifecycleGateMiddleware)
- loom/core/harness/lifecycle.py  (ActionState, ActionRecord, LifecycleContext)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from loom.core.harness.lifecycle import (
    ActionRecord,
    ActionState,
    LifecycleContext,
    LIFECYCLE_CTX_KEY,
)
from loom.core.harness.middleware import (
    BlastRadiusMiddleware,
    LifecycleGateMiddleware,
    LifecycleMiddleware,
    MiddlewarePipeline,
    ToolCall,
    ToolResult,
)
from loom.core.harness.permissions import PermissionContext, TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.harness.validation import SchemaValidationMiddleware


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry(*tools: ToolDefinition) -> ToolRegistry:
    """Build a ToolRegistry with pre-registered tools."""
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def _make_call(tool_name: str = "echo", **kwargs) -> ToolCall:
    return ToolCall(
        tool_name=tool_name,
        args=kwargs.get("args", {}),
        trust_level=kwargs.get("trust_level", TrustLevel.SAFE),
        session_id="test-session",
        abort_signal=kwargs.get("abort_signal", None),
    )


async def _echo_handler(call: ToolCall) -> ToolResult:
    """Trivial handler that echoes args."""
    return ToolResult(
        call_id=call.id, tool_name=call.tool_name,
        success=True, output=call.args,
    )


async def _slow_handler(call: ToolCall) -> ToolResult:
    """Handler that takes a long time (for abort testing)."""
    await asyncio.sleep(10)
    return ToolResult(
        call_id=call.id, tool_name=call.tool_name,
        success=True, output="done",
    )


async def _failing_handler(call: ToolCall) -> ToolResult:
    """Handler that returns an error."""
    return ToolResult(
        call_id=call.id, tool_name=call.tool_name,
        success=False, error="tool exploded",
        failure_type="execution_error",
    )


async def _timeout_handler(call: ToolCall) -> ToolResult:
    """Handler that returns a timeout result."""
    return ToolResult(
        call_id=call.id, tool_name=call.tool_name,
        success=False, error="timed out after 30s",
        failure_type="timeout",
    )


def _build_pipeline(
    registry: ToolRegistry,
    confirm_fn=None,
    on_lifecycle=None,
    on_state_change=None,
) -> MiddlewarePipeline:
    """
    Build a production-like pipeline:
    LifecycleMiddleware → SchemaValidation → BlastRadius → LifecycleGateMiddleware
    """
    perm = PermissionContext(session_id="test-session")
    if confirm_fn is None:
        confirm_fn = AsyncMock(return_value=True)
    return MiddlewarePipeline([
        LifecycleMiddleware(
            registry=registry,
            on_lifecycle=on_lifecycle,
            on_state_change=on_state_change,
        ),
        SchemaValidationMiddleware(registry=registry),
        BlastRadiusMiddleware(perm_ctx=perm, confirm_fn=confirm_fn),
        LifecycleGateMiddleware(registry=registry),
    ])


# ---------------------------------------------------------------------------
# LifecycleContext unit tests
# ---------------------------------------------------------------------------

class TestLifecycleContext:
    def test_defaults(self):
        """LifecycleContext fields default to None."""
        record = MagicMock()
        ctx = LifecycleContext(record=record)
        assert ctx.authorization_result is None
        assert ctx.authorization_reason is None

    @pytest.mark.asyncio
    async def test_transition_fires_callback(self):
        """transition() calls _on_state_change and updates record."""
        record = MagicMock()
        record.state = ActionState.DECLARED
        record.transition = MagicMock()

        cb = AsyncMock()
        ctx = LifecycleContext(record=record, _on_state_change=cb)
        await ctx.transition(ActionState.AUTHORIZED, reason="test")

        record.transition.assert_called_once_with(ActionState.AUTHORIZED, reason="test")
        cb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_transition_tolerates_callback_error(self):
        """transition() must not crash if the callback raises."""
        record = MagicMock()
        record.state = ActionState.DECLARED
        record.transition = MagicMock()

        cb = AsyncMock(side_effect=RuntimeError("boom"))
        ctx = LifecycleContext(record=record, _on_state_change=cb)
        # Should not raise
        await ctx.transition(ActionState.AUTHORIZED)

    @pytest.mark.asyncio
    async def test_memorialize_fires_both_callbacks(self):
        """memorialize() fires state change AND on_lifecycle."""
        record = MagicMock()
        record.state = ActionState.COMMITTED
        record.transition = MagicMock()

        state_cb = AsyncMock()
        lifecycle_cb = AsyncMock()
        ctx = LifecycleContext(
            record=record,
            _on_state_change=state_cb,
            _on_lifecycle=lifecycle_cb,
        )
        await ctx.memorialize("committed")
        lifecycle_cb.assert_awaited_once_with(record)


# ---------------------------------------------------------------------------
# Happy path: full pipeline
# ---------------------------------------------------------------------------

class TestHappyPath:
    @pytest.mark.asyncio
    async def test_states_ordered(self):
        """
        Happy path: states fire in order
        DECLARED → AUTHORIZED → PREPARED → EXECUTING → OBSERVED → COMMITTED → MEMORIALIZED
        """
        states: list[tuple[str, str]] = []

        async def track_state(record, old, new):
            states.append((old, new))

        tool = ToolDefinition(
            name="echo", description="echo tool",
            input_schema={}, executor=_echo_handler,
            trust_level=TrustLevel.SAFE,
        )
        reg = _make_registry(tool)
        pipeline = _build_pipeline(reg, on_state_change=track_state)
        call = _make_call("echo")

        result = await pipeline.execute(call, _echo_handler)
        assert result.success

        expected = [
            ("declared", "authorized"),
            ("authorized", "prepared"),
            ("prepared", "executing"),
            ("executing", "observed"),
            ("observed", "committed"),
            ("committed", "memorialized"),
        ]
        assert states == expected

    @pytest.mark.asyncio
    async def test_lifecycle_callback_fires(self):
        """on_lifecycle callback fires at MEMORIALIZED."""
        lifecycle_cb = AsyncMock()
        tool = ToolDefinition(
            name="echo", description="echo tool",
            input_schema={}, executor=_echo_handler,
            trust_level=TrustLevel.SAFE,
        )
        reg = _make_registry(tool)
        pipeline = _build_pipeline(reg, on_lifecycle=lifecycle_cb)
        call = _make_call("echo")

        await pipeline.execute(call, _echo_handler)
        lifecycle_cb.assert_awaited_once()
        record = lifecycle_cb.call_args[0][0]
        assert isinstance(record, ActionRecord)

    @pytest.mark.asyncio
    async def test_backward_compat_no_preconditions(self):
        """Tools with no preconditions/validators behave identically to Phase 1."""
        tool = ToolDefinition(
            name="simple", description="no frills",
            input_schema={}, executor=_echo_handler,
            trust_level=TrustLevel.SAFE,
        )
        reg = _make_registry(tool)
        pipeline = _build_pipeline(reg)
        call = _make_call("simple")

        result = await pipeline.execute(call, _echo_handler)
        assert result.success
        # Should complete without error — no preconditions, no validators


# ---------------------------------------------------------------------------
# AUTHORIZED driven by BlastRadiusMiddleware
# ---------------------------------------------------------------------------

class TestAuthorizedGate:
    @pytest.mark.asyncio
    async def test_authorized_from_blast_radius_pre_auth(self):
        """SAFE tools get AUTHORIZED with 'pre-authorized' reason."""
        states = []

        async def track(record, old, new):
            if new == "authorized":
                ctx = record.call.metadata.get(LIFECYCLE_CTX_KEY)
                states.append(ctx.authorization_reason)

        tool = ToolDefinition(
            name="safe_tool", description="",
            input_schema={}, executor=_echo_handler,
            trust_level=TrustLevel.SAFE,
        )
        reg = _make_registry(tool)
        pipeline = _build_pipeline(reg, on_state_change=track)
        call = _make_call("safe_tool")

        await pipeline.execute(call, _echo_handler)
        assert states == ["pre-authorized"]

    @pytest.mark.asyncio
    async def test_denied_fires_from_blast_radius(self):
        """When user denies, DENIED state is driven by BlastRadius."""
        states = []

        async def track(record, old, new):
            states.append(new)

        tool = ToolDefinition(
            name="danger", description="",
            input_schema={}, executor=_echo_handler,
            trust_level=TrustLevel.GUARDED,
        )
        reg = _make_registry(tool)
        confirm_fn = AsyncMock(return_value=False)
        pipeline = _build_pipeline(reg, confirm_fn=confirm_fn, on_state_change=track)
        call = _make_call("danger", trust_level=TrustLevel.GUARDED)

        result = await pipeline.execute(call, _echo_handler)
        assert not result.success
        assert result.failure_type == "permission_denied"
        assert "denied" in states
        assert "memorialized" in states

    @pytest.mark.asyncio
    async def test_blast_radius_no_lifecycle_context(self):
        """BlastRadiusMiddleware works fine without LifecycleContext (sub-agent)."""
        perm = PermissionContext(session_id="test-session")
        blast = BlastRadiusMiddleware(
            perm_ctx=perm,
            confirm_fn=AsyncMock(return_value=True),
        )
        # No LifecycleMiddleware to inject context
        call = _make_call("test_tool", trust_level=TrustLevel.SAFE)
        result = await blast.process(call, _echo_handler)
        assert result.success


# ---------------------------------------------------------------------------
# PREPARED — precondition gates
# ---------------------------------------------------------------------------

class TestPreconditionGates:
    @pytest.mark.asyncio
    async def test_precondition_failure_aborts_before_execution(self):
        """Callable precondition returns False → ABORTED, no tool call made."""
        handler_called = False

        async def tracked_handler(call):
            nonlocal handler_called
            handler_called = True
            return await _echo_handler(call)

        async def failing_check(call):
            return False

        tool = ToolDefinition(
            name="guarded_tool", description="",
            input_schema={}, executor=tracked_handler,
            trust_level=TrustLevel.SAFE,
            preconditions=["workspace must be clean"],
            precondition_checks=[failing_check],
        )
        reg = _make_registry(tool)
        pipeline = _build_pipeline(reg)
        call = _make_call("guarded_tool")

        result = await pipeline.execute(call, tracked_handler)
        assert not result.success
        assert "Precondition failed" in result.error
        assert "workspace must be clean" in result.error
        assert not handler_called  # Tool was never invoked!

    @pytest.mark.asyncio
    async def test_precondition_checks_all_must_pass(self):
        """Multiple preconditions: first failure aborts."""
        call_order = []

        async def check_a(call):
            call_order.append("a")
            return True

        async def check_b(call):
            call_order.append("b")
            return False

        async def check_c(call):
            call_order.append("c")
            return True

        tool = ToolDefinition(
            name="multi_check", description="",
            input_schema={}, executor=_echo_handler,
            trust_level=TrustLevel.SAFE,
            preconditions=["check a", "check b", "check c"],
            precondition_checks=[check_a, check_b, check_c],
        )
        reg = _make_registry(tool)
        pipeline = _build_pipeline(reg)
        call = _make_call("multi_check")

        result = await pipeline.execute(call, _echo_handler)
        assert not result.success
        assert call_order == ["a", "b"]  # c was never evaluated
        assert "check b" in result.error

    @pytest.mark.asyncio
    async def test_precondition_exception_treated_as_failure(self):
        """If precondition_check raises, treat as failure (not crash)."""
        async def exploding_check(call):
            raise ValueError("unexpected")

        tool = ToolDefinition(
            name="bomb", description="",
            input_schema={}, executor=_echo_handler,
            trust_level=TrustLevel.SAFE,
            precondition_checks=[exploding_check],
        )
        reg = _make_registry(tool)
        pipeline = _build_pipeline(reg)
        call = _make_call("bomb")

        result = await pipeline.execute(call, _echo_handler)
        assert not result.success
        assert "Precondition failed" in result.error

    @pytest.mark.asyncio
    async def test_all_preconditions_pass_proceeds(self):
        """All preconditions pass → tool executes normally."""
        async def ok_check(call):
            return True

        tool = ToolDefinition(
            name="ok_tool", description="",
            input_schema={}, executor=_echo_handler,
            trust_level=TrustLevel.SAFE,
            precondition_checks=[ok_check, ok_check],
        )
        reg = _make_registry(tool)
        pipeline = _build_pipeline(reg)
        call = _make_call("ok_tool")

        result = await pipeline.execute(call, _echo_handler)
        assert result.success


# ---------------------------------------------------------------------------
# EXECUTING — fires at exact dispatch moment
# ---------------------------------------------------------------------------

class TestExecutingGate:
    @pytest.mark.asyncio
    async def test_executing_fires_at_dispatch_moment(self):
        """EXECUTING state fires exactly when executor is about to be invoked."""
        state_when_handler_ran = None

        async def spy_handler(call):
            nonlocal state_when_handler_ran
            ctx = call.metadata.get(LIFECYCLE_CTX_KEY)
            if ctx:
                state_when_handler_ran = ctx.record.state
            return await _echo_handler(call)

        tool = ToolDefinition(
            name="spy", description="",
            input_schema={}, executor=spy_handler,
            trust_level=TrustLevel.SAFE,
        )
        reg = _make_registry(tool)
        pipeline = _build_pipeline(reg)
        call = _make_call("spy")

        result = await pipeline.execute(call, spy_handler)
        assert result.success
        assert state_when_handler_ran == ActionState.EXECUTING


# ---------------------------------------------------------------------------
# Abort signal during execution
# ---------------------------------------------------------------------------

class TestAbortDuringExecution:
    @pytest.mark.asyncio
    async def test_abort_before_execution_pre_set(self):
        """abort_signal already set → ABORTED before execution."""
        signal = asyncio.Event()
        signal.set()  # Pre-set

        tool = ToolDefinition(
            name="abortable", description="",
            input_schema={}, executor=_slow_handler,
            trust_level=TrustLevel.SAFE,
        )
        reg = _make_registry(tool)
        pipeline = _build_pipeline(reg)
        call = _make_call("abortable", abort_signal=signal)

        result = await pipeline.execute(call, _slow_handler)
        assert not result.success
        assert "Aborted" in result.error

    @pytest.mark.asyncio
    async def test_abort_during_execution_produces_aborted(self):
        """abort_signal fires during EXECUTING → ABORTED."""
        signal = asyncio.Event()
        states = []

        async def track(record, old, new):
            states.append(new)

        tool = ToolDefinition(
            name="slow_tool", description="",
            input_schema={}, executor=_slow_handler,
            trust_level=TrustLevel.SAFE,
        )
        reg = _make_registry(tool)
        pipeline = _build_pipeline(reg, on_state_change=track)
        call = _make_call("slow_tool", abort_signal=signal)

        async def fire_abort():
            await asyncio.sleep(0.05)
            signal.set()

        asyncio.ensure_future(fire_abort())
        result = await pipeline.execute(call, _slow_handler)

        assert not result.success
        assert "aborted" in states
        assert "memorialized" in states

    @pytest.mark.asyncio
    async def test_no_abort_signal_runs_normally(self):
        """No abort_signal → normal execution."""
        tool = ToolDefinition(
            name="normal", description="",
            input_schema={}, executor=_echo_handler,
            trust_level=TrustLevel.SAFE,
        )
        reg = _make_registry(tool)
        pipeline = _build_pipeline(reg)
        call = _make_call("normal")

        result = await pipeline.execute(call, _echo_handler)
        assert result.success


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------

class TestTimeoutHandling:
    @pytest.mark.asyncio
    async def test_timeout_produces_timed_out(self):
        """Timeout result from handler → TIMED_OUT state."""
        states = []

        async def track(record, old, new):
            states.append(new)

        tool = ToolDefinition(
            name="slow", description="",
            input_schema={}, executor=_timeout_handler,
            trust_level=TrustLevel.SAFE,
        )
        reg = _make_registry(tool)
        pipeline = _build_pipeline(reg, on_state_change=track)
        call = _make_call("slow")

        result = await pipeline.execute(call, _timeout_handler)
        assert not result.success
        assert "timed_out" in states
        assert "memorialized" in states


# ---------------------------------------------------------------------------
# Validation error handling
# ---------------------------------------------------------------------------

class TestValidationError:
    @pytest.mark.asyncio
    async def test_validation_error_produces_aborted(self):
        """Schema validation failure → AUTHORIZED → ABORTED → MEMORIALIZED."""
        states = []

        async def track(record, old, new):
            states.append(new)

        tool = ToolDefinition(
            name="strict", description="",
            input_schema={"properties": {"x": {"type": "string"}}, "required": ["x"]},
            executor=_echo_handler,
            trust_level=TrustLevel.SAFE,
        )
        reg = _make_registry(tool)
        pipeline = _build_pipeline(reg, on_state_change=track)
        call = _make_call("strict", args={})  # Missing required 'x'

        result = await pipeline.execute(call, _echo_handler)
        assert not result.success
        assert result.failure_type == "validation_error"
        assert "authorized" in states
        assert "aborted" in states
        assert "memorialized" in states


# ---------------------------------------------------------------------------
# Post-validation and rollback
# ---------------------------------------------------------------------------

class TestPostValidation:
    @pytest.mark.asyncio
    async def test_post_validator_pass_commits(self):
        """post_validator returns True → VALIDATED → COMMITTED."""
        states = []

        async def track(record, old, new):
            states.append(new)

        async def validator(call, result):
            return True

        tool = ToolDefinition(
            name="validated", description="",
            input_schema={}, executor=_echo_handler,
            trust_level=TrustLevel.SAFE,
            post_validator=validator,
        )
        reg = _make_registry(tool)
        pipeline = _build_pipeline(reg, on_state_change=track)
        call = _make_call("validated")

        result = await pipeline.execute(call, _echo_handler)
        assert result.success
        assert "validated" in states
        assert "committed" in states

    @pytest.mark.asyncio
    async def test_post_validator_fail_with_rollback(self):
        """post_validator returns False + rollback_fn → REVERTING → REVERTED."""
        states = []

        async def track(record, old, new):
            states.append(new)

        async def validator(call, result):
            return False

        async def rollback(call, result):
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=True, output="rolled back",
            )

        tool = ToolDefinition(
            name="rollbackable", description="",
            input_schema={}, executor=_echo_handler,
            trust_level=TrustLevel.SAFE,
            post_validator=validator,
            rollback_fn=rollback,
        )
        reg = _make_registry(tool)
        pipeline = _build_pipeline(reg, on_state_change=track)
        call = _make_call("rollbackable")

        result = await pipeline.execute(call, _echo_handler)
        assert not result.success
        assert "rolled back" in result.error
        assert "reverting" in states
        assert "reverted" in states


# ---------------------------------------------------------------------------
# LifecycleGateMiddleware pass-through
# ---------------------------------------------------------------------------

class TestGatePassThrough:
    @pytest.mark.asyncio
    async def test_gate_no_context_passthrough(self):
        """
        LifecycleGateMiddleware with no LifecycleContext in metadata
        is a transparent pass-through (sub-agent compatibility).
        """
        reg = _make_registry()
        gate = LifecycleGateMiddleware(registry=reg)
        call = _make_call("test")
        # No LifecycleContext injected — call.metadata is empty

        result = await gate.process(call, _echo_handler)
        assert result.success


# ---------------------------------------------------------------------------
# Tool not found
# ---------------------------------------------------------------------------

class TestToolNotFound:
    @pytest.mark.asyncio
    async def test_unknown_tool_denied_pre_pipeline(self):
        """
        Unknown tool with pipeline short-circuiting → DENIED → MEMORIALIZED.

        In production, _dispatch() returns an error BEFORE the pipeline runs.
        If the pipeline somehow receives the error (e.g. via a middleware that
        catches it), the outer middleware handles DECLARED → DENIED.
        """
        states = []

        async def track(record, old, new):
            states.append(new)

        # Simulate _dispatch()-level rejection: the handler is never called
        # because SchemaValidationMiddleware returns tool_not_found.
        reg = _make_registry()  # empty registry

        # Build pipeline WITHOUT LifecycleGateMiddleware to simulate
        perm = PermissionContext(session_id="test-session")
        pipeline = MiddlewarePipeline([
            LifecycleMiddleware(
                registry=reg,
                on_state_change=track,
            ),
            SchemaValidationMiddleware(registry=reg),
            BlastRadiusMiddleware(perm_ctx=perm, confirm_fn=AsyncMock(return_value=True)),
        ])
        call = _make_call("nonexistent")

        async def not_found_handler(c):
            return ToolResult(
                call_id=c.id, tool_name=c.tool_name,
                success=False, error="Unknown tool: nonexistent",
                failure_type="tool_not_found",
            )

        result = await pipeline.execute(call, not_found_handler)
        assert not result.success
        assert "denied" in states

