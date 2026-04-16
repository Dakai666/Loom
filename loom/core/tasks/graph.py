"""
DAG Task Engine.

Tasks are represented as nodes in a Directed Acyclic Graph (DAG).
Dependencies are declared at creation time; the graph then compiles
the optimal execution plan using Kahn's topological sort algorithm,
which naturally groups independent nodes into parallel levels.

Example
-------
    g = TaskGraph()
    a = g.add("Analyse existing API")
    b = g.add("Design schema",   depends_on=[a])
    c = g.add("Implement",       depends_on=[b])
    d = g.add("Write tests",     depends_on=[b])   # c and d are independent
    e = g.add("Update docs",     depends_on=[c, d])

    plan = g.compile()
    # plan.levels == [[a], [b], [c, d], [e]]
    # plan.parallel_groups == [[c, d]]
"""

from __future__ import annotations

import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(Enum):
    PENDING    = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED  = "completed"
    FAILED     = "failed"
    SKIPPED    = "skipped"


@dataclass
class TaskNode:
    content: str
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = field(default_factory=list)  # list of node IDs
    result: Any = None
    result_summary: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_done(self) -> bool:
        return self.status in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                               TaskStatus.SKIPPED)

    def complete(self, result: Any = None) -> None:
        self.status = TaskStatus.COMPLETED
        self.result = result

    def fail(self, error: str) -> None:
        self.status = TaskStatus.FAILED
        self.error = error

    def skip(self) -> None:
        self.status = TaskStatus.SKIPPED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status.value,
            "depends_on": self.depends_on,
            "result": self.result,
            "result_summary": self.result_summary,
            "error": self.error,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskNode:
        node = cls(
            content=data["content"],
            id=data["id"],
            status=TaskStatus(data["status"]),
            depends_on=data.get("depends_on", []),
            result=data.get("result"),
            result_summary=data.get("result_summary"),
            error=data.get("error"),
            metadata=data.get("metadata", {}),
        )
        return node


@dataclass
class ExecutionPlan:
    """
    The compiled execution plan for a TaskGraph.

    `levels` is a list-of-lists produced by topological sort.
    Each level contains nodes that can run in parallel because
    all their dependencies are in earlier levels.
    """
    levels: list[list[TaskNode]]

    @property
    def parallel_groups(self) -> list[list[TaskNode]]:
        """Return only levels with more than one node (true parallelism)."""
        return [lvl for lvl in self.levels if len(lvl) > 1]

    @property
    def all_nodes(self) -> list[TaskNode]:
        return [node for level in self.levels for node in level]

    def __str__(self) -> str:
        lines = ["ExecutionPlan:"]
        for i, level in enumerate(self.levels):
            parallel = "∥" if len(level) > 1 else " "
            tasks = ", ".join(f"[{n.id}] {n.content[:40]}" for n in level)
            lines.append(f"  L{i} {parallel} {tasks}")
        return "\n".join(lines)


class TaskGraph:
    """
    Build and compile a dependency graph of tasks.

    Nodes are added with `add()`; compile() produces an ExecutionPlan
    via Kahn's algorithm.  Raises ValueError if a cycle is detected.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, TaskNode] = {}

    def add(
        self,
        content: str,
        depends_on: list[TaskNode] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskNode:
        node = TaskNode(
            content=content,
            depends_on=[n.id for n in (depends_on or [])],
            metadata=metadata or {},
        )
        self._nodes[node.id] = node
        return node

    def add_with_id(
        self,
        node_id: str,
        content: str,
        depends_on: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskNode:
        """Add a node with an explicit user-chosen ID (for agent-driven construction)."""
        if node_id in self._nodes:
            raise ValueError(f"Node ID '{node_id}' already exists in the graph")
        node = TaskNode(
            content=content,
            id=node_id,
            depends_on=depends_on or [],
            metadata=metadata or {},
        )
        self._nodes[node.id] = node
        return node

    def remove(self, node_id: str) -> None:
        """Remove a PENDING node and clean up references to it."""
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

    def update_node(self, node_id: str, content: str | None = None,
                    depends_on: list[str] | None = None) -> TaskNode:
        """Update content or dependencies of a PENDING node."""
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
            for dep_id in depends_on:
                if dep_id not in self._nodes:
                    raise ValueError(f"Dependency '{dep_id}' not found in graph")
            node.depends_on = depends_on
        return node

    def get(self, node_id: str) -> TaskNode | None:
        return self._nodes.get(node_id)

    @property
    def nodes(self) -> list[TaskNode]:
        return list(self._nodes.values())

    def pending(self) -> list[TaskNode]:
        return [n for n in self._nodes.values() if n.status == TaskStatus.PENDING]

    def ready(self) -> list[TaskNode]:
        """Return PENDING nodes whose dependencies are all COMPLETED."""
        ready = []
        for node in self._nodes.values():
            if node.status != TaskStatus.PENDING:
                continue
            deps_met = all(
                self._nodes.get(dep_id) is not None
                and self._nodes[dep_id].status == TaskStatus.COMPLETED
                for dep_id in node.depends_on
            )
            if deps_met:
                ready.append(node)
        return ready

    def compile(self) -> ExecutionPlan:
        """
        Topological sort (Kahn's algorithm) → levels of parallel nodes.

        Raises ValueError if a cycle is detected.
        """
        nodes = self._nodes

        # in-degree map
        in_degree: dict[str, int] = {nid: 0 for nid in nodes}
        children: dict[str, list[str]] = defaultdict(list)

        for nid, node in nodes.items():
            for dep_id in node.depends_on:
                if dep_id not in nodes:
                    raise ValueError(
                        f"Node '{nid}' depends on unknown node '{dep_id}'"
                    )
                in_degree[nid] += 1
                children[dep_id].append(nid)

        queue: deque[str] = deque(
            nid for nid, deg in in_degree.items() if deg == 0
        )
        levels: list[list[TaskNode]] = []
        visited = 0

        while queue:
            level_size = len(queue)
            level: list[TaskNode] = []
            for _ in range(level_size):
                nid = queue.popleft()
                level.append(nodes[nid])
                visited += 1
                for child_id in children[nid]:
                    in_degree[child_id] -= 1
                    if in_degree[child_id] == 0:
                        queue.append(child_id)
            levels.append(level)

        if visited != len(nodes):
            raise ValueError(
                "Cycle detected in TaskGraph — cannot produce a valid execution plan."
            )

        return ExecutionPlan(levels=levels)

    def reset(self) -> None:
        """Reset all nodes to PENDING (for replanning)."""
        for node in self._nodes.values():
            node.status = TaskStatus.PENDING
            node.result = None
            node.result_summary = None
            node.error = None

    # ── Serialization ──────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self._nodes.values()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskGraph:
        graph = cls()
        for node_data in data["nodes"]:
            node = TaskNode.from_dict(node_data)
            graph._nodes[node.id] = node
        return graph

    def status_summary(self) -> dict[str, Any]:
        """Return a compact summary of graph state for the agent."""
        by_status: dict[str, list[str]] = {}
        for node in self._nodes.values():
            by_status.setdefault(node.status.value, []).append(node.id)

        plan = self.compile()
        return {
            "total_nodes": len(self._nodes),
            "levels": len(plan.levels),
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
