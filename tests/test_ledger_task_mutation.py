"""task_write tool → ledger task_mutation emit (#322 commit 3 / doc/53 §3.1, §11.1).

Post-#205 the entire TaskList mutation surface is the single ``task_write``
tool that replaces the whole list. Every successful write emits one
``task_mutation`` event with ``operation="write"`` carrying the full
post-write status summary as ``task_state``. Per-task done/modify/abandon
are reader-side derivations from successive snapshots — keeping them as
ledger event types would require diff logic and double-write the same
data the snapshot already encodes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.core.ledger import (
    LedgerEmitter,
    LedgerStore,
    async_correlation_scope,
    async_turn_scope,
)
from loom.core.tasks.manager import TaskListManager
from loom.platform.cli.tools import make_task_write_tool


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
async def task_tool(ledger: LedgerStore):
    manager = TaskListManager(session_id="sess_task")
    emitter = LedgerEmitter(ledger, session_id="sess_task")
    return make_task_write_tool(manager, ledger_emitter=emitter), manager


def _call(todos: list[dict]) -> ToolCall:
    return ToolCall(
        tool_name="task_write",
        args={"todos": todos},
        trust_level=TrustLevel.SAFE,
        session_id="sess_task",
    )


async def _events_of_type(ledger: LedgerStore, evt_type: str) -> list:
    rows = await ledger.fetch_by_turn("turn_task")
    return [r for r in rows if r.event_type == evt_type]


# ---------------------------------------------------------------------------
# Successful write emits one task_mutation event
# ---------------------------------------------------------------------------


async def test_task_write_emits_task_mutation(task_tool, ledger: LedgerStore) -> None:
    tool, _ = task_tool
    todos = [
        {"id": "1", "content": "do thing A", "status": "in_progress"},
        {"id": "2", "content": "do thing B", "status": "pending"},
    ]
    async with async_turn_scope("turn_task"), async_correlation_scope("c1"):
        result = await tool.executor(_call(todos))
    assert result.success

    events = await _events_of_type(ledger, "task_mutation")
    assert len(events) == 1
    p = events[0].payload
    assert p["operation"] == "write"
    assert p["task_id"] == "tasklist:sess_task"
    assert p["task_state"]["total"] == 2
    assert events[0].correlation_id == "c1"


async def test_subsequent_writes_emit_independent_events(
    task_tool, ledger: LedgerStore
) -> None:
    """Successive writes appear as independent events; the diff
    (1 → 2 → 1 in_progress) is reader-side derivable from snapshots."""
    tool, _ = task_tool
    async with async_turn_scope("turn_task"), async_correlation_scope("c1"):
        await tool.executor(_call([{"id": "1", "content": "x", "status": "pending"}]))
        await tool.executor(
            _call(
                [
                    {"id": "1", "content": "x", "status": "in_progress"},
                    {"id": "2", "content": "y", "status": "pending"},
                ]
            )
        )
        await tool.executor(_call([{"id": "1", "content": "x", "status": "completed"}]))

    events = await _events_of_type(ledger, "task_mutation")
    assert [e.payload["task_state"]["total"] for e in events] == [1, 2, 1]


# ---------------------------------------------------------------------------
# Empty write (clear) is still a valid mutation
# ---------------------------------------------------------------------------


async def test_empty_todos_clears_and_emits(task_tool, ledger: LedgerStore) -> None:
    tool, _ = task_tool
    async with async_turn_scope("turn_task"), async_correlation_scope("c1"):
        await tool.executor(_call([{"id": "1", "content": "x", "status": "pending"}]))
        await tool.executor(_call([]))

    events = await _events_of_type(ledger, "task_mutation")
    assert len(events) == 2
    assert events[1].payload["task_state"]["total"] == 0


# ---------------------------------------------------------------------------
# Validation error → no emit
# ---------------------------------------------------------------------------


async def test_invalid_todos_does_not_emit(task_tool, ledger: LedgerStore) -> None:
    tool, _ = task_tool
    async with async_turn_scope("turn_task"), async_correlation_scope("c1"):
        result = await tool.executor(
            ToolCall(
                tool_name="task_write",
                args={"todos": "not_a_list"},
                trust_level=TrustLevel.SAFE,
                session_id="sess_task",
            )
        )
    assert not result.success
    events = await _events_of_type(ledger, "task_mutation")
    assert events == []


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


async def test_emit_failure_does_not_break_tool(
    task_tool, ledger: LedgerStore
) -> None:
    tool, manager = task_tool
    await ledger.close()
    async with async_turn_scope("turn_task"), async_correlation_scope("c1"):
        result = await tool.executor(
            _call([{"id": "1", "content": "x", "status": "pending"}])
        )
    assert result.success
    # The list write itself succeeded:
    assert manager.has_active_nodes()


# ---------------------------------------------------------------------------
# No emitter → silent
# ---------------------------------------------------------------------------


async def test_no_emitter_means_no_emit(ledger: LedgerStore) -> None:
    manager = TaskListManager(session_id="sess_silent")
    tool = make_task_write_tool(manager)  # no ledger_emitter
    # Should not require a turn_scope:
    result = await tool.executor(
        _call([{"id": "1", "content": "x", "status": "pending"}])
    )
    assert result.success
