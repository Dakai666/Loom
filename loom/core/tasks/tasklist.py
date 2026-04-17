"""
TaskList — an agent's cognitive checklist for multi-step work.

Each node represents one task with a status and optional textual dependencies.
Dependencies (`depends_on`) are metadata for the agent's own reference — they
do NOT drive execution. The main agent executes nodes itself, one turn at a
time, using task_* tools to track progress and self-check before finishing.

Design origin: Issue #153. Supersedes the TaskGraph execution framework from
#128 / #150, whose compile()/scheduler/asyncio.gather path was never wired up
and caused silent stalls under autonomy (e.g. graph 66859851, 2026-04-17).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskNode:
    id: str
    content: str
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = field(default_factory=list)
    result: str | None = None
    result_summary: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_done(self) -> bool:
        return self.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)

    @property
    def is_active(self) -> bool:
        return self.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)

    def complete(self, result: str | None = None) -> None:
        self.status = TaskStatus.COMPLETED
        self.result = result

    def fail(self, error: str) -> None:
        self.status = TaskStatus.FAILED
        self.error = error


class TaskList:
    """Ordered collection of task nodes with textual dependencies.

    Dependencies do not participate in execution — they are documentation for
    the agent's own reference. There is no compiler, no scheduler, no graph
    topology; the agent reads the list and decides what to do next.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, TaskNode] = {}

    def add(
        self,
        node_id: str,
        content: str,
        depends_on: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskNode:
        if node_id in self._nodes:
            raise ValueError(f"Node ID '{node_id}' already exists")
        node = TaskNode(
            id=node_id,
            content=content,
            depends_on=list(depends_on or []),
            metadata=dict(metadata or {}),
        )
        self._nodes[node_id] = node
        return node

    def remove(self, node_id: str) -> None:
        node = self._nodes.get(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        if node.status != TaskStatus.PENDING:
            raise ValueError(
                f"Cannot remove node '{node_id}' with status {node.status.value} "
                f"— only PENDING nodes can be removed"
            )
        del self._nodes[node_id]
        for n in self._nodes.values():
            if node_id in n.depends_on:
                n.depends_on.remove(node_id)

    def update(
        self,
        node_id: str,
        content: str | None = None,
        depends_on: list[str] | None = None,
    ) -> TaskNode:
        node = self._nodes.get(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        if node.status != TaskStatus.PENDING:
            raise ValueError(
                f"Cannot update node '{node_id}' with status {node.status.value} "
                f"— only PENDING nodes can be updated"
            )
        if content is not None:
            node.content = content
        if depends_on is not None:
            for dep in depends_on:
                if dep not in self._nodes:
                    raise ValueError(f"Dependency '{dep}' not found in list")
            node.depends_on = list(depends_on)
        return node

    def get(self, node_id: str) -> TaskNode | None:
        return self._nodes.get(node_id)

    @property
    def nodes(self) -> list[TaskNode]:
        return list(self._nodes.values())

    def pending(self) -> list[TaskNode]:
        return [n for n in self._nodes.values() if n.status == TaskStatus.PENDING]

    def active(self) -> list[TaskNode]:
        return [n for n in self._nodes.values() if n.is_active]

    def ready(self) -> list[TaskNode]:
        """PENDING nodes whose `depends_on` are all COMPLETED.

        Convenience view for the agent. Since dependencies are advisory,
        the agent may still choose to execute out of order.
        """
        out: list[TaskNode] = []
        for n in self._nodes.values():
            if n.status != TaskStatus.PENDING:
                continue
            deps_met = all(
                (dep := self._nodes.get(d)) is not None
                and dep.status == TaskStatus.COMPLETED
                for d in n.depends_on
            )
            if deps_met:
                out.append(n)
        return out

    def validate(self) -> None:
        """Check that all depends_on references resolve and no cycles exist.

        Cycles are a plan bug even when dependencies are advisory — catching
        them prevents the agent silently assuming A needs B and B needs A.
        """
        for n in self._nodes.values():
            for dep in n.depends_on:
                if dep not in self._nodes:
                    raise ValueError(
                        f"Node '{n.id}' depends on unknown node '{dep}'"
                    )
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {nid: WHITE for nid in self._nodes}

        def visit(nid: str, chain: list[str]) -> None:
            if color[nid] == GRAY:
                cycle = " -> ".join(chain + [nid])
                raise ValueError(f"Cycle detected in task dependencies: {cycle}")
            if color[nid] == BLACK:
                return
            color[nid] = GRAY
            for dep in self._nodes[nid].depends_on:
                visit(dep, chain + [nid])
            color[nid] = BLACK

        for nid in list(self._nodes):
            visit(nid, [])

    def status_summary(self) -> dict[str, Any]:
        by_status: dict[str, list[str]] = {}
        for n in self._nodes.values():
            by_status.setdefault(n.status.value, []).append(n.id)
        return {
            "total_nodes": len(self._nodes),
            "by_status": by_status,
            "nodes": [
                {
                    "id": n.id,
                    "content": n.content[:80],
                    "status": n.status.value,
                    "depends_on": n.depends_on,
                    **({"result_summary": n.result_summary} if n.result_summary else {}),
                    **({"error": n.error} if n.error else {}),
                }
                for n in self._nodes.values()
            ],
        }
