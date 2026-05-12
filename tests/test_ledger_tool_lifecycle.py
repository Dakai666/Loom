"""Middleware → ledger tool_lifecycle + permission_decision emit
(#322 commit 5 / doc/53 §3.1, §11.1).

Asserts that:
  - LifecycleMiddleware emits one BEGIN at process start and one END at
    memorialize, with state_history bundled into END payload (§3.1).
  - rolled_back flag is set when REVERTED is the terminal state.
  - BlastRadiusMiddleware emits one permission_decision per
    _notify_lifecycle call.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from loom.core.harness.middleware import (
    BlastRadiusMiddleware,
    LifecycleMiddleware,
    MiddlewarePipeline,
    ToolCall,
    ToolResult,
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
    return LedgerEmitter(ledger, session_id="sess_mw")


def _make_call(tool_name: str = "echo", args: dict | None = None) -> ToolCall:
    return ToolCall(
        tool_name=tool_name,
        args=args or {"msg": "hi"},
        trust_level=TrustLevel.SAFE,
        session_id="sess_mw",
    )


def _registry_with(tool_name: str = "echo", *, post_validator=None, rollback_fn=None):
    reg = ToolRegistry()

    async def handler(call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.id,
            tool_name=call.tool_name,
            success=True,
            output="ok",
        )

    reg.register(
        ToolDefinition(
            name=tool_name,
            description="echo",
            trust_level=TrustLevel.SAFE,
            input_schema={"type": "object"},
            executor=handler,
            post_validator=post_validator,
            rollback_fn=rollback_fn,
        )
    )
    return reg


async def _fetch(ledger: LedgerStore, turn_id: str, evt_type: str) -> list:
    rows = await ledger.fetch_by_turn(turn_id)
    return [r for r in rows if r.event_type == evt_type]


def _signal_permission_emits(emitter: LedgerEmitter, monkeypatch, *, count: int = 1):
    emitted = 0
    done = asyncio.Event()
    original_emit = emitter.emit_permission_decision

    async def _emit_and_signal(*args, **kwargs):
        nonlocal emitted
        result = await original_emit(*args, **kwargs)
        emitted += 1
        if emitted >= count:
            done.set()
        return result

    monkeypatch.setattr(emitter, "emit_permission_decision", _emit_and_signal)
    return done


# ---------------------------------------------------------------------------
# tool_lifecycle BEGIN + END
# ---------------------------------------------------------------------------


async def test_begin_and_end_pair_emitted(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    reg = _registry_with()
    pipeline = MiddlewarePipeline(
        [LifecycleMiddleware(registry=reg, ledger_emitter=emitter)]
    )

    async with async_turn_scope("turn_mw"), async_correlation_scope("c1"):
        result = await pipeline.execute(_make_call(), reg.get("echo").executor)

    assert result.success
    events = await _fetch(ledger, "turn_mw", "tool_lifecycle")
    phases = [e.payload["phase"] for e in events]
    assert phases == ["BEGIN", "END"]
    # END should reference BEGIN as its parent
    assert events[1].parent_event_id == events[0].event_id
    # state_history is on END only, not BEGIN
    assert events[0].payload["state_history"] == []
    assert len(events[1].payload["state_history"]) > 0
    # Both share the same correlation_id and tool_call_id
    assert events[0].correlation_id == "c1"
    assert events[1].correlation_id == "c1"
    assert events[0].payload["tool_call_id"] == events[1].payload["tool_call_id"]


async def test_end_carries_result_digest_and_summary(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    reg = _registry_with()
    pipeline = MiddlewarePipeline(
        [LifecycleMiddleware(registry=reg, ledger_emitter=emitter)]
    )

    async with async_turn_scope("turn_mw"), async_correlation_scope("c1"):
        await pipeline.execute(_make_call(), reg.get("echo").executor)

    end = (await _fetch(ledger, "turn_mw", "tool_lifecycle"))[1]
    assert end.payload["result_digest"].startswith("sha256:")
    assert end.payload["result_summary"] == "ok"
    assert end.payload["rolled_back"] is False
    assert end.payload["error"] is None


async def test_args_digest_changes_with_args(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    reg = _registry_with()
    pipeline = MiddlewarePipeline(
        [LifecycleMiddleware(registry=reg, ledger_emitter=emitter)]
    )

    async with async_turn_scope("turn_mw"), async_correlation_scope("c1"):
        await pipeline.execute(_make_call(args={"x": 1}), reg.get("echo").executor)
        await pipeline.execute(_make_call(args={"x": 2}), reg.get("echo").executor)

    begins = [
        e for e in await _fetch(ledger, "turn_mw", "tool_lifecycle")
        if e.payload["phase"] == "BEGIN"
    ]
    assert len(begins) == 2
    assert begins[0].payload["args_digest"] != begins[1].payload["args_digest"]
    assert all(e.payload["args_digest"].startswith("sha256:") for e in begins)


# ---------------------------------------------------------------------------
# rolled_back flag — REVERTED terminal state
# ---------------------------------------------------------------------------


async def test_rolled_back_flag_set_on_reverted(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    """post_validator returning a failing verdict triggers REVERTING/REVERTED."""
    from loom.core.harness.middleware import VerifierResult

    async def post_validator(call: ToolCall, result: ToolResult) -> VerifierResult:
        return VerifierResult(passed=False, signal="bad", reason="rejected")

    rollback_called: list[bool] = []

    async def rollback_fn(call: ToolCall, result: ToolResult) -> ToolResult:
        rollback_called.append(True)
        return ToolResult(
            call_id=call.call_id if hasattr(call, "call_id") else call.id,
            tool_name=call.tool_name,
            success=True,
            output="rolled back",
        )

    reg = _registry_with(post_validator=post_validator, rollback_fn=rollback_fn)
    pipeline = MiddlewarePipeline(
        [LifecycleMiddleware(registry=reg, ledger_emitter=emitter)]
    )

    async with async_turn_scope("turn_mw"), async_correlation_scope("c1"):
        await pipeline.execute(_make_call(), reg.get("echo").executor)

    end = next(
        e for e in await _fetch(ledger, "turn_mw", "tool_lifecycle")
        if e.payload["phase"] == "END"
    )
    assert end.payload["rolled_back"] is True
    states = [t["to"] for t in end.payload["state_history"]]
    assert "reverted" in states


# ---------------------------------------------------------------------------
# preserves the user-supplied on_lifecycle hook
# ---------------------------------------------------------------------------


async def test_user_on_lifecycle_still_fires(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    user_seen: list = []

    async def user_hook(record):
        user_seen.append(record.tool_name)

    reg = _registry_with()
    pipeline = MiddlewarePipeline(
        [
            LifecycleMiddleware(
                registry=reg,
                on_lifecycle=user_hook,
                ledger_emitter=emitter,
            )
        ]
    )
    async with async_turn_scope("turn_mw"), async_correlation_scope("c1"):
        await pipeline.execute(_make_call(), reg.get("echo").executor)

    assert user_seen == ["echo"]


# ---------------------------------------------------------------------------
# permission_decision emit (BlastRadiusMiddleware)
# ---------------------------------------------------------------------------


async def test_permission_decision_grant_emit(
    ledger: LedgerStore, emitter: LedgerEmitter, monkeypatch
) -> None:
    emitted = _signal_permission_emits(emitter, monkeypatch)
    perm_ctx = MagicMock()
    perm_ctx.exec_auto = False
    confirm_fn = AsyncMock(return_value=True)

    mw = BlastRadiusMiddleware(
        perm_ctx=perm_ctx,
        confirm_fn=confirm_fn,
        ledger_emitter=emitter,
    )
    call = _make_call()

    async with async_turn_scope("turn_mw"), async_correlation_scope("c1"):
        mw._notify_lifecycle(call, True, "pre-authorized")
        await asyncio.wait_for(emitted.wait(), timeout=1.0)

    events = await _fetch(ledger, "turn_mw", "permission_decision")
    assert len(events) == 1
    p = events[0].payload
    assert p["decision"] == "grant"
    assert p["tool_call_id"] == call.id
    assert p["reason"] == "pre-authorized"


async def test_permission_decision_deny_emit(
    ledger: LedgerStore, emitter: LedgerEmitter, monkeypatch
) -> None:
    emitted = _signal_permission_emits(emitter, monkeypatch)
    perm_ctx = MagicMock()
    confirm_fn = AsyncMock(return_value=False)
    mw = BlastRadiusMiddleware(
        perm_ctx=perm_ctx,
        confirm_fn=confirm_fn,
        ledger_emitter=emitter,
    )
    call = _make_call()

    async with async_turn_scope("turn_mw"), async_correlation_scope("c1"):
        mw._notify_lifecycle(call, False, "user denied (deny)")
        await asyncio.wait_for(emitted.wait(), timeout=1.0)

    events = await _fetch(ledger, "turn_mw", "permission_decision")
    assert len(events) == 1
    assert events[0].payload["decision"] == "deny"


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


async def test_lifecycle_emit_failure_does_not_break_call(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    reg = _registry_with()
    pipeline = MiddlewarePipeline(
        [LifecycleMiddleware(registry=reg, ledger_emitter=emitter)]
    )
    await ledger.close()  # subsequent emits will raise

    async with async_turn_scope("turn_mw"), async_correlation_scope("c1"):
        result = await pipeline.execute(_make_call(), reg.get("echo").executor)

    assert result.success  # call still completes


async def test_no_emitter_means_no_lifecycle_events(ledger: LedgerStore) -> None:
    reg = _registry_with()
    pipeline = MiddlewarePipeline([LifecycleMiddleware(registry=reg)])  # no emitter

    # No turn_scope set either:
    result = await pipeline.execute(_make_call(), reg.get("echo").executor)
    assert result.success
    events = await _fetch(ledger, "turn_mw", "tool_lifecycle")
    assert events == []
