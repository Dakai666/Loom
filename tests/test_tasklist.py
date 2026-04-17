"""Tests for TaskList — Issue #153.

Covers:
  - TaskNode status transitions, is_done / is_active
  - TaskList add / remove / update / ready / validate (including cycle detection)
  - TaskListManager lifecycle: create, mark_in_progress, mark_completed,
    mark_failed, hard-truncation of long results, abandon
  - build_node_context pull-model injection
  - build_self_check_message (empty / active / no list)
  - Tool executors: task_plan, task_status, task_modify, task_done, task_read
  - Section filter semantics (head / tail / N-M / keyword)
"""

from __future__ import annotations

import json

import pytest

from loom.core.tasks.tasklist import TaskList, TaskNode, TaskStatus
from loom.core.tasks.manager import TaskListManager, HARD_RESULT_CAP
from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.platform.cli.tools import (
    make_task_plan_tool,
    make_task_status_tool,
    make_task_modify_tool,
    make_task_done_tool,
    make_task_read_tool,
)


def _tc(name: str, args: dict) -> ToolCall:
    return ToolCall(
        tool_name=name, args=args,
        trust_level=TrustLevel.SAFE, session_id="test",
    )


@pytest.fixture
def mgr():
    return TaskListManager("test-session")


@pytest.fixture
def tools(mgr):
    return {
        "plan": make_task_plan_tool(mgr),
        "status": make_task_status_tool(mgr),
        "modify": make_task_modify_tool(mgr),
        "done": make_task_done_tool(mgr),
        "read": make_task_read_tool(mgr),
    }


# ── TaskNode ───────────────────────────────────────────────────────────

class TestTaskNode:
    def test_defaults(self):
        n = TaskNode(id="a", content="do something")
        assert n.status == TaskStatus.PENDING
        assert n.depends_on == []
        assert n.result is None
        assert n.is_active
        assert not n.is_done

    def test_complete(self):
        n = TaskNode(id="a", content="x")
        n.complete(result="output")
        assert n.status == TaskStatus.COMPLETED
        assert n.result == "output"
        assert n.is_done
        assert not n.is_active

    def test_fail(self):
        n = TaskNode(id="a", content="x")
        n.fail("boom")
        assert n.status == TaskStatus.FAILED
        assert n.error == "boom"
        assert n.is_done


# ── TaskList ───────────────────────────────────────────────────────────

class TestTaskList:
    def test_add_and_get(self):
        lst = TaskList()
        node = lst.add("a", "content A")
        assert lst.get("a") is node
        assert lst.get("missing") is None

    def test_duplicate_id_rejected(self):
        lst = TaskList()
        lst.add("a", "A")
        with pytest.raises(ValueError, match="already exists"):
            lst.add("a", "A2")

    def test_remove_pending_node(self):
        lst = TaskList()
        lst.add("a", "A")
        lst.add("b", "B", depends_on=["a"])
        lst.remove("a")
        assert lst.get("a") is None
        # Dependency cleanup
        assert lst.get("b").depends_on == []

    def test_remove_nonpending_rejected(self):
        lst = TaskList()
        node = lst.add("a", "A")
        node.complete("done")
        with pytest.raises(ValueError, match="only PENDING"):
            lst.remove("a")

    def test_update_pending_content_and_deps(self):
        lst = TaskList()
        lst.add("a", "A")
        lst.add("b", "B")
        lst.update("b", content="B2", depends_on=["a"])
        assert lst.get("b").content == "B2"
        assert lst.get("b").depends_on == ["a"]

    def test_update_nonpending_rejected(self):
        lst = TaskList()
        node = lst.add("a", "A")
        node.complete("done")
        with pytest.raises(ValueError, match="only PENDING"):
            lst.update("a", content="X")

    def test_ready_respects_deps(self):
        lst = TaskList()
        lst.add("a", "A")
        lst.add("b", "B", depends_on=["a"])
        assert [n.id for n in lst.ready()] == ["a"]
        lst.get("a").complete("ok")
        assert [n.id for n in lst.ready()] == ["b"]

    def test_active_excludes_done(self):
        lst = TaskList()
        a = lst.add("a", "A")
        lst.add("b", "B")
        a.complete("ok")
        assert {n.id for n in lst.active()} == {"b"}

    def test_validate_catches_unknown_dep(self):
        lst = TaskList()
        lst._nodes["a"] = TaskNode(id="a", content="A", depends_on=["ghost"])
        with pytest.raises(ValueError, match="unknown node"):
            lst.validate()

    def test_validate_catches_cycle(self):
        lst = TaskList()
        lst._nodes["a"] = TaskNode(id="a", content="A", depends_on=["b"])
        lst._nodes["b"] = TaskNode(id="b", content="B", depends_on=["a"])
        with pytest.raises(ValueError, match="Cycle detected"):
            lst.validate()

    def test_validate_accepts_dag(self):
        lst = TaskList()
        lst.add("a", "A")
        lst.add("b", "B", depends_on=["a"])
        lst.add("c", "C", depends_on=["a", "b"])
        lst.validate()


# ── TaskListManager ────────────────────────────────────────────────────

class TestTaskListManager:
    def test_create_list(self, mgr):
        mgr.create_list([
            {"id": "a", "content": "A"},
            {"id": "b", "content": "B", "depends_on": ["a"]},
        ])
        assert mgr.has_list
        assert mgr.tasklist.get("a") is not None
        assert mgr.tasklist.get("b").depends_on == ["a"]

    def test_create_rejects_duplicate_ids(self, mgr):
        with pytest.raises(ValueError, match="Duplicate"):
            mgr.create_list([
                {"id": "a", "content": "A"},
                {"id": "a", "content": "A2"},
            ])

    def test_create_rejects_while_active(self, mgr):
        mgr.create_list([{"id": "a", "content": "A"}])
        with pytest.raises(ValueError, match="already exists"):
            mgr.create_list([{"id": "b", "content": "B"}])

    def test_create_replaces_fully_done_list(self, mgr):
        mgr.create_list([{"id": "a", "content": "A"}])
        mgr.mark_completed("a", "done")
        mgr.create_list([{"id": "b", "content": "B"}])
        assert mgr.tasklist.get("a") is None
        assert mgr.tasklist.get("b") is not None

    def test_mark_completed_small_result(self, mgr):
        mgr.create_list([{"id": "a", "content": "A"}])
        mgr.mark_completed("a", "short result")
        node = mgr.tasklist.get("a")
        assert node.status == TaskStatus.COMPLETED
        assert node.result == "short result"

    def test_mark_completed_hard_truncates_long_result(self, mgr):
        mgr.create_list([{"id": "a", "content": "A"}])
        long_result = "x" * (HARD_RESULT_CAP + 1000)
        mgr.mark_completed("a", long_result)
        node = mgr.tasklist.get("a")
        assert len(node.result) <= HARD_RESULT_CAP + 300  # allow notice suffix
        assert "hard-truncated" in node.result
        assert "#154" in node.result

    def test_mark_failed_preserves_error(self, mgr):
        mgr.create_list([{"id": "a", "content": "A"}])
        mgr.mark_failed("a", "API returned 500")
        node = mgr.tasklist.get("a")
        assert node.status == TaskStatus.FAILED
        assert node.error == "API returned 500"

    def test_get_node_result_pending_returns_none(self, mgr):
        mgr.create_list([{"id": "a", "content": "A"}])
        assert mgr.get_node_result("a") is None

    def test_get_node_result_section_head(self, mgr):
        mgr.create_list([{"id": "a", "content": "A"}])
        result = "\n".join(f"line {i}" for i in range(1, 500))
        mgr.mark_completed("a", result)
        head = mgr.get_node_result("a", section="head")
        assert head.count("\n") <= 200
        assert head.startswith("line 1")

    def test_get_node_result_section_range(self, mgr):
        mgr.create_list([{"id": "a", "content": "A"}])
        result = "\n".join(f"line {i}" for i in range(1, 100))
        mgr.mark_completed("a", result)
        mid = mgr.get_node_result("a", section="5-10")
        lines = [ln for ln in mid.splitlines() if ln]
        assert lines == [f"line {i}" for i in range(5, 11)]

    def test_get_node_result_section_keyword(self, mgr):
        mgr.create_list([{"id": "a", "content": "A"}])
        result = "apple\nbanana\ncherry apple\ndate"
        mgr.mark_completed("a", result)
        matches = mgr.get_node_result("a", section="apple")
        assert "apple" in matches
        assert "banana" not in matches

    def test_abandon_drops_list(self, mgr):
        mgr.create_list([{"id": "a", "content": "A"}])
        mgr.abandon()
        assert not mgr.has_list

    def test_build_node_context_includes_dep_summary(self, mgr):
        mgr.create_list([
            {"id": "a", "content": "Fetch data"},
            {"id": "b", "content": "Analyze", "depends_on": ["a"]},
        ])
        mgr.mark_completed("a", "raw data payload")
        ctx = mgr.build_node_context(mgr.tasklist.get("b"))
        assert "[a]" in ctx
        assert "task_read" in ctx


# ── Self-check message ────────────────────────────────────────────────

class TestSelfCheckMessage:
    def test_no_list_returns_none(self, mgr):
        assert mgr.build_self_check_message() is None

    def test_all_done_returns_none(self, mgr):
        mgr.create_list([{"id": "a", "content": "A"}])
        mgr.mark_completed("a", "ok")
        assert mgr.build_self_check_message() is None

    def test_pending_nodes_included_in_message(self, mgr):
        mgr.create_list([
            {"id": "a", "content": "Write tech briefing"},
            {"id": "b", "content": "Write medical briefing"},
        ])
        msg = mgr.build_self_check_message()
        assert msg is not None
        assert "2 unfinished" in msg
        assert "[a]" in msg
        assert "[b]" in msg
        assert "task_done" in msg

    def test_in_progress_still_counts_as_active(self, mgr):
        mgr.create_list([{"id": "a", "content": "A"}])
        mgr.mark_in_progress("a")
        msg = mgr.build_self_check_message()
        assert msg is not None
        assert "in_progress" in msg


# ── Tool executors ────────────────────────────────────────────────────

class TestToolExecutors:
    async def test_task_plan_creates_list(self, tools):
        call = _tc("task_plan", {"tasks": [
            {"id": "a", "content": "A"},
            {"id": "b", "content": "B", "depends_on": ["a"]},
        ]})
        result = await tools["plan"].executor(call)
        assert result.success
        data = json.loads(result.output)
        assert data["status"] == "tasklist_created"
        assert data["total_nodes"] == 2
        assert data["ready_nodes"] == ["a"]

    async def test_task_plan_rejects_empty(self, tools):
        call = _tc("task_plan", {"tasks": []})
        result = await tools["plan"].executor(call)
        assert not result.success

    async def test_task_plan_rejects_missing_fields(self, tools):
        call = _tc("task_plan", {"tasks": [{"id": "a"}]})
        result = await tools["plan"].executor(call)
        assert not result.success
        assert "content" in result.error

    async def test_task_status_no_list(self, tools):
        call = _tc("task_status", {})
        result = await tools["status"].executor(call)
        assert result.success
        assert "No active" in result.output

    async def test_task_done_completed_path(self, tools, mgr):
        await tools["plan"].executor(_tc("task_plan", {"tasks": [
            {"id": "a", "content": "A"},
            {"id": "b", "content": "B", "depends_on": ["a"]},
        ]}))
        done = await tools["done"].executor(_tc("task_done", {
            "node_id": "a", "result": "fetched payload",
        }))
        assert done.success
        data = json.loads(done.output)
        assert data["action"] == "node_completed"
        assert data["ready_nodes"][0]["node_id"] == "b"
        assert "[a]" in data["ready_nodes"][0]["context"]

    async def test_task_done_failed_path(self, tools):
        await tools["plan"].executor(_tc("task_plan", {"tasks": [
            {"id": "a", "content": "A"},
        ]}))
        done = await tools["done"].executor(_tc("task_done", {
            "node_id": "a", "error": "upstream API 500",
        }))
        assert done.success
        data = json.loads(done.output)
        assert data["action"] == "node_failed"
        assert data["error"] == "upstream API 500"

    async def test_task_done_requires_result_or_error(self, tools):
        await tools["plan"].executor(_tc("task_plan", {"tasks": [
            {"id": "a", "content": "A"},
        ]}))
        done = await tools["done"].executor(_tc("task_done", {
            "node_id": "a",
        }))
        assert not done.success

    async def test_task_modify_add_remove_update(self, tools, mgr):
        await tools["plan"].executor(_tc("task_plan", {"tasks": [
            {"id": "a", "content": "A"},
        ]}))
        result = await tools["modify"].executor(_tc("task_modify", {
            "add": [{"id": "b", "content": "B", "depends_on": ["a"]}],
            "update": [{"id": "a", "content": "A-revised"}],
        }))
        assert result.success
        assert mgr.tasklist.get("a").content == "A-revised"
        assert mgr.tasklist.get("b") is not None

    async def test_task_read_pending_returns_error(self, tools):
        await tools["plan"].executor(_tc("task_plan", {"tasks": [
            {"id": "a", "content": "A"},
        ]}))
        result = await tools["read"].executor(_tc("task_read", {"node_id": "a"}))
        assert not result.success
        assert "pending" in result.error

    async def test_task_read_completed_returns_full(self, tools, mgr):
        await tools["plan"].executor(_tc("task_plan", {"tasks": [
            {"id": "a", "content": "A"},
        ]}))
        mgr.mark_completed("a", "long payload content")
        result = await tools["read"].executor(_tc("task_read", {"node_id": "a"}))
        assert result.success
        assert result.output == "long payload content"
