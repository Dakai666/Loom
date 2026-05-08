"""Dual-emit comparator (#322 commit 7 / exit criterion).

doc/53 §11.2 — "dev branch 內 dual-emit 對拍驗證 envelope vs ledger 序列一致"

Existing observability path:
    LifecycleMiddleware → on_lifecycle(record) → ActionRecord.state_history
                       → on_state_change       → live state-change stream
    (envelope / SessionLog consumers project from these)

Ledger path:
    LifecycleMiddleware → ledger_emitter.emit_tool_lifecycle(BEGIN)
                       → on memorialize → emit_tool_lifecycle(END)
                          with state_history bundled into payload (§3.1)

This module asserts the two sides agree on every observable property
that survives Step 5 cutover:
- one BEGIN + one END per tool call (matching tool_call_id)
- ledger state_history equals ActionRecord.history_dicts() at memorialize
- success vs failure verdict aligns (envelope record.is_failure ↔
  ledger.error / payload.rolled_back)

Permission decisions emitted by BlastRadiusMiddleware are also compared
against the trace-side ``call.metadata`` markers when a confirm flow is
exercised.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from loom.core.harness.lifecycle import ActionRecord
from loom.core.harness.middleware import (
    BlastRadiusMiddleware,
    LifecycleMiddleware,
    MiddlewarePipeline,
    ToolCall,
    ToolResult,
    VerifierResult,
)
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.ledger import (
    LedgerEmitter,
    LedgerStore,
    async_correlation_scope,
    async_turn_scope,
)


@pytest_asyncio.fixture
async def ledger(tmp_path: Path) -> LedgerStore:
    s = LedgerStore(
        db_path=tmp_path / "ledger.db",
        blob_dir=tmp_path / "blobs",
    )
    await s.open()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
def emitter(ledger: LedgerStore) -> LedgerEmitter:
    return LedgerEmitter(ledger, session_id="sess_dual")


def _registry(
    *,
    name: str = "do_thing",
    succeeds: bool = True,
    post_validator=None,
    rollback_fn=None,
) -> ToolRegistry:
    reg = ToolRegistry()

    async def handler(call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.id,
            tool_name=call.tool_name,
            success=succeeds,
            output="result_ok" if succeeds else None,
            error=None if succeeds else "tool failed",
        )

    reg.register(
        ToolDefinition(
            name=name,
            description="dual-emit test tool",
            trust_level=TrustLevel.SAFE,
            input_schema={"type": "object"},
            executor=handler,
            post_validator=post_validator,
            rollback_fn=rollback_fn,
        )
    )
    return reg


def _make_call(name: str = "do_thing", args: dict | None = None) -> ToolCall:
    return ToolCall(
        tool_name=name,
        args=args or {"v": 1},
        trust_level=TrustLevel.SAFE,
        session_id="sess_dual",
    )


# ---------------------------------------------------------------------------
# Per-tool-call alignment
# ---------------------------------------------------------------------------


async def test_state_history_matches_between_envelope_and_ledger(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    captured_records: list[ActionRecord] = []

    async def envelope_hook(record: ActionRecord) -> None:
        captured_records.append(record)

    reg = _registry(succeeds=True)
    pipeline = MiddlewarePipeline(
        [
            LifecycleMiddleware(
                registry=reg,
                on_lifecycle=envelope_hook,
                ledger_emitter=emitter,
            )
        ]
    )

    async with async_turn_scope("turn_dual"), async_correlation_scope("c1"):
        await pipeline.execute(_make_call(), reg.get("do_thing").executor)

    assert len(captured_records) == 1
    record = captured_records[0]
    rows = await ledger.fetch_by_turn("turn_dual")
    end = next(r for r in rows if r.payload["phase"] == "END")

    # The ledger END's state_history is exactly what the envelope sees.
    assert end.payload["state_history"] == record.history_dicts()
    # tool_call_id matches the underlying ActionRecord.call.id
    assert end.payload["tool_call_id"] == record.call.id


async def test_success_failure_verdict_alignment(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    """A failing tool surfaces consistently on both sides."""
    captured: list[ActionRecord] = []

    async def hook(rec: ActionRecord) -> None:
        captured.append(rec)

    reg = _registry(succeeds=False)
    pipeline = MiddlewarePipeline(
        [
            LifecycleMiddleware(
                registry=reg, on_lifecycle=hook, ledger_emitter=emitter
            )
        ]
    )

    async with async_turn_scope("turn_dual"), async_correlation_scope("c1"):
        await pipeline.execute(_make_call(), reg.get("do_thing").executor)

    record = captured[0]
    end = next(
        r for r in await ledger.fetch_by_turn("turn_dual")
        if r.payload["phase"] == "END"
    )

    # Envelope side: record.result.success is False
    assert record.result is not None and record.result.success is False
    # Ledger side: error field carries the message; rolled_back False
    # (this tool defines no rollback_fn so REVERTED never fires).
    assert end.payload["error"] == "tool failed"
    assert end.payload["rolled_back"] is False
    # Both agree on the final state through state_history
    assert end.payload["state_history"] == record.history_dicts()


async def test_rollback_path_alignment(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    """When post_validator triggers rollback, both sides record REVERTED."""
    captured: list[ActionRecord] = []

    async def hook(rec: ActionRecord) -> None:
        captured.append(rec)

    async def post_validator(call, result):
        return VerifierResult(passed=False, reason="post-fail", signal="bad")

    async def rollback_fn(call, result):
        return ToolResult(
            call_id=call.id,
            tool_name=call.tool_name,
            success=True,
            output="rolled back",
        )

    reg = _registry(
        succeeds=True, post_validator=post_validator, rollback_fn=rollback_fn
    )
    pipeline = MiddlewarePipeline(
        [
            LifecycleMiddleware(
                registry=reg, on_lifecycle=hook, ledger_emitter=emitter
            )
        ]
    )

    async with async_turn_scope("turn_dual"), async_correlation_scope("c1"):
        await pipeline.execute(_make_call(), reg.get("do_thing").executor)

    record = captured[0]
    end = next(
        r for r in await ledger.fetch_by_turn("turn_dual")
        if r.payload["phase"] == "END"
    )
    assert end.payload["rolled_back"] is True
    # Envelope side also records REVERTED in state_history
    assert any(t.to_state.value == "reverted" for t in record.state_history)
    # Sequences agree
    assert end.payload["state_history"] == record.history_dicts()


# ---------------------------------------------------------------------------
# Multi-call ordering — ledger BEGIN events arrive in the same order as
# envelope on_lifecycle invocations.
# ---------------------------------------------------------------------------


async def test_multi_call_ordering(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    captured: list[ActionRecord] = []

    async def hook(rec: ActionRecord) -> None:
        captured.append(rec)

    reg = _registry()
    pipeline = MiddlewarePipeline(
        [
            LifecycleMiddleware(
                registry=reg, on_lifecycle=hook, ledger_emitter=emitter
            )
        ]
    )

    async with async_turn_scope("turn_dual"), async_correlation_scope("c1"):
        for i in range(5):
            await pipeline.execute(
                _make_call(args={"i": i}), reg.get("do_thing").executor
            )

    rows = await ledger.fetch_by_turn("turn_dual")
    begin_call_ids = [
        r.payload["tool_call_id"] for r in rows if r.payload["phase"] == "BEGIN"
    ]
    envelope_call_ids = [r.call.id for r in captured]
    assert begin_call_ids == envelope_call_ids
    assert len(begin_call_ids) == 5


# ---------------------------------------------------------------------------
# Permission decision alignment
# ---------------------------------------------------------------------------


async def test_permission_decision_matches_call_metadata(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    """Every _notify_lifecycle call lands one ledger event matching
    ctx.authorization_result/reason."""
    perm_ctx = MagicMock()
    confirm_fn = AsyncMock(return_value=True)
    mw = BlastRadiusMiddleware(
        perm_ctx=perm_ctx, confirm_fn=confirm_fn, ledger_emitter=emitter
    )

    call = _make_call()
    async with async_turn_scope("turn_dual"), async_correlation_scope("c1"):
        mw._notify_lifecycle(call, True, "scope-allow: precondition")
        mw._notify_lifecycle(call, False, "user denied")
        # Step 4 added _emit_lock that serialises ledger inserts; scheduled
        # emit tasks now need real I/O ticks to drain. Poll for completion
        # rather than guess at sleep counts.
        for _ in range(50):
            rows = await ledger.fetch_by_turn("turn_dual")
            if sum(1 for r in rows if r.event_type == "permission_decision") >= 2:
                break
            await asyncio.sleep(0.01)

    rows = await ledger.fetch_by_turn("turn_dual")
    pds = [r for r in rows if r.event_type == "permission_decision"]
    assert [p.payload["decision"] for p in pds] == ["grant", "deny"]
    assert pds[0].payload["reason"] == "scope-allow: precondition"
    assert pds[1].payload["reason"] == "user denied"
    # tool_call_id propagated correctly
    assert all(p.payload["tool_call_id"] == call.id for p in pds)
