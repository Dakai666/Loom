"""
Tests for the DAG Task Engine and Scheduler:
  - TaskNode: status transitions, is_done
  - TaskGraph: add, compile (Kahn's algorithm), cycle detection
  - ExecutionPlan: levels, parallel_groups, all_nodes
  - TaskScheduler: sequential and parallel execution, stop_on_failure
"""

import asyncio
import pytest

from loom.core.tasks.graph import (
    TaskGraph, TaskNode, TaskStatus, ExecutionPlan,
)
from loom.core.tasks.scheduler import TaskScheduler


# ---------------------------------------------------------------------------
# TaskNode
# ---------------------------------------------------------------------------

class TestTaskNode:
    def test_default_status_is_pending(self):
        n = TaskNode(content="do something")
        assert n.status == TaskStatus.PENDING

    def test_complete_sets_status_and_result(self):
        n = TaskNode(content="x")
        n.complete(result="output")
        assert n.status == TaskStatus.COMPLETED
        assert n.result == "output"

    def test_fail_sets_status_and_error(self):
        n = TaskNode(content="x")
        n.fail("boom")
        assert n.status == TaskStatus.FAILED
        assert n.error == "boom"

    def test_skip_sets_status(self):
        n = TaskNode(content="x")
        n.skip()
        assert n.status == TaskStatus.SKIPPED

    def test_is_done_for_completed(self):
        n = TaskNode(content="x")
        n.complete()
        assert n.is_done is True

    def test_is_done_for_failed(self):
        n = TaskNode(content="x")
        n.fail("e")
        assert n.is_done is True

    def test_is_done_for_skipped(self):
        n = TaskNode(content="x")
        n.skip()
        assert n.is_done is True

    def test_not_done_when_pending(self):
        n = TaskNode(content="x")
        assert n.is_done is False

    def test_not_done_when_in_progress(self):
        n = TaskNode(content="x")
        n.status = TaskStatus.IN_PROGRESS
        assert n.is_done is False

    def test_auto_id_is_unique(self):
        ids = {TaskNode(content="x").id for _ in range(50)}
        assert len(ids) == 50


# ---------------------------------------------------------------------------
# TaskGraph — structure
# ---------------------------------------------------------------------------

class TestTaskGraph:
    def test_add_creates_node(self):
        g = TaskGraph()
        n = g.add("step A")
        assert n.content == "step A"
        assert n in g.nodes

    def test_depends_on_stored_as_ids(self):
        g = TaskGraph()
        a = g.add("A")
        b = g.add("B", depends_on=[a])
        assert a.id in b.depends_on

    def test_get_by_id(self):
        g = TaskGraph()
        n = g.add("task")
        assert g.get(n.id) is n

    def test_get_missing_returns_none(self):
        g = TaskGraph()
        assert g.get("nonexistent") is None

    def test_pending_returns_only_pending(self):
        g = TaskGraph()
        a = g.add("A")
        b = g.add("B")
        a.complete()
        pending = g.pending()
        assert a not in pending
        assert b in pending

    def test_reset_returns_all_to_pending(self):
        g = TaskGraph()
        a = g.add("A")
        b = g.add("B")
        a.complete()
        b.fail("e")
        g.reset()
        assert all(n.status == TaskStatus.PENDING for n in g.nodes)


# ---------------------------------------------------------------------------
# TaskGraph — compile / topological sort
# ---------------------------------------------------------------------------

class TestTaskGraphCompile:
    def test_single_node(self):
        g = TaskGraph()
        a = g.add("A")
        plan = g.compile()
        assert len(plan.levels) == 1
        assert plan.levels[0] == [a]

    def test_linear_chain(self):
        g = TaskGraph()
        a = g.add("A")
        b = g.add("B", depends_on=[a])
        c = g.add("C", depends_on=[b])
        plan = g.compile()
        assert len(plan.levels) == 3
        assert plan.levels[0] == [a]
        assert plan.levels[1] == [b]
        assert plan.levels[2] == [c]

    def test_parallel_nodes_same_level(self):
        """Nodes with same dependencies land in the same level."""
        g = TaskGraph()
        a = g.add("A")
        b = g.add("B", depends_on=[a])
        c = g.add("C", depends_on=[a])  # parallel to b
        plan = g.compile()
        assert len(plan.levels) == 2
        # Level 0: a; Level 1: b and c (order may vary)
        assert set(n.id for n in plan.levels[1]) == {b.id, c.id}

    def test_diamond_dependency(self):
        """Classic diamond: A → B, A → C; B → D, C → D"""
        g = TaskGraph()
        a = g.add("A")
        b = g.add("B", depends_on=[a])
        c = g.add("C", depends_on=[a])
        d = g.add("D", depends_on=[b, c])
        plan = g.compile()
        assert len(plan.levels) == 3
        assert plan.levels[0] == [a]
        assert set(n.id for n in plan.levels[1]) == {b.id, c.id}
        assert plan.levels[2] == [d]

    def test_complex_graph_from_plan_md_example(self):
        g = TaskGraph()
        a = g.add("Analyse existing API")
        b = g.add("Design schema",    depends_on=[a])
        c = g.add("Implement",        depends_on=[b])
        d = g.add("Write tests",      depends_on=[b])
        e = g.add("Update docs",      depends_on=[c, d])
        plan = g.compile()
        assert len(plan.levels) == 4
        # c and d are parallel
        assert len(plan.levels[2]) == 2
        assert plan.levels[3] == [e]

    def test_all_nodes_visited(self):
        g = TaskGraph()
        nodes = [g.add(f"task-{i}") for i in range(10)]
        plan = g.compile()
        assert len(plan.all_nodes) == 10

    def test_cycle_raises_value_error(self):
        g = TaskGraph()
        a = g.add("A")
        b = g.add("B", depends_on=[a])
        # Manually inject a cycle: A depends on B
        a.depends_on.append(b.id)
        with pytest.raises(ValueError, match="Cycle"):
            g.compile()

    def test_unknown_dependency_raises(self):
        g = TaskGraph()
        n = g.add("A")
        n.depends_on.append("ghost-id")
        with pytest.raises(ValueError, match="unknown"):
            g.compile()


# ---------------------------------------------------------------------------
# ExecutionPlan
# ---------------------------------------------------------------------------

class TestExecutionPlan:
    def _make_plan(self):
        g = TaskGraph()
        a = g.add("A")
        b = g.add("B", depends_on=[a])
        c = g.add("C", depends_on=[a])
        d = g.add("D", depends_on=[b, c])
        return g.compile()

    def test_parallel_groups_only_multi_node_levels(self):
        plan = self._make_plan()
        groups = plan.parallel_groups
        # Only level 1 (b, c) is parallel
        assert len(groups) == 1
        assert len(groups[0]) == 2

    def test_str_representation(self):
        plan = self._make_plan()
        s = str(plan)
        assert "ExecutionPlan" in s
        assert "L0" in s

    def test_all_nodes_count(self):
        plan = self._make_plan()
        assert len(plan.all_nodes) == 4


# ---------------------------------------------------------------------------
# TaskScheduler
# ---------------------------------------------------------------------------

class TestTaskScheduler:
    @pytest.mark.asyncio
    async def test_all_nodes_completed(self):
        async def executor(node):
            return f"result:{node.content}"

        g = TaskGraph()
        a = g.add("A")
        b = g.add("B", depends_on=[a])
        plan = g.compile()

        scheduler = TaskScheduler(executor=executor)
        nodes = await scheduler.run(plan)

        assert all(n.status == TaskStatus.COMPLETED for n in nodes)
        assert a.result == "result:A"
        assert b.result == "result:B"

    @pytest.mark.asyncio
    async def test_parallel_level_runs_concurrently(self):
        """B and C should start at roughly the same time."""
        start_times: list[float] = []

        async def executor(node):
            import time
            start_times.append(time.monotonic())
            await asyncio.sleep(0.05)
            return node.content

        g = TaskGraph()
        a = g.add("A")
        b = g.add("B", depends_on=[a])
        c = g.add("C", depends_on=[a])
        plan = g.compile()

        await TaskScheduler(executor=executor).run(plan)

        # B and C are in the same level; their starts should be very close
        assert len(start_times) == 3
        # The last two starts (B and C) differ by less than 30ms
        assert abs(start_times[1] - start_times[2]) < 0.03

    @pytest.mark.asyncio
    async def test_failing_node_records_error(self):
        async def executor(node):
            if node.content == "bad":
                raise ValueError("intentional failure")
            return "ok"

        g = TaskGraph()
        a = g.add("good")
        b = g.add("bad")
        plan = g.compile()

        await TaskScheduler(executor=executor, stop_on_failure=False).run(plan)

        assert a.status == TaskStatus.COMPLETED
        assert b.status == TaskStatus.FAILED
        assert "intentional" in b.error

    @pytest.mark.asyncio
    async def test_stop_on_failure_skips_downstream(self):
        async def executor(node):
            if node.content == "fail_here":
                raise RuntimeError("stop")
            return "ok"

        g = TaskGraph()
        a = g.add("fail_here")
        b = g.add("downstream", depends_on=[a])
        plan = g.compile()

        await TaskScheduler(executor=executor, stop_on_failure=True).run(plan)

        assert a.status == TaskStatus.FAILED
        assert b.status == TaskStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_returns_all_nodes(self):
        async def executor(node):
            return node.id

        g = TaskGraph()
        for i in range(5):
            g.add(f"t{i}")
        plan = g.compile()
        nodes = await TaskScheduler(executor=executor).run(plan)
        assert len(nodes) == 5
