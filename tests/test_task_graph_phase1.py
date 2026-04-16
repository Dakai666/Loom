"""Tests for TaskGraph Phase 1 — Issue #128.

Covers:
  - TaskGraph serialization round-trip
  - Graph mutation (add_with_id, remove, update_node, ready)
  - TaskGraphManager lifecycle (create, complete, fail, persist, load)
  - Result summary generation
  - Tool executors (task_plan, task_status, task_modify, task_done, task_read)
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from loom.core.tasks.graph import TaskGraph, TaskNode, TaskStatus, ExecutionPlan
from loom.core.tasks.manager import TaskGraphManager, GraphState
from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.platform.cli.tools import (
    make_task_plan_tool,
    make_task_status_tool,
    make_task_modify_tool,
    make_task_done_tool,
    make_task_read_tool,
)


# ── Helpers ────────────────────────────────────────────────────────────

def _tc(name: str, args: dict) -> ToolCall:
    return ToolCall(
        tool_name=name, args=args,
        trust_level=TrustLevel.SAFE, session_id="test",
    )


@pytest.fixture
def tmp_persist_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def mgr(tmp_persist_dir):
    return TaskGraphManager("test-session", persist_dir=tmp_persist_dir)


@pytest.fixture
def tools(mgr):
    return {
        "plan": make_task_plan_tool(mgr),
        "status": make_task_status_tool(mgr),
        "modify": make_task_modify_tool(mgr),
        "done": make_task_done_tool(mgr),
        "read": make_task_read_tool(mgr),
    }


# ── TaskGraph core ─────────────────────────────────────────────────────

class TestTaskGraphSerialization:
    def test_round_trip(self):
        g = TaskGraph()
        a = g.add_with_id("a", "Step A")
        b = g.add_with_id("b", "Step B", depends_on=["a"])
        a.complete("result A")
        a.result_summary = "summary A"

        data = g.to_dict()
        g2 = TaskGraph.from_dict(data)

        assert len(g2.nodes) == 2
        na = g2.get("a")
        assert na.status == TaskStatus.COMPLETED
        assert na.result == "result A"
        assert na.result_summary == "summary A"
        nb = g2.get("b")
        assert nb.depends_on == ["a"]
        assert nb.status == TaskStatus.PENDING

    def test_round_trip_preserves_topology(self):
        g = TaskGraph()
        g.add_with_id("a", "A")
        g.add_with_id("b", "B", depends_on=["a"])
        g.add_with_id("c", "C", depends_on=["a"])
        g.add_with_id("d", "D", depends_on=["b", "c"])

        plan1 = g.compile()
        g2 = TaskGraph.from_dict(g.to_dict())
        plan2 = g2.compile()

        levels1 = [[n.id for n in lvl] for lvl in plan1.levels]
        levels2 = [[n.id for n in lvl] for lvl in plan2.levels]
        assert levels1 == levels2


class TestTaskGraphMutation:
    def test_add_with_id(self):
        g = TaskGraph()
        n = g.add_with_id("myid", "content")
        assert n.id == "myid"
        assert g.get("myid") is n

    def test_add_with_id_duplicate_raises(self):
        g = TaskGraph()
        g.add_with_id("x", "X")
        with pytest.raises(ValueError, match="already exists"):
            g.add_with_id("x", "X again")

    def test_remove_pending(self):
        g = TaskGraph()
        g.add_with_id("a", "A")
        g.add_with_id("b", "B", depends_on=["a"])
        g.remove("b")
        assert g.get("b") is None
        assert len(g.nodes) == 1

    def test_remove_cleans_dep_references(self):
        g = TaskGraph()
        g.add_with_id("a", "A")
        g.add_with_id("b", "B", depends_on=["a"])
        g.add_with_id("c", "C", depends_on=["a", "b"])
        g.remove("b")
        assert "b" not in g.get("c").depends_on

    def test_remove_non_pending_raises(self):
        g = TaskGraph()
        n = g.add_with_id("a", "A")
        n.complete("done")
        with pytest.raises(ValueError, match="PENDING"):
            g.remove("a")

    def test_update_node(self):
        g = TaskGraph()
        g.add_with_id("a", "A")
        g.add_with_id("b", "B")
        g.update_node("b", content="B updated", depends_on=["a"])
        b = g.get("b")
        assert b.content == "B updated"
        assert b.depends_on == ["a"]

    def test_update_non_pending_raises(self):
        g = TaskGraph()
        n = g.add_with_id("a", "A")
        n.status = TaskStatus.IN_PROGRESS
        with pytest.raises(ValueError, match="PENDING"):
            g.update_node("a", content="changed")

    def test_ready(self):
        g = TaskGraph()
        g.add_with_id("a", "A")
        g.add_with_id("b", "B", depends_on=["a"])
        g.add_with_id("c", "C")

        ready = g.ready()
        assert sorted(n.id for n in ready) == ["a", "c"]

        g.get("a").complete("done")
        ready = g.ready()
        assert sorted(n.id for n in ready) == ["b", "c"]

    def test_status_summary(self):
        g = TaskGraph()
        g.add_with_id("a", "A")
        g.add_with_id("b", "B", depends_on=["a"])
        g.get("a").complete("done")

        s = g.status_summary()
        assert s["total_nodes"] == 2
        assert "completed" in s["by_status"]
        assert "pending" in s["by_status"]

    def test_reset_clears_result_summary(self):
        g = TaskGraph()
        n = g.add_with_id("a", "A")
        n.complete("done")
        n.result_summary = "summary"
        g.reset()
        assert n.status == TaskStatus.PENDING
        assert n.result_summary is None


# ── TaskGraphManager ───────────────────────────────────────────────────

class TestTaskGraphManager:
    def test_create_graph(self, mgr):
        mgr.create_graph([
            {"id": "a", "content": "Step A"},
            {"id": "b", "content": "Step B", "depends_on": ["a"]},
        ])
        assert mgr.has_graph
        assert mgr.state == GraphState.ACTIVE
        assert len(mgr.graph.nodes) == 2

    def test_create_duplicate_graph_raises(self, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        with pytest.raises(ValueError, match="already exists"):
            mgr.create_graph([{"id": "b", "content": "B"}])

    def test_create_cycle_raises(self, mgr):
        with pytest.raises(ValueError, match="Cycle"):
            mgr.create_graph([
                {"id": "a", "content": "A", "depends_on": ["b"]},
                {"id": "b", "content": "B", "depends_on": ["a"]},
            ])

    def test_mark_completed(self, mgr):
        mgr.create_graph([
            {"id": "a", "content": "A"},
            {"id": "b", "content": "B", "depends_on": ["a"]},
        ])
        node = mgr.mark_completed("a", "result of A")
        assert node.status == TaskStatus.COMPLETED
        assert node.result == "result of A"
        assert node.result_summary is not None

    def test_mark_failed(self, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        node = mgr.mark_failed("a", "API down")
        assert node.status == TaskStatus.FAILED
        assert node.error == "API down"

    def test_get_ready_nodes(self, mgr):
        mgr.create_graph([
            {"id": "a", "content": "A"},
            {"id": "b", "content": "B", "depends_on": ["a"]},
            {"id": "c", "content": "C"},
        ])
        ready = mgr.get_ready_nodes()
        assert sorted(n.id for n in ready) == ["a", "c"]

    def test_auto_advance(self, mgr):
        mgr.create_graph([
            {"id": "a", "content": "A"},
            {"id": "b", "content": "B", "depends_on": ["a"]},
        ])
        mgr.mark_completed("a", "done A")
        ready = mgr.get_ready_nodes()
        assert [n.id for n in ready] == ["b"]

    def test_graph_completion_state(self, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        mgr.mark_completed("a", "done")
        assert mgr.state == GraphState.COMPLETED

    def test_graph_failed_state(self, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        mgr.mark_failed("a", "error")
        assert mgr.state == GraphState.FAILED

    def test_build_node_context(self, mgr):
        mgr.create_graph([
            {"id": "a", "content": "Analyze"},
            {"id": "b", "content": "Design", "depends_on": ["a"]},
        ])
        mgr.mark_completed("a", "Found 12 endpoints")
        ctx = mgr.build_node_context(mgr.graph.get("b"))
        assert "task node [b]" in ctx
        assert "Prior task [a]" in ctx
        assert "Found 12 endpoints" in ctx
        assert "task_read" in ctx

    def test_persistence_round_trip(self, mgr, tmp_persist_dir):
        mgr.create_graph([
            {"id": "x", "content": "X"},
            {"id": "y", "content": "Y", "depends_on": ["x"]},
        ])
        mgr.mark_completed("x", "done X")

        mgr2 = TaskGraphManager("test-session", persist_dir=tmp_persist_dir)
        assert mgr2.load_persisted()
        assert len(mgr2.graph.nodes) == 2
        assert mgr2.graph.get("x").status == TaskStatus.COMPLETED
        assert mgr2.graph.get("y").status == TaskStatus.PENDING

    def test_abandon(self, mgr, tmp_persist_dir):
        mgr.create_graph([{"id": "a", "content": "A"}])
        persist_path = tmp_persist_dir / "test-session.json"
        assert persist_path.exists()
        mgr.abandon()
        assert not mgr.has_graph
        assert not persist_path.exists()

    def test_summary_short(self, mgr):
        assert mgr._generate_summary("short") == "short"

    def test_summary_medium(self, mgr):
        text = "x" * 2000
        summary = mgr._generate_summary(text)
        assert "truncated" in summary
        assert len(summary) < len(text)

    def test_summary_long(self, mgr):
        text = "H" * 3000 + "MIDDLE" + "T" * 3000
        summary = mgr._generate_summary(text)
        assert "..." in summary
        assert len(summary) < len(text)


# ── Tool executors ─────────────────────────────────────────────────────

class TestTaskPlanTool:
    async def test_create_plan(self, tools, mgr):
        r = await tools["plan"].executor(_tc("task_plan", {
            "tasks": [
                {"id": "a", "content": "Step A"},
                {"id": "b", "content": "Step B", "depends_on": ["a"]},
            ],
        }))
        assert r.success
        out = json.loads(r.output)
        assert out["status"] == "graph_created"
        assert out["total_nodes"] == 2
        assert out["ready_nodes"] == ["a"]

    async def test_empty_tasks_fails(self, tools):
        r = await tools["plan"].executor(_tc("task_plan", {"tasks": []}))
        assert not r.success

    async def test_missing_content_fails(self, tools):
        r = await tools["plan"].executor(_tc("task_plan", {
            "tasks": [{"id": "a"}],
        }))
        assert not r.success


class TestTaskStatusTool:
    async def test_no_graph(self, tools):
        r = await tools["status"].executor(_tc("task_status", {}))
        assert r.success
        assert "No active" in r.output

    async def test_with_graph(self, tools, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        r = await tools["status"].executor(_tc("task_status", {}))
        assert r.success
        out = json.loads(r.output)
        assert out["total_nodes"] == 1


class TestTaskDoneTool:
    async def test_complete_node(self, tools, mgr):
        mgr.create_graph([
            {"id": "a", "content": "A"},
            {"id": "b", "content": "B", "depends_on": ["a"]},
        ])
        r = await tools["done"].executor(_tc("task_done", {
            "node_id": "a", "result": "Finished A",
        }))
        assert r.success
        out = json.loads(r.output)
        assert out["action"] == "node_completed"
        assert len(out["ready_nodes"]) == 1
        assert out["ready_nodes"][0]["node_id"] == "b"
        assert "context" in out["ready_nodes"][0]

    async def test_fail_node(self, tools, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        r = await tools["done"].executor(_tc("task_done", {
            "node_id": "a", "error": "API 500",
        }))
        assert r.success
        out = json.loads(r.output)
        assert out["action"] == "node_failed"
        assert "hint" in out

    async def test_complete_without_result_fails(self, tools, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        r = await tools["done"].executor(_tc("task_done", {"node_id": "a"}))
        assert not r.success
        assert "result" in r.error.lower()


class TestTaskModifyTool:
    async def test_add_remove_update(self, tools, mgr):
        mgr.create_graph([
            {"id": "a", "content": "A"},
            {"id": "b", "content": "B"},
        ])
        r = await tools["modify"].executor(_tc("task_modify", {
            "add": [{"id": "c", "content": "C", "depends_on": ["a"]}],
            "remove": ["b"],
            "update": [{"id": "a", "content": "A revised"}],
        }))
        assert r.success
        out = json.loads(r.output)
        assert len(out["changes"]) == 3

    async def test_no_graph_fails(self, tools):
        r = await tools["modify"].executor(_tc("task_modify", {
            "add": [{"id": "x", "content": "X"}],
        }))
        assert not r.success


class TestTaskReadTool:
    async def test_read_completed(self, tools, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        mgr.mark_completed("a", "Full result text here")
        r = await tools["read"].executor(_tc("task_read", {"node_id": "a"}))
        assert r.success
        assert r.output == "Full result text here"

    async def test_read_pending_fails(self, tools, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        r = await tools["read"].executor(_tc("task_read", {"node_id": "a"}))
        assert not r.success
        assert "pending" in r.error
