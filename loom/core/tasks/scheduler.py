"""
Task Scheduler — executes an ExecutionPlan level by level,
running nodes within each level concurrently via asyncio.gather.

Usage
-----
    async def my_executor(node: TaskNode) -> Any:
        # do work, return result
        return f"done: {node.content}"

    scheduler = TaskScheduler(executor=my_executor)
    await scheduler.run(plan)
    # All nodes now have status COMPLETED or FAILED
"""

import asyncio
from typing import Any, Callable, Awaitable

from .graph import ExecutionPlan, TaskNode, TaskStatus


ExecutorFn = Callable[[TaskNode], Awaitable[Any]]


class TaskScheduler:
    """
    Runs an ExecutionPlan concurrently within each level.

    If a node fails and `stop_on_failure=True`, all downstream nodes
    in later levels are skipped.
    """

    def __init__(
        self,
        executor: ExecutorFn,
        stop_on_failure: bool = False,
    ) -> None:
        self._executor = executor
        self._stop_on_failure = stop_on_failure

    async def run(self, plan: ExecutionPlan) -> list[TaskNode]:
        """
        Execute all levels in order.  Returns the list of all nodes
        with their final statuses set.
        """
        failed_any = False

        for level in plan.levels:
            if failed_any and self._stop_on_failure:
                for node in level:
                    node.skip()
                continue

            results = await asyncio.gather(
                *[self._run_node(node) for node in level],
                return_exceptions=True,
            )

            for node, result in zip(level, results):
                if isinstance(result, Exception):
                    node.fail(str(result))
                    failed_any = True

        return plan.all_nodes

    async def _run_node(self, node: TaskNode) -> Any:
        node.status = TaskStatus.IN_PROGRESS
        try:
            result = await self._executor(node)
            node.complete(result)
            return result
        except Exception as exc:
            node.fail(str(exc))
            raise
