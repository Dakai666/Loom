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

Phase 2 additions:
- Result overflow: large results spill to artifact files, task_read does lazy read.
- Breakpoint resume: persisted graphs detected on session start, agent prompted.
- Lifecycle: active → suspended (session end) → resumed or abandoned (TTL 24h).
"""

from __future__ import annotations

import json
import logging
import time
from enum import Enum
from pathlib import Path
from typing import Any

from .graph import TaskGraph, TaskNode, TaskStatus

logger = logging.getLogger(__name__)

# Result size thresholds for summary generation
_SHORT_THRESHOLD = 500     # chars (rough proxy for tokens)
_MEDIUM_THRESHOLD = 5000

# Result overflow threshold — results larger than this spill to artifact files
_OVERFLOW_THRESHOLD = 5000

# Persisted graph TTL — graphs older than this are considered abandoned
_GRAPH_TTL_SECONDS = 24 * 60 * 60  # 24 hours


class GraphState(Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    SUSPENDED = "suspended"
    ABANDONED = "abandoned"


class TaskGraphManager:
    """Session-scoped manager for a single active TaskGraph."""

    def __init__(self, session_id: str, persist_dir: Path | None = None,
                 artifact_dir: Path | None = None) -> None:
        self.session_id = session_id
        self._graph: TaskGraph | None = None
        self._state: GraphState = GraphState.ACTIVE
        self._persist_dir = persist_dir or (Path.home() / ".loom" / "task_graphs")
        self._artifact_dir = artifact_dir or (Path.home() / ".loom" / "artifacts")

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
            if self._state in (GraphState.COMPLETED, GraphState.FAILED):
                # Exhausted graph — auto-reset so a new graph can start
                self._cleanup_artifacts()
                self._graph = None
                self._delete_persisted()
            else:
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

    def mark_in_progress(self, node_id: str) -> TaskNode:
        """Mark a node as in-progress (agent is working on it)."""
        self._require_graph()
        node = self._graph.get(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        if node.status != TaskStatus.PENDING:
            raise ValueError(
                f"Cannot start node '{node_id}' with status {node.status.value} "
                f"— only PENDING nodes can be started"
            )
        node.status = TaskStatus.IN_PROGRESS
        self._persist()
        return node

    def mark_completed(self, node_id: str, result: str) -> TaskNode:
        """Mark a node as completed, store result and generate summary.

        Phase 2: results exceeding _OVERFLOW_THRESHOLD are spilled to an
        artifact file; only the file path is stored on the node. task_read
        transparently loads from the artifact file.
        """
        self._require_graph()
        node = self._graph.get(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")

        # Phase 2: overflow large results to artifact file
        if len(result) > _OVERFLOW_THRESHOLD:
            artifact_path = self._write_artifact(node_id, result)
            node.complete(result)
            node.metadata["_artifact_path"] = str(artifact_path)
        else:
            node.complete(result)

        node.result_summary = self._generate_summary(result, node_id=node_id)
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

    def get_node_result(self, node_id: str, section: str | None = None) -> str | None:
        """Return full result of a completed node (pull model).

        Phase 2: if the result was overflowed to an artifact file, lazily
        reads from disk. Optional ``section`` parameter filters output:
        - "head": first 200 lines
        - "tail": last 200 lines
        - "N-M": line range (e.g. "10-50")
        - keyword string: grep-like filter returning matching lines
        """
        self._require_graph()
        node = self._graph.get(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        if node.status != TaskStatus.COMPLETED:
            return None

        # Phase 2: lazy read from artifact file if overflowed
        artifact_path = node.metadata.get("_artifact_path")
        if artifact_path:
            result = self._read_artifact(artifact_path)
        else:
            result = node.result

        if result is None:
            return None

        if section is None:
            return result

        return self._apply_section_filter(result, section)

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
            is_overflow = "_artifact_path" in dep.metadata
            summary = dep.result_summary or "(no summary)"
            location = "artifact file" if is_overflow else "memory"
            parts.append(
                f"\nPrior task [{dep.id}] ({dep.content[:60]}) completed.\n"
                f"Result summary: {summary}\n"
                f"(full result: ~{result_len} chars in {location}"
                f" — use task_read(node_id='{dep.id}') to inspect,"
                f" supports section='head'/'tail'/'10-50'/keyword)"
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
        """Abandon the current graph and clean up artifacts."""
        if self._graph is not None:
            self._cleanup_artifacts()
        self._graph = None
        self._state = GraphState.ACTIVE
        self._delete_persisted()

    # ── Lifecycle (Phase 2) ────────────────────────────────────────────

    def suspend(self) -> None:
        """Suspend the active graph (called on session stop).

        Only active graphs are suspended. Completed/failed graphs are
        left as-is for archival.
        """
        if self._graph is None:
            return
        if self._state == GraphState.ACTIVE:
            self._state = GraphState.SUSPENDED
            self._persist()

    def resume(self) -> bool:
        """Resume a suspended graph. Returns True if resumed."""
        if self._graph is None or self._state != GraphState.SUSPENDED:
            return False
        self._state = GraphState.ACTIVE
        self._persist()
        return True

    def build_resume_context(self) -> str | None:
        """Build a context message for the agent about a persisted graph.

        Returns None if there's no graph to resume. The caller injects
        this into the system prompt or as a system message so the agent
        is aware of the interrupted task graph.
        """
        if self._graph is None:
            return None
        # Only prompt for suspended or active (in-progress) graphs
        if self._state not in (GraphState.SUSPENDED, GraphState.ACTIVE):
            return None

        nodes = self._graph.nodes
        completed = [n for n in nodes if n.status == TaskStatus.COMPLETED]
        failed = [n for n in nodes if n.status == TaskStatus.FAILED]
        pending = [n for n in nodes if n.status == TaskStatus.PENDING]
        in_progress = [n for n in nodes if n.status == TaskStatus.IN_PROGRESS]

        parts = [
            "## Interrupted Task Graph",
            f"A task graph from a prior session was found (state: {self._state.value}).",
            "",
        ]
        if completed:
            parts.append(f"**Completed** ({len(completed)}):")
            for n in completed:
                summary = n.result_summary or "(no summary)"
                parts.append(f"  - [{n.id}] {n.content[:60]} — {summary[:80]}")
        if failed:
            parts.append(f"**Failed** ({len(failed)}):")
            for n in failed:
                parts.append(f"  - [{n.id}] {n.content[:60]} — error: {n.error[:80] if n.error else '?'}")
        if in_progress:
            parts.append(f"**Was in-progress** ({len(in_progress)}):")
            for n in in_progress:
                parts.append(f"  - [{n.id}] {n.content[:60]}")
        if pending:
            parts.append(f"**Pending** ({len(pending)}):")
            for n in pending:
                deps = f" (depends on: {', '.join(n.depends_on)})" if n.depends_on else ""
                parts.append(f"  - [{n.id}] {n.content[:60]}{deps}")

        parts.append("")
        parts.append(
            "Use `task_status` to inspect details, `task_modify` to adjust the plan, "
            "or `task_done` to continue execution. To discard, inform the user."
        )
        text = "\n".join(parts)
        # Cap length to avoid bloating the system prompt
        if len(text) > 2000:
            text = text[:1950] + "\n... (truncated — use task_status for full details)"
        return text

    @staticmethod
    def cleanup_stale_graphs(persist_dir: Path | None = None,
                             ttl_seconds: int = _GRAPH_TTL_SECONDS) -> int:
        """Remove persisted graphs older than TTL. Returns count removed.

        Called during housekeeping (e.g. session start or a scheduled task).
        Only removes graphs in SUSPENDED state past their TTL.
        """
        d = persist_dir or (Path.home() / ".loom" / "task_graphs")
        if not d.exists():
            return 0
        now = time.time()
        removed = 0
        for path in d.glob("*.json"):
            try:
                mtime = path.stat().st_mtime
                if (now - mtime) < ttl_seconds:
                    continue
                data = json.loads(path.read_text())
                state = data.get("state", "active")
                if state == GraphState.SUSPENDED.value:
                    path.unlink()
                    # Also clean up artifacts for this session
                    sid = data.get("session_id")
                    if sid:
                        artifact_dir = d.parent / "artifacts" / sid
                        if artifact_dir.exists():
                            import shutil
                            shutil.rmtree(artifact_dir, ignore_errors=True)
                    removed += 1
            except Exception as exc:
                logger.debug("cleanup_stale_graphs: skip %s: %s", path.name, exc)
        return removed

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
                "updated_at": time.time(),
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

    # ── Artifact overflow (Phase 2) ────────────────────────────────────

    def _artifact_session_dir(self) -> Path:
        return self._artifact_dir / self.session_id

    def _write_artifact(self, node_id: str, content: str) -> Path:
        """Write a large result to an artifact file. Returns the file path."""
        d = self._artifact_session_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{node_id}.txt"
        path.write_text(content, encoding="utf-8")
        return path

    def _read_artifact(self, artifact_path: str) -> str | None:
        """Read content from an artifact file. Returns None if missing."""
        p = Path(artifact_path)
        if not p.exists():
            logger.warning("Artifact file missing: %s", artifact_path)
            return None
        return p.read_text(encoding="utf-8")

    def _cleanup_artifacts(self) -> None:
        """Remove all artifact files for this session."""
        d = self._artifact_session_dir()
        if d.exists():
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    @staticmethod
    def _apply_section_filter(text: str, section: str) -> str:
        """Apply a section filter to result text.

        Supported filters:
        - "head": first 200 lines
        - "tail": last 200 lines
        - "N-M": line range (1-indexed, e.g. "10-50")
        - anything else: grep-like keyword filter
        """
        try:
            lines = text.splitlines(keepends=True)
            if section == "head":
                return "".join(lines[:200])
            if section == "tail":
                return "".join(lines[-200:])
            # Try line range "N-M"
            if "-" in section:
                parts = section.split("-", 1)
                try:
                    start = max(1, int(parts[0])) - 1  # 1-indexed to 0-indexed
                    end = int(parts[1])
                    return "".join(lines[start:end])
                except (ValueError, IndexError):
                    pass  # fall through to keyword filter
            # Keyword filter
            matched = [ln for ln in lines if section.lower() in ln.lower()]
            if matched:
                return "".join(matched)
            return f"(no lines matching '{section}')"
        except Exception as exc:
            return f"(section filter error: {exc})"

    # ── Internal ───────────────────────────────────────────────────────

    def _require_graph(self) -> None:
        if self._graph is None:
            raise ValueError("No active task graph. Use task_plan to create one.")

    def _check_graph_completion(self) -> None:
        """Check if all nodes are done; update graph state accordingly.

        Note: artifacts are NOT cleaned up on completion/failure because
        the agent may still call task_read after the graph finishes.
        Artifacts are cleaned on abandon() and TTL cleanup only.
        """
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
    def _generate_summary(result: str, node_id: str = "") -> str:
        """Generate a compact summary of a node result.

        Short results are used verbatim. Medium/long results get truncated
        with a task_read hint so the agent knows full data is available.
        """
        if not result:
            return "(empty result)"
        length = len(result)
        if length <= _SHORT_THRESHOLD:
            return result
        read_hint = (
            f"\n⚠ Full result ({length} chars) available — "
            f"use task_read(node_id='{node_id}') to retrieve, "
            f"supports section='head'/'tail'/'10-50'/keyword"
        ) if node_id else f"\n... ({length} chars total, use task_read for full result)"
        if length <= _MEDIUM_THRESHOLD:
            return result[:400] + read_hint
        # Long result: head + tail
        head = result[:300]
        tail = result[-200:]
        return (
            f"{head}\n"
            f"... ({length} chars total) ...\n"
            f"{tail}"
            f"{read_hint}"
        )
