"""
TaskListManager — session-scoped wrapper around a single active TaskList.

Provides the narrow API used by the task_* tools and the pre-final-response
self-check middleware. No persistence, no artifact overflow, no graph-state
lifecycle — see Issue #153 for the rationale behind removing those.

Long-result storage (overflow) will return in Issue #154 via the Scratchpad.
Until then, results larger than HARD_RESULT_CAP are hard-truncated.
"""

from __future__ import annotations

import logging
from typing import Any

from .tasklist import TaskList, TaskNode, TaskStatus

logger = logging.getLogger(__name__)

SHORT_THRESHOLD = 500
MEDIUM_THRESHOLD = 5000
HARD_RESULT_CAP = 5000


class TaskListManager:
    """Session-scoped manager for a single active TaskList."""

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

    # ── List construction ──────────────────────────────────────────────

    def create_list(self, tasks: list[dict[str, Any]]) -> TaskList:
        """Build a fresh TaskList from agent-provided task specs.

        If a prior list exists but all its nodes are done, it is replaced.
        If it still has active nodes, creation fails — agent must abandon
        or finish first.
        """
        if self._list is not None:
            if all(n.is_done for n in self._list.nodes):
                self._list = None
            else:
                raise ValueError(
                    "A task list already exists with active nodes. "
                    "Use task_modify to adjust, or mark unfinished nodes "
                    "with task_done(error=...) first."
                )

        lst = TaskList()
        seen: set[str] = set()
        for spec in tasks:
            tid = spec["id"]
            if tid in seen:
                raise ValueError(f"Duplicate task ID: '{tid}'")
            seen.add(tid)

        for spec in tasks:
            lst.add(
                node_id=spec["id"],
                content=spec["content"],
                depends_on=spec.get("depends_on", []),
            )
        lst.validate()

        self._list = lst
        return lst

    # ── List mutation ──────────────────────────────────────────────────

    def add_nodes(self, tasks: list[dict[str, Any]]) -> list[TaskNode]:
        self._require_list()
        added: list[TaskNode] = []
        for spec in tasks:
            node = self._list.add(
                node_id=spec["id"],
                content=spec["content"],
                depends_on=spec.get("depends_on", []),
            )
            added.append(node)
        self._list.validate()
        return added

    def remove_nodes(self, node_ids: list[str]) -> None:
        self._require_list()
        for nid in node_ids:
            self._list.remove(nid)

    def update_nodes(self, updates: list[dict[str, Any]]) -> list[TaskNode]:
        self._require_list()
        updated: list[TaskNode] = []
        for spec in updates:
            node = self._list.update(
                node_id=spec["id"],
                content=spec.get("content"),
                depends_on=spec.get("depends_on"),
            )
            updated.append(node)
        if any(spec.get("depends_on") is not None for spec in updates):
            self._list.validate()
        return updated

    # ── Execution support ──────────────────────────────────────────────

    def get_ready_nodes(self) -> list[TaskNode]:
        self._require_list()
        return self._list.ready()

    def mark_in_progress(self, node_id: str) -> TaskNode:
        self._require_list()
        node = self._list.get(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        if node.status != TaskStatus.PENDING:
            raise ValueError(
                f"Cannot start node '{node_id}' with status {node.status.value} "
                f"— only PENDING nodes can be started"
            )
        node.status = TaskStatus.IN_PROGRESS
        return node

    def mark_completed(self, node_id: str, result: str) -> TaskNode:
        """Mark a node as completed. Long results are hard-truncated.

        Issue #154 will reintroduce Scratchpad-backed overflow so that long
        results don't lose information. For now the safe default is truncate
        plus a clear notice, and the agent is nudged to write files directly.
        """
        self._require_list()
        node = self._list.get(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")

        original_len = len(result)
        if original_len > HARD_RESULT_CAP:
            truncated = result[:HARD_RESULT_CAP]
            truncated += (
                f"\n\n[TaskList: result hard-truncated at {HARD_RESULT_CAP} chars "
                f"— original was {original_len} chars. For long outputs, write to "
                f"a file via write_file and record only the path/summary here. "
                f"Scratchpad-backed overflow is tracked in Issue #154.]"
            )
            node.complete(truncated)
        else:
            node.complete(result)

        node.result_summary = self._generate_summary(result, node_id=node_id)
        return node

    def mark_failed(self, node_id: str, error: str) -> TaskNode:
        self._require_list()
        node = self._list.get(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        node.fail(error)
        return node

    def get_node_result(self, node_id: str, section: str | None = None) -> str | None:
        """Return the full result of a completed node (pull model)."""
        self._require_list()
        node = self._list.get(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        if node.status != TaskStatus.COMPLETED:
            return None
        result = node.result
        if result is None:
            return None
        if section is None:
            return result
        return self._apply_section_filter(result, section)

    def build_node_context(self, node: TaskNode) -> str:
        """Build an injection string summarising prior-node results for the agent.

        Returned to the agent via task_done's response so it can include upstream
        context when executing a downstream node. Pull model: the agent sees the
        summary plus a hint to call task_read for full details.
        """
        self._require_list()
        parts = [f"You are executing task node [{node.id}]: {node.content}"]

        for dep_id in node.depends_on:
            dep = self._list.get(dep_id)
            if dep is None or dep.status != TaskStatus.COMPLETED:
                continue
            result_len = len(dep.result) if dep.result else 0
            summary = dep.result_summary or "(no summary)"
            parts.append(
                f"\nPrior task [{dep.id}] ({dep.content[:60]}) completed.\n"
                f"Result summary: {summary}\n"
                f"(full result: ~{result_len} chars"
                f" — use task_read(node_id='{dep.id}') to inspect,"
                f" supports section='head'/'tail'/'10-50'/keyword)"
            )

        return "\n".join(parts)

    # ── Status / query ─────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        self._require_list()
        return self._list.status_summary()

    def abandon(self) -> None:
        """Discard the current list. No cleanup beyond dropping the reference."""
        self._list = None

    # ── Self-check (Issue #153) ────────────────────────────────────────

    def build_self_check_message(self) -> str | None:
        """Build a pre-final-response reminder when active nodes remain.

        Returns None if there is no list or all nodes are done. Otherwise
        returns a reminder the session inserts as a system-style nudge so
        the agent either continues or explicitly marks abandonment.
        """
        if self._list is None:
            return None
        active = self._list.active()
        if not active:
            return None

        lines = [
            f"[TaskList self-check] You still have {len(active)} unfinished node(s):",
        ]
        for n in active:
            deps = f" (deps: {', '.join(n.depends_on)})" if n.depends_on else ""
            lines.append(
                f"  - [{n.id}] {n.content[:80]} — {n.status.value}{deps}"
            )
        lines.append("")
        lines.append(
            "If you intend to finish the task, continue executing the remaining "
            "nodes now. If the task is no longer viable, mark each unfinished "
            "node via task_done(node_id=..., error=\"reason\") so the abandonment "
            "reason is preserved. Do not end this turn silently with work still "
            "pending."
        )
        return "\n".join(lines)

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _apply_section_filter(text: str, section: str) -> str:
        try:
            lines = text.splitlines(keepends=True)
            if section == "head":
                return "".join(lines[:200])
            if section == "tail":
                return "".join(lines[-200:])
            if "-" in section:
                parts = section.split("-", 1)
                try:
                    start = max(1, int(parts[0])) - 1
                    end = int(parts[1])
                    return "".join(lines[start:end])
                except (ValueError, IndexError):
                    pass
            matched = [ln for ln in lines if section.lower() in ln.lower()]
            if matched:
                return "".join(matched)
            return f"(no lines matching '{section}')"
        except Exception as exc:
            return f"(section filter error: {exc})"

    def _require_list(self) -> None:
        if self._list is None:
            raise ValueError("No active task list. Use task_plan to create one.")

    @staticmethod
    def _generate_summary(result: str, node_id: str = "") -> str:
        if not result:
            return "(empty result)"
        length = len(result)
        if length <= SHORT_THRESHOLD:
            return result
        read_hint = (
            f"\nFull result ({length} chars) available — "
            f"use task_read(node_id='{node_id}') to retrieve, "
            f"supports section='head'/'tail'/'10-50'/keyword"
        ) if node_id else f"\n... ({length} chars total, use task_read for full result)"
        if length <= MEDIUM_THRESHOLD:
            return result[:400] + read_hint
        head = result[:300]
        tail = result[-200:]
        return (
            f"{head}\n"
            f"... ({length} chars total) ...\n"
            f"{tail}"
            f"{read_hint}"
        )
