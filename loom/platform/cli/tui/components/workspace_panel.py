"""
WorkspacePanel component — container for Artifacts + Knowledge Graph.
"""

from __future__ import annotations

from enum import Enum

from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from .artifact_card import Artifact, ArtifactState
from .artifacts_panel import ArtifactsPanel
from .knowledge_graph import KnowledgeGraph, KnowledgeNode


class WorkspaceTab(Enum):
    ARTIFACTS = "artifacts"
    KNOWLEDGE = "knowledge"


class WorkspacePanel(Widget):
    """
    Container panel with two tabs: Artifacts and Knowledge Graph.

    Layout:
    ┌─ WORKSPACE ─── [Artifacts] [Knowledge] ────────────────────┐
    │                                                                  │
    │  (active tab content)                                          │
    │                                                                  │
    │  ArtifactsPanel or KnowledgeGraph                              │
    │                                                                  │
    └──────────────────────────────────────────────────────────────────┘
    """

    active_tab: reactive[WorkspaceTab] = reactive(WorkspaceTab.ARTIFACTS)

    class TabChanged(Message, bubble=True):
        def __init__(self, tab: WorkspaceTab) -> None:
            super().__init__()
            self.tab = tab

    def __init__(self) -> None:
        super().__init__()
        self._artifacts_panel: ArtifactsPanel | None = None
        self._kg_panel: KnowledgeGraph | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="workspace-header")
        yield ArtifactsPanel(id="artifacts-panel")
        yield KnowledgeGraph(id="knowledge-panel")

    def on_mount(self) -> None:
        self._artifacts_panel = self.query_one("#artifacts-panel", ArtifactsPanel)
        self._kg_panel = self.query_one("#knowledge-panel", KnowledgeGraph)
        self._update_visibility()
        self._render_header()

    def watch_active_tab(self, tab: WorkspaceTab) -> None:
        self._update_visibility()
        self._render_header()

    def switch_tab(self, tab: WorkspaceTab) -> None:
        """Switch to a different tab."""
        self.active_tab = tab

    def toggle_tab(self) -> None:
        """Toggle between Artifacts and Knowledge Graph."""
        self.active_tab = (
            WorkspaceTab.KNOWLEDGE
            if self.active_tab == WorkspaceTab.ARTIFACTS
            else WorkspaceTab.ARTIFACTS
        )

    def add_artifact(
        self,
        path: str,
        state: ArtifactState,
        diff_lines: list[str] | None = None,
        preview: str = "",
    ) -> None:
        """Add an artifact to the Artifacts panel."""
        if self._artifacts_panel:
            self._artifacts_panel.add_artifact(path, state, diff_lines, preview)

    def load_knowledge_graph(
        self,
        semantic_count: int = 0,
        procedural_count: int = 0,
        episodic_count: int = 0,
    ) -> None:
        """Load knowledge graph from session memory stats."""
        if self._kg_panel:
            self._kg_panel.load_from_session(
                semantic_count, procedural_count, episodic_count
            )

    def _update_visibility(self) -> None:
        """Show/hide panels based on active tab."""
        if self._artifacts_panel:
            self._artifacts_panel.display = self.active_tab == WorkspaceTab.ARTIFACTS
        if self._kg_panel:
            self._kg_panel.display = self.active_tab == WorkspaceTab.KNOWLEDGE

    def _render_header(self) -> None:
        from textual.css.query import NoMatches

        try:
            header = self.query_one("#workspace-header", Static)
        except NoMatches:
            return

        artifacts_active = self.active_tab == WorkspaceTab.ARTIFACTS
        knowledge_active = self.active_tab == WorkspaceTab.KNOWLEDGE

        artifacts_tag = (
            "[bold green]Artifacts[/bold green]"
            if artifacts_active
            else "[dim]Artifacts[/dim]"
        )
        knowledge_tag = (
            "[bold green]Knowledge[/bold green]"
            if knowledge_active
            else "[dim]Knowledge[/dim]"
        )

        header.update(f"[bold]WORKSPACE[/bold]    {artifacts_tag}  |  {knowledge_tag}")
