"""
TaskListManager — session-scoped wrapper around the active TaskList.

After Issue #205 collapse: this is a thin wrapper that exposes only what
the single ``task_write`` tool and the turn-boundary self-check middleware
need. No result storage, no overflow handling, no per-node lifecycle —
those concepts went away with ``task_done``.

Issue #207: Discord reminder embed. When ``discord_client`` is provided,
``write()`` returns an extra ``_discord_reminder`` key that the caller
(``session.py``) uses to post a checkbox embed to the Discord thread.
"""

from __future__ import annotations

from .tasklist import TaskList, TaskStatus


class TaskListManager:
    """Session-scoped wrapper for one TaskList."""

    def __init__(
        self,
        session_id: str,
        discord_client=None,
        discord_thread_id: int | None = None,
    ) -> None:
        self.session_id = session_id
        self._list: TaskList | None = None
        self._discord_client = discord_client
        self._discord_thread_id = discord_thread_id

    @property
    def tasklist(self) -> TaskList | None:
        return self._list

    @property
    def has_list(self) -> bool:
        return self._list is not None

    def has_active_nodes(self) -> bool:
        return self._list is not None and bool(self._list.active())

    # ── Issue #207 ──────────────────────────────────────────────────────────

    def _build_reminder_embed(self) -> dict | None:
        """Build a Discord reminder embed dict for the current list state.

        Returns None if Discord client is not available (CLI mode).
        Returns the embed dict ready to pass to send_discord_embed.
        """
        if self._discord_client is None or self._discord_thread_id is None:
            return None

        if self._list is None:
            return None

        todos = self._list.nodes
        completed = [n for n in todos if n.status.value == "completed"]
        in_progress = [n for n in todos if n.status.value == "in_progress"]
        pending = [n for n in todos if n.status.value == "pending"]

        total = len(todos)
        done_count = len(completed)

        lines_completed = "  ".join(f"✅ {n.id}" for n in completed) if completed else None
        lines_progress = "  ".join(f"[→] {n.id}" for n in in_progress) if in_progress else None
        lines_pending = "  ".join(f"[ ] {n.id}" for n in pending) if pending else None

        description_parts = []
        if lines_completed:
            description_parts.append(lines_completed)
        if lines_progress:
            description_parts.append(lines_progress)
        if lines_pending:
            description_parts.append(lines_pending)

        import datetime
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M UTC")

        return {
            "title": f"🔄 任務進度 — {done_count}/{total} 完成",
            "description": "\n".join(description_parts),
            "color": "#4CAF93",
            "fields": [
                {
                    "name": "📝 觸發",
                    "value": f"task_write 更新 · {timestamp}",
                    "inline": True,
                }
            ],
        }

    # ── Core API ────────────────────────────────────────────────────────────

    def write(self, todos: list[dict]) -> dict:
        """Replace the entire list with the agent-provided todos.

        Empty list clears the active TaskList. Returns the new status
        summary. If Discord is configured, also returns ``_discord_reminder``
        (an embed dict or None) that the caller should post.
        """
        if not todos:
            self._list = None
            return {"total": 0, "by_status": {}, "todos": []}

        lst = TaskList()
        lst.replace(todos)
        self._list = lst
        result = lst.status_summary()

        # Issue #207: embed lives alongside the summary
        result["_discord_reminder"] = self._build_reminder_embed()
        return result

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
