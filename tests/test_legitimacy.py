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

@pytest.mark.asyncio
async def test_probe_first_heuristic_blocks_blind_mutation(handler):
    guard = LegitimacyGuardMiddleware()
    
    # Try write_file without probe
    call_write = make_call("write_file", TrustLevel.GUARDED)
    res = await guard.process(call_write, handler)
    assert not res.success
    assert res.failure_type == "permission_denied"
    assert "You must gather context first" in res.error
    assert call_write.metadata[LIFECYCLE_CTX_KEY].authorization_result is False

@pytest.mark.asyncio
async def test_probe_first_heuristic_allows_after_probe(handler):
    guard = LegitimacyGuardMiddleware()
    
    # 1. Probe
    call_read = make_call("read_file", TrustLevel.SAFE)
    res_read = await guard.process(call_read, handler)
    assert res_read.success
    assert guard.has_probed is True
    
    # 2. Mutate
    call_write = make_call("write_file", TrustLevel.GUARDED)
    res_write = await guard.process(call_write, handler)
    assert res_write.success

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
