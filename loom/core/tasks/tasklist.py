"""
TaskList — the agent's cognitive checklist for multi-step work.

Each node is just an id, a one-line content, and a status. There are no
result fields, no dependencies, no readiness computation. The whole list
is replaced atomically each time the agent calls ``task_write`` — the
act of marking a node ``completed`` is editing the data structure, not
calling a "done" verb. This avoids the cognitive substitution where
``task_done(...)`` feels like reporting progress to the framework even
when no artifact exists (Issue #205).

All real outputs go to disk via ``write_file``. The TaskList tracks only
"have I forgotten this step?", never "what did this step produce?".

Design origin: Issue #153 introduced the agent-driven model. Issue #205
collapsed it further to a single ``task_write`` tool after observing
``task_done`` was the source of false-completion failures.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass
class TaskNode:
    id: str
    content: str
    status: TaskStatus = TaskStatus.PENDING

    @property
    def is_active(self) -> bool:
        return self.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)


class TaskList:
    """Ordered collection of task nodes — a sticky-note board, nothing more.

    Replace-style: the agent submits the full intended state every time;
    the list mirrors what was submitted. No partial mutations, no per-node
    APIs, no execution scheduling.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, TaskNode] = {}

    def replace(self, todos: list[dict]) -> None:
        """Replace the entire list with the agent-provided todos.

        Each todo is ``{"id", "content", "status"}``. Order is preserved
        from the input. Validates: non-empty unique ids, non-empty content,
        recognised status values.
        """
        seen: set[str] = set()
        new_nodes: dict[str, TaskNode] = {}
        for i, spec in enumerate(todos):
            tid = (spec.get("id") or "").strip()
            content = (spec.get("content") or "").strip()
            status_str = (spec.get("status") or "pending").strip().lower()
            if not tid:
                raise ValueError(f"todo at index {i} is missing 'id'")
            if not content:
                raise ValueError(f"todo {tid!r} is missing 'content'")
            if tid in seen:
                raise ValueError(f"duplicate id {tid!r}")
            seen.add(tid)
            try:
                status = TaskStatus(status_str)
            except ValueError:
                raise ValueError(
                    f"todo {tid!r} has unknown status {status_str!r} "
                    f"(expected one of: pending, in_progress, completed)"
                ) from None
            new_nodes[tid] = TaskNode(id=tid, content=content, status=status)
        self._nodes = new_nodes

    @property
    def nodes(self) -> list[TaskNode]:
        return list(self._nodes.values())

    def active(self) -> list[TaskNode]:
        return [n for n in self._nodes.values() if n.is_active]

    def status_summary(self) -> dict:
        by_status: dict[str, list[str]] = {}
        for n in self._nodes.values():
            by_status.setdefault(n.status.value, []).append(n.id)
        return {
            "total": len(self._nodes),
            "by_status": by_status,
            "todos": [
                {"id": n.id, "content": n.content, "status": n.status.value}
                for n in self._nodes.values()
            ],
        }
