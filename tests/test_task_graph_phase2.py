"""Tests for TaskGraph Phase 2 — Issue #128.

Covers:
  - Breakpoint resume: context generation for interrupted graphs
  - Result overflow: artifact file spill + lazy read + section filtering
  - Lifecycle: suspend/resume, TTL cleanup, abandon with artifact cleanup
  - task_read section parameter
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from loom.core.tasks.graph import TaskGraph, TaskNode, TaskStatus
from loom.core.tasks.manager import (
    TaskGraphManager, GraphState,
    _OVERFLOW_THRESHOLD, _GRAPH_TTL_SECONDS,
)
from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.platform.cli.tools import make_task_read_tool


# ── Helpers ────────────────────────────────────────────────────────────

def _tc(name: str, args: dict) -> ToolCall:
    return ToolCall(
        tool_name=name, args=args,
        trust_level=TrustLevel.SAFE, session_id="test",
    )


@pytest.fixture
def tmp_dirs():
    with tempfile.TemporaryDirectory() as persist, \
         tempfile.TemporaryDirectory() as artifact:
        yield Path(persist), Path(artifact)


@pytest.fixture
def mgr(tmp_dirs):
    persist, artifact = tmp_dirs
    return TaskGraphManager(
        "test-session", persist_dir=persist, artifact_dir=artifact,
    )


# ── Result overflow ───────────────────────────────────────────────────

class TestResultOverflow:
    def test_small_result_stays_in_memory(self, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        mgr.mark_completed("a", "short result")
        node = mgr.graph.get("a")
        assert "_artifact_path" not in node.metadata
        assert node.result == "short result"

    def test_large_result_overflows_to_file(self, mgr, tmp_dirs):
        _, artifact_dir = tmp_dirs
        mgr.create_graph([{"id": "a", "content": "A"}])
        large = "x" * (_OVERFLOW_THRESHOLD + 100)
        mgr.mark_completed("a", large)
        node = mgr.graph.get("a")
        assert "_artifact_path" in node.metadata
        artifact_path = Path(node.metadata["_artifact_path"])
        assert artifact_path.exists()
        assert artifact_path.read_text() == large

    def test_get_node_result_reads_from_artifact(self, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        large = "line {}\n".format("x" * 100) * 100  # > 5000 chars
        mgr.mark_completed("a", large)
        result = mgr.get_node_result("a")
        assert result == large

    def test_get_node_result_section_head(self, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        lines = "".join(f"line {i}\n" for i in range(500))
        mgr.mark_completed("a", lines)
        result = mgr.get_node_result("a", section="head")
        assert result.count("\n") == 200

    def test_get_node_result_section_tail(self, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        lines = "".join(f"line {i}\n" for i in range(500))
        mgr.mark_completed("a", lines)
        result = mgr.get_node_result("a", section="tail")
        assert "line 499" in result
        assert "line 0\n" not in result

    def test_get_node_result_section_range(self, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        lines = "".join(f"line {i}\n" for i in range(100))
        mgr.mark_completed("a", lines)
        result = mgr.get_node_result("a", section="5-10")
        assert "line 4\n" in result   # 1-indexed: line 5 = index 4
        assert "line 9\n" in result   # line 10 = index 9
        assert "line 10\n" not in result

    def test_get_node_result_section_keyword(self, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        text = "apple\nbanana\napricot\ncherry\n"
        mgr.mark_completed("a", text)
        result = mgr.get_node_result("a", section="ap")
        assert "apple" in result
        assert "apricot" in result
        assert "banana" not in result
        assert "cherry" not in result

    def test_get_node_result_section_no_match(self, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        mgr.mark_completed("a", "hello world")
        result = mgr.get_node_result("a", section="zzz")
        assert "no lines matching" in result


# ── Lifecycle: suspend / resume ───────────────────────────────────────

class TestLifecycle:
    def test_suspend_active_graph(self, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        mgr.suspend()
        assert mgr.state == GraphState.SUSPENDED

    def test_suspend_completed_graph_no_change(self, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        mgr.mark_completed("a", "done")
        assert mgr.state == GraphState.COMPLETED
        mgr.suspend()
        assert mgr.state == GraphState.COMPLETED  # unchanged

    def test_suspend_no_graph(self, mgr):
        mgr.suspend()  # should not raise
        assert mgr.state == GraphState.ACTIVE

    def test_resume_suspended(self, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        mgr.suspend()
        assert mgr.resume()
        assert mgr.state == GraphState.ACTIVE

    def test_resume_non_suspended_returns_false(self, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        assert not mgr.resume()  # already active

    def test_suspend_persists(self, mgr, tmp_dirs):
        persist, artifact = tmp_dirs
        mgr.create_graph([{"id": "a", "content": "A"}])
        mgr.suspend()

        mgr2 = TaskGraphManager("test-session", persist_dir=persist, artifact_dir=artifact)
        mgr2.load_persisted()
        assert mgr2.state == GraphState.SUSPENDED


# ── Breakpoint resume context ─────────────────────────────────────────

class TestResumeContext:
    def test_no_graph_returns_none(self, mgr):
        assert mgr.build_resume_context() is None

    def test_completed_graph_returns_none(self, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        mgr.mark_completed("a", "done")
        assert mgr.build_resume_context() is None

    def test_suspended_graph_has_context(self, mgr):
        mgr.create_graph([
            {"id": "a", "content": "Analyze"},
            {"id": "b", "content": "Design", "depends_on": ["a"]},
        ])
        mgr.mark_completed("a", "Found 12 endpoints")
        mgr.suspend()
        ctx = mgr.build_resume_context()
        assert ctx is not None
        assert "Interrupted Task Graph" in ctx
        assert "[a]" in ctx
        assert "Completed" in ctx
        assert "[b]" in ctx
        assert "Pending" in ctx
        assert "task_status" in ctx

    def test_active_incomplete_has_context(self, mgr):
        mgr.create_graph([
            {"id": "a", "content": "Step A"},
            {"id": "b", "content": "Step B", "depends_on": ["a"]},
        ])
        # Active with pending nodes → should show context
        ctx = mgr.build_resume_context()
        assert ctx is not None
        assert "Pending" in ctx

    def test_failed_nodes_shown(self, mgr):
        mgr.create_graph([{"id": "a", "content": "A"}])
        mgr.mark_failed("a", "API 500 error")
        mgr.suspend()  # failed graph doesn't suspend, but let's test the failed state
        # Since failed graph state won't be suspended, test directly
        mgr._state = GraphState.SUSPENDED  # force for testing
        ctx = mgr.build_resume_context()
        assert "Failed" in ctx
        assert "API 500" in ctx

    def test_resume_round_trip(self, tmp_dirs):
        """Full round-trip: create → partial complete → suspend → reload → resume."""
        persist, artifact = tmp_dirs
        mgr1 = TaskGraphManager("rt-test", persist_dir=persist, artifact_dir=artifact)
        mgr1.create_graph([
            {"id": "a", "content": "Analyze"},
            {"id": "b", "content": "Design", "depends_on": ["a"]},
            {"id": "c", "content": "Implement", "depends_on": ["b"]},
        ])
        mgr1.mark_completed("a", "Analysis complete: 5 tables")
        mgr1.suspend()

        # New session loads the graph
        mgr2 = TaskGraphManager("rt-test", persist_dir=persist, artifact_dir=artifact)
        assert mgr2.load_persisted()
        assert mgr2.state == GraphState.SUSPENDED
        assert mgr2.resume()
        assert mgr2.state == GraphState.ACTIVE

        # Agent can see context and continue
        ctx = mgr2.build_resume_context()
        assert ctx is not None  # still active with pending
        ready = mgr2.get_ready_nodes()
        assert [n.id for n in ready] == ["b"]


# ── TTL cleanup ───────────────────────────────────────────────────────

class TestTTLCleanup:
    def test_stale_suspended_cleaned(self, tmp_dirs):
        persist, artifact = tmp_dirs
        mgr = TaskGraphManager("stale-session", persist_dir=persist, artifact_dir=artifact)
        mgr.create_graph([{"id": "a", "content": "A"}])
        mgr.suspend()

        # Backdate the file
        path = persist / "stale-session.json"
        old_time = time.time() - _GRAPH_TTL_SECONDS - 100
        import os
        os.utime(path, (old_time, old_time))

        removed = TaskGraphManager.cleanup_stale_graphs(persist_dir=persist, ttl_seconds=_GRAPH_TTL_SECONDS)
        assert removed == 1
        assert not path.exists()

    def test_fresh_suspended_not_cleaned(self, tmp_dirs):
        persist, artifact = tmp_dirs
        mgr = TaskGraphManager("fresh-session", persist_dir=persist, artifact_dir=artifact)
        mgr.create_graph([{"id": "a", "content": "A"}])
        mgr.suspend()

        removed = TaskGraphManager.cleanup_stale_graphs(persist_dir=persist)
        assert removed == 0

    def test_completed_graph_not_cleaned(self, tmp_dirs):
        persist, artifact = tmp_dirs
        mgr = TaskGraphManager("done-session", persist_dir=persist, artifact_dir=artifact)
        mgr.create_graph([{"id": "a", "content": "A"}])
        mgr.mark_completed("a", "done")
        mgr._persist()  # force re-persist with completed state

        # Backdate
        path = persist / "done-session.json"
        old_time = time.time() - _GRAPH_TTL_SECONDS - 100
        import os
        os.utime(path, (old_time, old_time))

        removed = TaskGraphManager.cleanup_stale_graphs(persist_dir=persist)
        assert removed == 0  # completed graphs are not cleaned

    def test_cleanup_also_removes_artifacts(self, tmp_dirs):
        persist, artifact = tmp_dirs
        # Use a layout matching the default: persist=~/.loom/task_graphs, artifact=~/.loom/artifacts
        # cleanup_stale_graphs looks for artifacts at persist_dir.parent / "artifacts"
        base = persist.parent
        real_persist = base / "task_graphs"
        real_artifact = base / "artifacts"
        real_persist.mkdir(parents=True, exist_ok=True)

        mgr = TaskGraphManager("art-session", persist_dir=real_persist, artifact_dir=real_artifact)
        mgr.create_graph([
            {"id": "a", "content": "A"},
            {"id": "b", "content": "B", "depends_on": ["a"]},
        ])
        large = "x" * (_OVERFLOW_THRESHOLD + 100)
        mgr.mark_completed("a", large)
        # Graph still active (b is pending), so suspend works
        mgr.suspend()
        assert mgr.state == GraphState.SUSPENDED

        artifact_session_dir = real_artifact / "art-session"
        assert artifact_session_dir.exists()

        # Backdate
        path = real_persist / "art-session.json"
        old_time = time.time() - _GRAPH_TTL_SECONDS - 100
        import os
        os.utime(path, (old_time, old_time))

        removed = TaskGraphManager.cleanup_stale_graphs(persist_dir=real_persist)
        assert removed == 1
        assert not artifact_session_dir.exists()


# ── Abandon with artifact cleanup ─────────────────────────────────────

class TestAbandonCleanup:
    def test_abandon_removes_artifacts(self, mgr, tmp_dirs):
        _, artifact = tmp_dirs
        mgr.create_graph([{"id": "a", "content": "A"}])
        large = "x" * (_OVERFLOW_THRESHOLD + 100)
        mgr.mark_completed("a", large)

        artifact_dir = artifact / "test-session"
        assert artifact_dir.exists()

        mgr.abandon()
        assert not artifact_dir.exists()
        assert not mgr.has_graph


# ── task_read with section parameter ──────────────────────────────────

class TestTaskReadSection:
    async def test_read_with_section(self, mgr):
        tool = make_task_read_tool(mgr)
        mgr.create_graph([{"id": "a", "content": "A"}])
        text = "".join(f"line {i}\n" for i in range(300))
        mgr.mark_completed("a", text)

        r = await tool.executor(_tc("task_read", {"node_id": "a", "section": "head"}))
        assert r.success
        assert r.output.count("\n") == 200

    async def test_read_with_keyword(self, mgr):
        tool = make_task_read_tool(mgr)
        mgr.create_graph([{"id": "a", "content": "A"}])
        mgr.mark_completed("a", "foo\nbar\nfoobar\nbaz\n")

        r = await tool.executor(_tc("task_read", {"node_id": "a", "section": "foo"}))
        assert r.success
        assert "foo" in r.output
        assert "baz" not in r.output

    async def test_read_without_section(self, mgr):
        tool = make_task_read_tool(mgr)
        mgr.create_graph([{"id": "a", "content": "A"}])
        mgr.mark_completed("a", "full content here")

        r = await tool.executor(_tc("task_read", {"node_id": "a"}))
        assert r.success
        assert r.output == "full content here"
