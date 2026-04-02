"""
KnowledgeGraph component — Memory layer visualization.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class NodeState(Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    EXPANDED = "expanded"
    COLLAPSED = "collapsed"


@dataclass
class KnowledgeNode:
    """A single node in the knowledge graph."""

    id: str
    label: str
    node_type: str  # "concept", "fact", "skill", "memory"
    state: NodeState = NodeState.ACTIVE
    confidence: float = 1.0  # 0-1
    children: list["KnowledgeNode"] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    expanded: bool = False

    def to_lines(self, indent: int = 0, show_children: bool = True) -> list[str]:
        """Render node as indented text lines."""
        prefix = "  " * indent
        bullet = "o" if self.state == NodeState.ACTIVE else "o"

        if self.node_type == "root":
            line = f"[bold cyan]{self.label}[/bold cyan]"
        elif self.node_type == "category":
            marker = "[-]" if self.expanded else "[+]"
            line = f"{prefix}{marker} [yellow]{self.label}[/yellow]"
        else:
            conf = self._confidence_label()
            conf_color = (
                "green"
                if self.confidence > 0.8
                else "yellow"
                if self.confidence > 0.5
                else "red"
            )
            line = f"{prefix}{bullet} [dim]{self.label}[/dim]"
            if self.confidence != 1.0:
                line += f"  [{conf_color}]{conf}[/{conf_color}]"

        lines = [line]

        if self.expanded and show_children and self.children:
            for child in self.children:
                lines.extend(child.to_lines(indent + 1, show_children=True))

        return lines

    def _confidence_label(self) -> str:
        if self.confidence >= 0.9:
            return "high"
        if self.confidence >= 0.6:
            return "med"
        return "low"


class KnowledgeGraph(Widget):
    """
    Visualizes the Memory layer as an interactive knowledge graph.

    Shows:
    - Semantic memory entries
    - Procedural memory (skills)
    - Episodic memory summary

    Layout:
    ┌─ KNOWLEDGE GRAPH ────────────────────────────────────────────┐
    │        o Project                                             │
    │        │                                                    │
    │    o Framework: FastAPI                                    │
    │    o Language: Python 3.12                                 │
    │    o Key Concepts                                          │
    │        [-] Middleware Pattern                             │
    │        [+] Memory Types (4)                               │
    │        [+] DAG Task Engine                                │
    │                                                               │
    │  [Expand All]  [Collapse All]  [Search]                   │
    └─────────────────────────────────────────────────────────────┘
    """

    root_nodes: reactive[list[KnowledgeNode]] = reactive([], layout=True)

    class NodeClicked(Message, bubble=True):
        """User clicked a node."""

        def __init__(self, node: KnowledgeNode) -> None:
            super().__init__()
            self.node = node

    def compose(self) -> ComposeResult:
        yield Static("", id="kg-content")

    def set_nodes(self, nodes: list[KnowledgeNode]) -> None:
        """Set the knowledge graph nodes."""
        self.root_nodes = nodes
        self._render()

    def load_from_session(
        self, semantic_count: int, procedural_count: int, episodic_count: int
    ) -> None:
        """Load a default session summary into the graph."""
        root = KnowledgeNode(id="root", label="Session", node_type="root")

        project = KnowledgeNode(id="project", label="Project", node_type="root")
        framework = KnowledgeNode(
            id="framework",
            label="Framework: FastAPI",
            node_type="concept",
            confidence=0.95,
        )
        language = KnowledgeNode(
            id="language",
            label="Language: Python 3.12",
            node_type="concept",
            confidence=0.9,
        )

        concepts = KnowledgeNode(
            id="concepts", label="Key Concepts", node_type="category", expanded=False
        )
        concepts.children = [
            KnowledgeNode(
                id="middleware",
                label="Middleware Pattern",
                node_type="concept",
                confidence=0.85,
            ),
            KnowledgeNode(
                id="memory-types",
                label="Memory Types (4)",
                node_type="concept",
                confidence=0.9,
                expanded=False,
            ),
            KnowledgeNode(
                id="dag", label="DAG Task Engine", node_type="concept", confidence=0.75
            ),
        ]

        project.children = [framework, language, concepts]

        memory = KnowledgeNode(id="memory", label="Memory", node_type="root")
        memory.children = [
            KnowledgeNode(
                id="semantic",
                label=f"Semantic: {semantic_count} entries",
                node_type="memory",
                confidence=0.95,
            ),
            KnowledgeNode(
                id="procedural",
                label=f"Procedural: {procedural_count} skills",
                node_type="skill",
                confidence=0.9,
            ),
            KnowledgeNode(
                id="episodic",
                label=f"Episodic: {episodic_count} entries",
                node_type="memory",
                confidence=0.8,
            ),
        ]

        self.root_nodes = [root, project, memory]
        self._render()

    def toggle_node(self, node_id: str) -> None:
        """Toggle expanded/collapsed state of a node."""

        def find_and_toggle(nodes: list[KnowledgeNode]) -> bool:
            for node in nodes:
                if node.id == node_id:
                    node.expanded = not node.expanded
                    return True
                if node.children and find_and_toggle(node.children):
                    return True
            return False

        find_and_toggle(self.root_nodes)
        self._render()

    def expand_all(self) -> None:
        """Expand all collapsible nodes."""

        def expand(nodes: list[KnowledgeNode]) -> None:
            for node in nodes:
                if node.children:
                    node.expanded = True
                    expand(node.children)

        expand(self.root_nodes)
        self._render()

    def collapse_all(self) -> None:
        """Collapse all expandable nodes."""

        def collapse(nodes: list[KnowledgeNode]) -> None:
            for node in nodes:
                if node.children and node.node_type == "category":
                    node.expanded = False
                collapse(node.children)

        collapse(self.root_nodes)
        self._render()

    def _render(self) -> None:
        content = self.query_one("#kg-content", Static)
        if not self.root_nodes:
            content.update("[dim](no knowledge graph)[/dim]")
            return

        lines = ["[bold]KNOWLEDGE GRAPH[/bold]", ""]

        for node in self.root_nodes:
            lines.extend(node.to_lines(indent=0))

        lines.append("")
        lines.append(
            "[bold cyan]expand[/bold cyan]  "
            "[bold cyan]collapse[/bold cyan]  "
            "[bold cyan]search[/bold cyan]"
        )

        content.update("\n".join(lines))
