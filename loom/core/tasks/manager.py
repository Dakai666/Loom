"""
TaskListManager — session-scoped wrapper around the active TaskList.

After Issue #205 collapse: this is a thin wrapper that exposes only what
the single ``task_write`` tool and the turn-boundary self-check middleware
need. No result storage, no overflow handling, no per-node lifecycle —
those concepts went away with ``task_done``.
"""

from __future__ import annotations

from .tasklist import TaskList


class TaskListManager:
    """Session-scoped wrapper for one TaskList."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._list: TaskList | None = None

    @property
    def tasklist(self) -> TaskList | None:
        return self._list

    @property
    def has_list(self) -> bool:
        return self._list is not None

    def has_active_nodes(self) -> bool:
        return self._list is not None and bool(self._list.active())

    def write(self, todos: list[dict]) -> dict:
        """Replace the entire list with the agent-provided todos.

        Empty list clears the active TaskList. Returns the new status
        summary so the tool layer can echo it back to the agent.
        """
        if not todos:
            self._list = None
            return {"total": 0, "by_status": {}, "todos": []}
        lst = TaskList()
        lst.replace(todos)
        self._list = lst
        return lst.status_summary()

    def status(self) -> dict:
        if self._list is None:
            return {"total": 0, "by_status": {}, "todos": []}
        return self._list.status_summary()

    def build_self_check_message(self) -> str | None:
        """Build a pre-final-response reminder when active nodes remain.

        Returns None if there is no list or all nodes are done. Otherwise
        returns a reminder the session inserts as a system-style nudge so
        the agent either continues or rewrites the list to mark closure.
        """
        if self._list is None:
            return None
        active = self._list.active()
        if not active:
            return None

        lines = [
            f"[TaskList self-check] You still have {len(active)} unfinished todo(s):",
        ]
        for n in active:
            lines.append(f"  - [{n.id}] {n.content[:80]} — {n.status.value}")
        lines.append("")
        lines.append(
            "Either continue executing now, or — if the work is no longer "
            "viable — call task_write again with the full updated list, "
            "marking the abandoned items as 'completed' with the reason "
            "stated in their content. Do not end this turn silently with "
            "todos still pending."
        )
        return "\n".join(lines)
