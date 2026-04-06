"""
Tests for the Harness Layer:
  - TrustLevel & PermissionContext
  - ToolCall / ToolResult data types
  - MiddlewarePipeline execution and ordering
  - Built-in middlewares: LogMiddleware, TraceMiddleware, BlastRadiusMiddleware
  - ToolRegistry
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from loom.core.harness.permissions import TrustLevel, PermissionContext
from loom.core.harness.middleware import (
    ToolCall, ToolResult,
    Middleware, MiddlewarePipeline,
    LogMiddleware, TraceMiddleware, BlastRadiusMiddleware,
)
from loom.core.harness.registry import ToolDefinition, ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_call(
    tool_name: str = "test_tool",
    trust_level: TrustLevel = TrustLevel.SAFE,
    session_id: str = "sess-001",
    args: dict | None = None,
) -> ToolCall:
    return ToolCall(
        tool_name=tool_name,
        args=args or {},
        trust_level=trust_level,
        session_id=session_id,
    )


async def ok_handler(call: ToolCall) -> ToolResult:
    return ToolResult(call_id=call.id, tool_name=call.tool_name,
                      success=True, output="ok")


async def fail_handler(call: ToolCall) -> ToolResult:
    return ToolResult(call_id=call.id, tool_name=call.tool_name,
                      success=False, error="boom")


# ---------------------------------------------------------------------------
# TrustLevel
# ---------------------------------------------------------------------------

class TestTrustLevel:
    def test_enum_values(self):
        assert TrustLevel.SAFE.value == "safe"
        assert TrustLevel.GUARDED.value == "guarded"
        assert TrustLevel.CRITICAL.value == "critical"

    def test_label_contains_level_name(self):
        assert "SAFE" in TrustLevel.SAFE.label
        assert "GUARDED" in TrustLevel.GUARDED.label
        assert "CRITICAL" in TrustLevel.CRITICAL.label


# ---------------------------------------------------------------------------
# PermissionContext
# ---------------------------------------------------------------------------

class TestPermissionContext:
    def test_safe_always_authorized(self):
        ctx = PermissionContext(session_id="s1")
        assert ctx.is_authorized("any_tool", TrustLevel.SAFE) is True

    def test_guarded_not_authorized_by_default(self):
        ctx = PermissionContext(session_id="s1")
        assert ctx.is_authorized("write_file", TrustLevel.GUARDED) is False

    def test_guarded_authorized_after_authorize(self):
        ctx = PermissionContext(session_id="s1")
        ctx.authorize("write_file")
        assert ctx.is_authorized("write_file", TrustLevel.GUARDED) is True

    def test_guarded_revoked_after_revoke(self):
        ctx = PermissionContext(session_id="s1")
        ctx.authorize("write_file")
        ctx.revoke("write_file")
        assert ctx.is_authorized("write_file", TrustLevel.GUARDED) is False

    def test_critical_never_pre_authorized(self):
        ctx = PermissionContext(session_id="s1")
        ctx.authorize("danger_tool")   # even if explicitly authorized
        assert ctx.is_authorized("danger_tool", TrustLevel.CRITICAL) is False


# ---------------------------------------------------------------------------
# ToolCall / ToolResult
# ---------------------------------------------------------------------------

class TestToolCallToolResult:
    def test_toolcall_auto_id(self):
        c1 = make_call()
        c2 = make_call()
        assert c1.id != c2.id

    def test_toolcall_timestamp_set(self):
        call = make_call()
        assert call.timestamp is not None

    def test_toolresult_defaults(self):
        r = ToolResult(call_id="x", tool_name="t", success=True)
        assert r.output is None
        assert r.error is None
        assert r.duration_ms == 0.0


# ---------------------------------------------------------------------------
# MiddlewarePipeline — execution and ordering
# ---------------------------------------------------------------------------

class TestMiddlewarePipeline:
    @pytest.mark.asyncio
    async def test_empty_pipeline_calls_handler(self):
        pipeline = MiddlewarePipeline()
        call = make_call()
        result = await pipeline.execute(call, ok_handler)
        assert result.success is True
        assert result.output == "ok"

    @pytest.mark.asyncio
    async def test_single_middleware_passthrough(self):
        order = []

        class Recorder(Middleware):
            async def process(self, call, next):
                order.append("before")
                r = await next(call)
                order.append("after")
                return r

        pipeline = MiddlewarePipeline([Recorder()])
        await pipeline.execute(make_call(), ok_handler)
        assert order == ["before", "after"]

    @pytest.mark.asyncio
    async def test_middleware_ordering_outermost_first(self):
        """Middleware added first wraps outermost (before first, after last)."""
        order = []

        def make_mw(label):
            class MW(Middleware):
                async def process(self, call, next):
                    order.append(f"{label}:before")
                    r = await next(call)
                    order.append(f"{label}:after")
                    return r
            return MW()

        pipeline = MiddlewarePipeline([make_mw("A"), make_mw("B"), make_mw("C")])
        await pipeline.execute(make_call(), ok_handler)
        assert order == [
            "A:before", "B:before", "C:before",
            "C:after", "B:after", "A:after",
        ]

    @pytest.mark.asyncio
    async def test_middleware_can_short_circuit(self):
        """A middleware that does not call next() stops the chain."""
        class Blocker(Middleware):
            async def process(self, call, next):
                return ToolResult(call_id=call.id, tool_name=call.tool_name,
                                  success=False, error="blocked")

        handler_called = []
        async def handler(call):
            handler_called.append(True)
            return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True)

        pipeline = MiddlewarePipeline([Blocker()])
        result = await pipeline.execute(make_call(), handler)
        assert result.success is False
        assert result.error == "blocked"
        assert handler_called == []

    @pytest.mark.asyncio
    async def test_use_method_chaining(self):
        order = []

        class Recorder(Middleware):
            def __init__(self, label):
                self.label = label
            async def process(self, call, next):
                order.append(self.label)
                return await next(call)

        p = MiddlewarePipeline()
        p.use(Recorder("X")).use(Recorder("Y"))
        await p.execute(make_call(), ok_handler)
        assert order == ["X", "Y"]


# ---------------------------------------------------------------------------
# TraceMiddleware
# ---------------------------------------------------------------------------

class TestTraceMiddleware:
    @pytest.mark.asyncio
    async def test_records_duration(self):
        pipeline = MiddlewarePipeline([TraceMiddleware()])
        result = await pipeline.execute(make_call(), ok_handler)
        assert result.duration_ms >= 0.0

    @pytest.mark.asyncio
    async def test_callback_called_with_call_and_result(self):
        received = []

        async def on_trace(call, result):
            received.append((call.tool_name, result.success))

        pipeline = MiddlewarePipeline([TraceMiddleware(on_trace=on_trace)])
        await pipeline.execute(make_call("my_tool"), ok_handler)
        assert received == [("my_tool", True)]

    @pytest.mark.asyncio
    async def test_callback_called_even_on_failure(self):
        received = []

        async def on_trace(call, result):
            received.append(result.success)

        pipeline = MiddlewarePipeline([TraceMiddleware(on_trace=on_trace)])
        await pipeline.execute(make_call(), fail_handler)
        assert received == [False]


# ---------------------------------------------------------------------------
# BlastRadiusMiddleware
# ---------------------------------------------------------------------------

class TestBlastRadiusMiddleware:
    @pytest.mark.asyncio
    async def test_safe_tool_passes_without_confirm(self):
        confirm_called = []
        async def confirm(call):
            confirm_called.append(True)
            return True

        ctx = PermissionContext("s1")
        ctx.authorize("safe_tool")   # SAFE tools are pre-authorized in session

        pipeline = MiddlewarePipeline([
            BlastRadiusMiddleware(perm_ctx=ctx, confirm_fn=confirm)
        ])
        result = await pipeline.execute(
            make_call("safe_tool", TrustLevel.SAFE), ok_handler
        )
        assert result.success is True
        assert confirm_called == []   # confirm never invoked for authorized tools

    @pytest.mark.asyncio
    async def test_guarded_tool_triggers_confirm(self):
        confirm_called = []
        async def confirm(call):
            confirm_called.append(call.tool_name)
            return True

        ctx = PermissionContext("s1")   # write_file NOT authorized
        pipeline = MiddlewarePipeline([
            BlastRadiusMiddleware(perm_ctx=ctx, confirm_fn=confirm)
        ])
        result = await pipeline.execute(
            make_call("write_file", TrustLevel.GUARDED), ok_handler
        )
        assert result.success is True
        assert "write_file" in confirm_called

    @pytest.mark.asyncio
    async def test_denied_by_user_returns_failure(self):
        async def confirm(call):
            return False  # user says no

        ctx = PermissionContext("s1")
        pipeline = MiddlewarePipeline([
            BlastRadiusMiddleware(perm_ctx=ctx, confirm_fn=confirm)
        ])
        result = await pipeline.execute(
            make_call("write_file", TrustLevel.GUARDED), ok_handler
        )
        assert result.success is False
        assert "denied" in result.error.lower()

    @pytest.mark.asyncio
    async def test_guarded_authorized_after_first_confirm(self):
        confirm_count = [0]
        async def confirm(call):
            confirm_count[0] += 1
            return True

        ctx = PermissionContext("s1")
        pipeline = MiddlewarePipeline([
            BlastRadiusMiddleware(perm_ctx=ctx, confirm_fn=confirm)
        ])
        call = make_call("write_file", TrustLevel.GUARDED)
        await pipeline.execute(call, ok_handler)
        await pipeline.execute(call, ok_handler)   # second call — should not prompt again
        assert confirm_count[0] == 1   # confirmed only once

    @pytest.mark.asyncio
    async def test_critical_always_triggers_confirm(self):
        confirm_count = [0]
        async def confirm(call):
            confirm_count[0] += 1
            return True

        ctx = PermissionContext("s1")
        ctx.authorize("nuke")   # even explicitly authorized…
        pipeline = MiddlewarePipeline([
            BlastRadiusMiddleware(perm_ctx=ctx, confirm_fn=confirm)
        ])
        call = make_call("nuke", TrustLevel.CRITICAL)
        await pipeline.execute(call, ok_handler)
        await pipeline.execute(call, ok_handler)
        assert confirm_count[0] == 2   # …CRITICAL always re-confirms


# ---------------------------------------------------------------------------
# LogMiddleware (smoke test — just ensure it doesn't crash)
# ---------------------------------------------------------------------------

class TestLogMiddleware:
    @pytest.mark.asyncio
    async def test_does_not_crash(self):
        console = MagicMock()
        pipeline = MiddlewarePipeline([LogMiddleware(console)])
        result = await pipeline.execute(make_call(), ok_handler)
        assert result.success is True
        assert console.print.called

    @pytest.mark.asyncio
    async def test_logs_on_failure_too(self):
        console = MagicMock()
        pipeline = MiddlewarePipeline([LogMiddleware(console)])
        result = await pipeline.execute(make_call(), fail_handler)
        assert result.success is False
        assert console.print.called


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class TestToolRegistry:
    def _make_tool(self, name, trust=TrustLevel.SAFE):
        return ToolDefinition(
            name=name,
            description=f"Test tool {name}",
            trust_level=trust,
            input_schema={"type": "object", "properties": {}, "required": []},
            executor=ok_handler,
        )

    def test_register_and_get(self):
        reg = ToolRegistry()
        reg.register(self._make_tool("alpha"))
        assert reg.get("alpha") is not None
        assert reg.get("beta") is None

    def test_list_returns_all_registered(self):
        reg = ToolRegistry()
        reg.register(self._make_tool("a"))
        reg.register(self._make_tool("b"))
        names = {t.name for t in reg.list()}
        assert names == {"a", "b"}

    def test_to_anthropic_schema_structure(self):
        reg = ToolRegistry()
        reg.register(self._make_tool("tool_x"))
        schemas = reg.to_anthropic_schema()
        assert len(schemas) == 1
        s = schemas[0]
        assert s["name"] == "tool_x"
        assert "description" in s
        assert "input_schema" in s

    def test_overwrite_on_reregister(self):
        reg = ToolRegistry()
        t1 = self._make_tool("dup")
        t2 = ToolDefinition(
            name="dup", description="updated", trust_level=TrustLevel.GUARDED,
            input_schema={}, executor=ok_handler,
        )
        reg.register(t1)
        reg.register(t2)
        assert reg.get("dup").description == "updated"
        assert len(reg.list()) == 1
