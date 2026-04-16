"""
TaskGraphManager — session-scoped lifecycle manager for agent-driven TaskGraphs.

Owns the active TaskGraph for a session, handles persistence, generates
result summaries, and provides the query interface used by task_* tools.

Design principles (Issue #128):
- Pull model: downstream nodes receive summaries, not full results.
  Agent uses task_read to pull full results on demand.
- 1 node = 1 turn: each node maps to a single agent turn.
- Mutable graph: pending nodes can be added/removed/updated at any time.
- Failure → agent decides: harness never auto-retries, yields control back.
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path
from typing import Any

from .graph import TaskGraph, TaskNode, TaskStatus

logger = logging.getLogger(__name__)

# Result size thresholds for summary generation
_SHORT_THRESHOLD = 500     # tokens ≈ chars for rough estimate
_MEDIUM_THRESHOLD = 5000


class GraphState(Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    SUSPENDED = "suspended"


class TaskGraphManager:
    """Session-scoped manager for a single active TaskGraph."""

    def __init__(self, session_id: str, persist_dir: Path | None = None) -> None:
        self.session_id = session_id
        self._graph: TaskGraph | None = None
        self._state: GraphState = GraphState.ACTIVE
        self._persist_dir = persist_dir or (Path.home() / ".loom" / "task_graphs")

    @property
    def graph(self) -> TaskGraph | None:
        return self._graph

    @property
    def has_graph(self) -> bool:
        return self._graph is not None

    @property
    def state(self) -> GraphState:
        return self._state

    # ── Graph construction ─────────────────────────────────────────────

    def create_graph(self, tasks: list[dict[str, Any]]) -> TaskGraph:
        """Build a TaskGraph from agent-provided task specs.

        Each task dict has: id, content, depends_on? (list of IDs).
        """
        if self._graph is not None:
            raise ValueError(
                "A task graph already exists for this session. "
                "Use task_modify to change it, or complete/abandon it first."
            )

        graph = TaskGraph()
        # First pass: validate no duplicate IDs
        seen_ids: set[str] = set()
        for spec in tasks:
            tid = spec["id"]
            if tid in seen_ids:
                raise ValueError(f"Duplicate task ID: '{tid}'")
            seen_ids.add(tid)

        # Second pass: add nodes (dependencies reference IDs, not TaskNode objects)
        for spec in tasks:
            graph.add_with_id(
                node_id=spec["id"],
                content=spec["content"],
                depends_on=spec.get("depends_on", []),
            )

        # Validate the graph compiles (catches cycles + missing deps)
        graph.compile()

        self._graph = graph
        self._state = GraphState.ACTIVE
        self._persist()
        return graph

    # ── Graph mutation ─────────────────────────────────────────────────

    def add_nodes(self, tasks: list[dict[str, Any]]) -> list[TaskNode]:
        """Add new nodes to the active graph."""
        self._require_graph()
        added = []
        for spec in tasks:
            node = self._graph.add_with_id(
                node_id=spec["id"],
                content=spec["content"],
                depends_on=spec.get("depends_on", []),
            )
            added.append(node)
        # Re-validate after mutation
        self._graph.compile()
        self._persist()
        return added

    def remove_nodes(self, node_ids: list[str]) -> None:
        """Remove pending nodes from the active graph."""
        self._require_graph()
        for nid in node_ids:
            self._graph.remove(nid)
        self._persist()

    def update_nodes(self, updates: list[dict[str, Any]]) -> list[TaskNode]:
        """Update pending nodes in the active graph."""
        self._require_graph()
        updated = []
        for spec in updates:
            node = self._graph.update_node(
                node_id=spec["id"],
                content=spec.get("content"),
                depends_on=spec.get("depends_on"),
            )
            updated.append(node)
        if any(spec.get("depends_on") is not None for spec in updates):
            self._graph.compile()  # re-validate topology
        self._persist()
        return updated

    # ── Execution support ──────────────────────────────────────────────

    def get_ready_nodes(self) -> list[TaskNode]:
        """Return nodes ready to execute (all deps completed)."""
        self._require_graph()
        return self._graph.ready()

    def mark_completed(self, node_id: str, result: str) -> TaskNode:
        """Mark a node as completed, store result and generate summary."""
        self._require_graph()
        node = self._graph.get(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        node.complete(result)
        node.result_summary = self._generate_summary(result)
        self._check_graph_completion()
        self._persist()
        return node

    def mark_failed(self, node_id: str, error: str) -> TaskNode:
        """Mark a node as failed."""
        self._require_graph()
        node = self._graph.get(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        node.fail(error)
        self._check_graph_completion()
        self._persist()
        return node

    def get_node_result(self, node_id: str) -> str | None:
        """Return full result of a completed node (pull model)."""
        self._require_graph()
        node = self._graph.get(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        if node.status != TaskStatus.COMPLETED:
            return None
        return node.result

    def build_node_context(self, node: TaskNode) -> str:
        """Build injection context for a node about to execute.

        For each dependency, includes the result summary and a hint
        to use task_read for full details (Pull Model).
        """
        self._require_graph()
        parts = [f"You are executing task node [{node.id}]: {node.content}"]

        for dep_id in node.depends_on:
            dep = self._graph.get(dep_id)
            if dep is None or dep.status != TaskStatus.COMPLETED:
                continue
            result_len = len(dep.result) if dep.result else 0
            summary = dep.result_summary or "(no summary)"
            parts.append(
                f"\nPrior task [{dep.id}] ({dep.content[:60]}) completed.\n"
                f"Result summary: {summary}\n"
                f"(full result: ~{result_len} chars — use task_read(node_id='{dep.id}') to inspect)"
            )

        return "\n".join(parts)

    # ── Status / query ─────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """Return graph status summary for the agent."""
        self._require_graph()
        summary = self._graph.status_summary()
        summary["graph_state"] = self._state.value
        return summary

    def abandon(self) -> None:
        """Abandon the current graph."""
        self._graph = None
        self._state = GraphState.ACTIVE
        self._delete_persisted()

    # ── Persistence ────────────────────────────────────────────────────

    def _persist(self) -> None:
        if self._graph is None:
            return
        try:
            self._persist_dir.mkdir(parents=True, exist_ok=True)
            path = self._persist_dir / f"{self.session_id}.json"
            data = {
                "session_id": self.session_id,
                "state": self._state.value,
                "graph": self._graph.to_dict(),
            }
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as exc:
            logger.warning("Failed to persist TaskGraph: %s", exc)

    def _delete_persisted(self) -> None:
        try:
            path = self._persist_dir / f"{self.session_id}.json"
            if path.exists():
                path.unlink()
        except Exception as exc:
            logger.warning("Failed to delete persisted TaskGraph: %s", exc)

    def load_persisted(self) -> bool:
        """Attempt to load a persisted graph for this session. Returns True if found."""
        path = self._persist_dir / f"{self.session_id}.json"
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text())
            self._graph = TaskGraph.from_dict(data["graph"])
            self._state = GraphState(data.get("state", "active"))
            return True
        except Exception as exc:
            logger.warning("Failed to load persisted TaskGraph: %s", exc)
            return False

    # ── Internal ───────────────────────────────────────────────────────

    def _require_graph(self) -> None:
        if self._graph is None:
            raise ValueError("No active task graph. Use task_plan to create one.")

    def _check_graph_completion(self) -> None:
        """Check if all nodes are done; update graph state accordingly."""
        if self._graph is None:
            return
        all_done = all(n.is_done for n in self._graph.nodes)
        if not all_done:
            return
        has_failure = any(
            n.status == TaskStatus.FAILED for n in self._graph.nodes
        )
        self._state = GraphState.FAILED if has_failure else GraphState.COMPLETED

    @staticmethod
    def _generate_summary(result: str) -> str:
        """Generate a compact summary of a node result.

        Short results are used verbatim. Medium results get truncated.
        Long results get head + tail extraction.
        """
        if not result:
            return "(empty result)"
        length = len(result)
        if length <= _SHORT_THRESHOLD:
            return result
        if length <= _MEDIUM_THRESHOLD:
            return result[:400] + f"\n... ({length} chars total, truncated)"
        # Long result: head + tail
        head = result[:300]
        tail = result[-200:]
        return (
            f"{head}\n"
            f"... ({length} chars total) ...\n"
            f"{tail}"
        )
