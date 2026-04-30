"""Tests for TaskList — Issue #153, updated for Issue #205 collapse.

Covers:
  - TaskNode: status defaults, is_active
  - TaskList: replace(), nodes, active(), status_summary()
  - TaskListManager lifecycle: write(), status(), build_self_check_message()
  - task_write tool executor
"""

from __future__ import annotations

import json

import pytest

from loom.core.tasks.tasklist import TaskList, TaskNode, TaskStatus
from loom.core.tasks.manager import TaskListManager
from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.platform.cli.tools import make_task_write_tool


def _tc(name: str, args: dict) -> ToolCall:
    return ToolCall(
        tool_name=name, args=args,
        trust_level=TrustLevel.SAFE, session_id="test",
    )


@pytest.fixture
def mgr():
    return TaskListManager("test-session")


@pytest.fixture
def tool(mgr):
    return make_task_write_tool(mgr)


# ── TaskNode ───────────────────────────────────────────────────────────

class TestTaskNode:
    def test_defaults(self):
        n = TaskNode(id="a", content="do something")
        assert n.status == TaskStatus.PENDING
        assert n.is_active

    def test_other_status(self):
        n = TaskNode(id="a", content="x", status=TaskStatus.COMPLETED)
        assert n.status == TaskStatus.COMPLETED
        assert not n.is_active

    def test_in_progress_is_active(self):
        n = TaskNode(id="a", content="x", status=TaskStatus.IN_PROGRESS)
        assert n.is_active


# ── TaskList ───────────────────────────────────────────────────────────

class TestTaskList:
    def test_replace_creates_nodes(self):
        lst = TaskList()
        lst.replace([
            {"id": "a", "content": "A"},
            {"id": "b", "content": "B", "status": "completed"},
        ])
        assert len(lst.nodes) == 2
        assert lst.nodes[0].id == "a"
        assert lst.nodes[1].status == TaskStatus.COMPLETED

    def test_replace_empty_clears(self):
        lst = TaskList()
        lst.replace([{"id": "a", "content": "A"}])
        lst.replace([])
        assert len(lst.nodes) == 0

    def test_replace_rejects_missing_id(self):
        lst = TaskList()
        with pytest.raises(ValueError, match="missing 'id'"):
            lst.replace([{"content": "no id"}])

    def test_replace_rejects_missing_content(self):
        lst = TaskList()
        with pytest.raises(ValueError, match="missing 'content'"):
            lst.replace([{"id": "a"}])

    def test_replace_rejects_duplicate_ids(self):
        lst = TaskList()
        with pytest.raises(ValueError, match="duplicate"):
            lst.replace([
                {"id": "a", "content": "A"},
                {"id": "a", "content": "A2"},
            ])

    def test_replace_rejects_bad_status(self):
        lst = TaskList()
        with pytest.raises(ValueError, match="unknown status"):
            lst.replace([{"id": "a", "content": "A", "status": "bogus"}])

    def test_active_filters_in_progress(self):
        lst = TaskList()
        lst.replace([
            {"id": "a", "content": "A"},
            {"id": "b", "content": "B", "status": "in_progress"},
            {"id": "c", "content": "C", "status": "completed"},
        ])
        active_ids = {n.id for n in lst.active()}
        assert active_ids == {"a", "b"}

    def test_status_summary(self):
        lst = TaskList()
        lst.replace([
            {"id": "a", "content": "A"},
            {"id": "b", "content": "B", "status": "completed"},
        ])
        summary = lst.status_summary()
        assert summary["total"] == 2
        assert "pending" in summary["by_status"]
        assert "completed" in summary["by_status"]
        assert len(summary["todos"]) == 2


# ── TaskListManager ────────────────────────────────────────────────────

class TestTaskListManager:
    def test_initial_empty(self, mgr):
        assert not mgr.has_list
        assert mgr.tasklist is None

    def test_write_creates_list(self, mgr):
        summary = mgr.write([
            {"id": "a", "content": "A"},
            {"id": "b", "content": "B"},
        ])
        assert mgr.has_list
        assert summary["total"] == 2
        assert len(mgr.tasklist.nodes) == 2

    def test_write_empty_clears(self, mgr):
        mgr.write([{"id": "a", "content": "A"}])
        mgr.write([])
        assert not mgr.has_list
        assert mgr.tasklist is None

    def test_write_replaces_previous(self, mgr):
        mgr.write([{"id": "a", "content": "A"}])
        mgr.write([{"id": "b", "content": "B"}])
        assert len(mgr.tasklist.nodes) == 1
        assert mgr.tasklist.nodes[0].id == "b"

    def test_status_returns_summary(self, mgr):
        mgr.write([{"id": "a", "content": "A"}])
        s = mgr.status()
        assert s["total"] == 1
        assert "pending" in s["by_status"]

    def test_status_empty(self, mgr):
        s = mgr.status()
        assert s["total"] == 0

    def test_has_active_nodes(self, mgr):
        mgr.write([{"id": "a", "content": "A"}])
        assert mgr.has_active_nodes()

    def test_no_active_when_all_completed(self, mgr):
        mgr.write([{"id": "a", "content": "A", "status": "completed"}])
        assert not mgr.has_active_nodes()

    def test_build_self_check_none_when_empty(self, mgr):
        assert mgr.build_self_check_message() is None

    def test_build_self_check_none_when_all_done(self, mgr):
        mgr.write([{"id": "a", "content": "A", "status": "completed"}])
        assert mgr.build_self_check_message() is None

    def test_build_self_check_includes_active(self, mgr):
        mgr.write([
            {"id": "a", "content": "Write tech briefing"},
            {"id": "b", "content": "Write medical briefing", "status": "in_progress"},
        ])
        msg = mgr.build_self_check_message()
        assert msg is not None
        assert "2 unfinished" in msg
        assert "[a]" in msg
        assert "[b]" in msg
        assert "task_write" in msg

    def test_on_change_fires(self, mgr):
        calls = []
        mgr.on_change = lambda s: calls.append(s)
        mgr.write([{"id": "a", "content": "A"}])
        assert len(calls) == 1
        assert calls[0]["total"] == 1


# ── task_write tool executor ───────────────────────────────────────────

class TestTaskWriteTool:
    async def test_creates_list(self, tool, mgr):
        call = _tc("task_write", {"todos": [
            {"id": "a", "content": "A"},
            {"id": "b", "content": "B", "status": "completed"},
        ]})
        result = await tool.executor(call)
        assert result.success
        data = json.loads(result.output)
        assert data["total"] == 2
        assert "pending" in data["by_status"]
        assert "completed" in data["by_status"]
        assert mgr.has_list

    async def test_clears_with_empty(self, tool, mgr):
        mgr.write([{"id": "a", "content": "A"}])
        call = _tc("task_write", {"todos": []})
        result = await tool.executor(call)
        assert result.success
        data = json.loads(result.output)
        assert data["total"] == 0
        assert not mgr.has_list

    async def test_rejects_missing_content(self, tool):
        call = _tc("task_write", {"todos": [{"id": "a"}]})
        result = await tool.executor(call)
        assert not result.success
        assert "content" in result.error

    async def test_rejects_duplicate_ids(self, tool):
        call = _tc("task_write", {"todos": [
            {"id": "a", "content": "A"},
            {"id": "a", "content": "A2"},
        ]})
        result = await tool.executor(call)
        assert not result.success

    async def test_status_with_no_list(self, tool):
        call = _tc("task_write", {"todos": []})
        result = await tool.executor(call)
        assert result.success
        data = json.loads(result.output)
        assert data["total"] == 0
