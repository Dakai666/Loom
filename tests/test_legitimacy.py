import asyncio
import pytest

from loom.core.harness.middleware import (
    ToolCall, ToolResult, MiddlewarePipeline, BlastRadiusMiddleware, LegitimacyGuardMiddleware, ToolHandler
)
from loom.core.harness.permissions import PermissionContext, TrustLevel
from loom.core.harness.scope import ConfirmDecision
from loom.core.harness.registry import ToolRegistry, ToolDefinition
from loom.core.harness.lifecycle import ActionRecord, ActionState, LifecycleContext, LIFECYCLE_CTX_KEY, ActionIntent

@pytest.fixture
def registry():
    r = ToolRegistry()
    async def mock_executor(call): return ToolResult(call_id="", tool_name="", success=True)
    r.register(ToolDefinition(name="read_file", description="", input_schema={}, executor=mock_executor, trust_level=TrustLevel.SAFE))
    r.register(ToolDefinition(name="write_file", description="", input_schema={}, executor=mock_executor, trust_level=TrustLevel.GUARDED))
    return r

@pytest.fixture
def perm_ctx():
    return PermissionContext(session_id="test")

@pytest.fixture
def handler():
    async def mock_handler(call: ToolCall) -> ToolResult:
        return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True)
    return mock_handler

def make_call(name: str, trust: TrustLevel) -> ToolCall:
    call = ToolCall(tool_name=name, args={}, trust_level=trust, session_id="test")
    # minimal lifecycle ctx
    record = ActionRecord(call=call, intent=ActionIntent(intent_summary=name))
    ctx = LifecycleContext(record=record)
    call.metadata[LIFECYCLE_CTX_KEY] = ctx
    return call

def make_call_with_args(name: str, trust: TrustLevel, args: dict) -> ToolCall:
    call = ToolCall(tool_name=name, args=args, trust_level=trust, session_id="test")
    record = ActionRecord(call=call, intent=ActionIntent(intent_summary=name))
    ctx = LifecycleContext(record=record)
    call.metadata[LIFECYCLE_CTX_KEY] = ctx
    return call


@pytest.mark.asyncio
async def test_write_file_blocked_without_probe(handler):
    """write_file is blocked when no read_file / list_dir has been called this turn."""
    guard = LegitimacyGuardMiddleware()
    call_write = make_call_with_args("write_file", TrustLevel.GUARDED, {"path": "foo.py", "content": ""})
    res = await guard.process(call_write, handler)
    assert not res.success
    assert res.failure_type == "permission_denied"
    assert "read the target file" in res.error
    assert call_write.metadata[LIFECYCLE_CTX_KEY].authorization_result is False


@pytest.mark.asyncio
async def test_write_file_allowed_after_read(handler):
    """write_file passes after any probe tool runs (has_probed=True)."""
    guard = LegitimacyGuardMiddleware()
    call_read = make_call_with_args("read_file", TrustLevel.SAFE, {"path": "foo.py"})
    await guard.process(call_read, handler)
    assert guard.has_probed
    assert "foo.py" in guard._read_paths

    call_write = make_call_with_args("write_file", TrustLevel.GUARDED, {"path": "foo.py", "content": ""})
    res = await guard.process(call_write, handler)
    assert res.success


@pytest.mark.asyncio
async def test_run_bash_not_guarded_by_probe(handler):
    """run_bash is NOT in strict_guard_tools — BlastRadius owns exec authorization."""
    guard = LegitimacyGuardMiddleware()
    assert "run_bash" not in guard.strict_guard_tools
    call_bash = make_call("run_bash", TrustLevel.GUARDED)
    res = await guard.process(call_bash, handler)
    assert res.success, "run_bash should bypass LegitimacyGuard entirely"


@pytest.mark.asyncio
async def test_read_paths_cleared_on_reset(handler):
    """reset_probe() clears _read_paths and has_probed; session trust survives."""
    guard = LegitimacyGuardMiddleware()
    await guard.process(make_call_with_args("read_file", TrustLevel.SAFE, {"path": "a.py"}), handler)
    # First write succeeds → session-trusted
    await guard.process(make_call_with_args("write_file", TrustLevel.GUARDED, {"path": "a.py", "content": ""}), handler)
    assert "write_file" in guard._session_trusted

    guard.reset_probe()
    assert not guard.has_probed
    assert not guard._read_paths  # per-turn paths cleared

    # write_file session-trusted → still passes without re-probe
    res = await guard.process(make_call_with_args("write_file", TrustLevel.GUARDED, {"path": "b.py", "content": ""}), handler)
    assert res.success, "session-trusted write_file must not require re-probe"


@pytest.mark.asyncio
async def test_session_trust_skips_probe_on_subsequent_turns(handler):
    """Issue #118: once write_file succeeds in a session, future turns skip the probe requirement."""
    guard = LegitimacyGuardMiddleware()

    # Turn 1: probe + write → session-trusted
    await guard.process(make_call_with_args("read_file", TrustLevel.SAFE, {"path": "x.md"}), handler)
    res = await guard.process(make_call_with_args("write_file", TrustLevel.GUARDED, {"path": "x.md", "content": ""}), handler)
    assert res.success
    assert "write_file" in guard._session_trusted

    # New turn — per-turn state reset
    guard.reset_probe()
    assert not guard.has_probed

    # Turn 2: no probe, but session-trusted → passes
    res2 = await guard.process(make_call_with_args("write_file", TrustLevel.GUARDED, {"path": "y.md", "content": ""}), handler)
    assert res2.success, "session-trusted write_file must not require re-probe on new turn"


@pytest.mark.asyncio
async def test_circuit_breaker_trips_after_3_denies(perm_ctx, registry, handler):
    deny_count = 0
    async def always_deny(call) -> ConfirmDecision:
        nonlocal deny_count
        deny_count += 1
        return ConfirmDecision.DENY

    bmw = BlastRadiusMiddleware(
        perm_ctx=perm_ctx,
        confirm_fn=always_deny,
        registry=registry,
    )
    
    call = make_call("write_file", TrustLevel.GUARDED)
    call.abort_signal = asyncio.Event()
    
    # Deny 1
    res1 = await bmw.process(call, handler)
    assert not res1.success
    assert perm_ctx.recent_denies == 1
    assert not call.metadata.get("circuit_breaker")
    
    # Deny 2
    res2 = await bmw.process(call, handler)
    assert not res2.success
    assert perm_ctx.recent_denies == 2
    assert not call.metadata.get("circuit_breaker")
    
    # Deny 3 -> Circuit Breaker trips
    res3 = await bmw.process(call, handler)
    assert not res3.success
    assert perm_ctx.recent_denies == 3
    assert call.metadata.get("circuit_breaker") is True
    assert "Penalty Box activated" in res3.error
    assert call.abort_signal.is_set()

@pytest.mark.asyncio
async def test_circuit_breaker_resets_on_allow(perm_ctx, registry, handler):
    decision = ConfirmDecision.DENY
    async def mock_confirm(call) -> ConfirmDecision:
        return decision

    bmw = BlastRadiusMiddleware(
        perm_ctx=perm_ctx,
        confirm_fn=mock_confirm,
        registry=registry,
    )
    
    call = make_call("write_file", TrustLevel.GUARDED)
    
    # Deny 1 & 2
    await bmw.process(call, handler)
    await bmw.process(call, handler)
    assert perm_ctx.recent_denies == 2
    
    # Allow resets
    decision = ConfirmDecision.ONCE
    res3 = await bmw.process(call, handler)
    assert res3.success
    assert perm_ctx.recent_denies == 0
