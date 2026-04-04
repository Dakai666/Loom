"""
WorkspacePanel — three-tab sidebar (Artifacts / Activity / Budget).

Tabs are switched by clicking the tab header or pressing F2 (cycles).
Width is 25% of the screen (set in App.CSS).
"""

from __future__ import annotations

from enum import Enum

from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from .artifact_card import ArtifactState
from .artifacts_panel import ArtifactsPanel
from .activity_log import ActivityLog, ActivityEntry
from .budget_panel import BudgetPanel


class WorkspaceTab(Enum):
    ARTIFACTS = "artifacts"
    ACTIVITY = "activity"
    BUDGET = "budget"


class WorkspacePanel(Widget):
    """
    Container panel with three tabs: Artifacts, Activity, Budget.

    Tab header is clickable — click the label text to switch tabs.
    F2 cycles through tabs.
    """

    DEFAULT_CSS = """
    WorkspacePanel {
        layout: vertical;
        overflow: hidden hidden;
        padding: 0 1;
    }
    #workspace-header {
        height: 1;
        /* No padding or border — height 1 = exactly one text row, always visible */
    }
    #workspace-divider {
        height: 1;
        border-bottom: solid #4a4038;
    }
    #artifacts-panel {
        height: 1fr;
        overflow-y: auto;
        scrollbar-color: #4a4038;
        scrollbar-color-hover: #c8a464;
        scrollbar-background: #1c1814;
    }
    #activity-panel {
        height: 1fr;
        overflow-y: auto;
        scrollbar-color: #4a4038;
        scrollbar-color-hover: #c8a464;
        scrollbar-background: #1c1814;
    }
    #budget-panel {
        height: 1fr;
        overflow-y: auto;
        scrollbar-color: #4a4038;
        scrollbar-color-hover: #c8a464;
        scrollbar-background: #1c1814;
    }
    """

    active_tab: reactive[WorkspaceTab] = reactive(WorkspaceTab.ARTIFACTS)

    class TabChanged(Message, bubble=True):
        def __init__(self, tab: WorkspaceTab) -> None:
            super().__init__()
            self.tab = tab

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._artifacts_panel: ArtifactsPanel | None = None
        self._activity_panel: ActivityLog | None = None
        self._budget_panel: BudgetPanel | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="workspace-header")
        yield Static("", id="workspace-divider")
        yield ArtifactsPanel(id="artifacts-panel")
        yield ActivityLog(id="activity-panel")
        yield BudgetPanel(id="budget-panel")

    def on_mount(self) -> None:
        self._artifacts_panel = self.query_one("#artifacts-panel", ArtifactsPanel)
        self._activity_panel = self.query_one("#activity-panel", ActivityLog)
        self._budget_panel = self.query_one("#budget-panel", BudgetPanel)
        self._update_visibility()
        self._render_header()

    def watch_active_tab(self, _: WorkspaceTab) -> None:
        self._update_visibility()
        self._render_header()

    # ── Tab switching ─────────────────────────────────────────────────────────

    def switch_tab(self, tab: WorkspaceTab) -> None:
        self.active_tab = tab

    def toggle_tab(self) -> None:
        """Cycle Artifacts → Activity → Budget → Artifacts."""
        order = [WorkspaceTab.ARTIFACTS, WorkspaceTab.ACTIVITY, WorkspaceTab.BUDGET]
        idx = order.index(self.active_tab)
        self.active_tab = order[(idx + 1) % len(order)]

    def on_click(self, event) -> None:
        """Handle clicks on tab header labels to switch tabs."""
        # We detect clicks only in the header area (first 2 rows).
        try:
            if event.y >= 2:
                return
        except AttributeError:
            return
        # Use x position to guess which tab label was clicked.
        # Layout: "WORKSPACE  [Art] [Act] [Bud]"
        # Rough column ranges (at 25% width ≈ 25 chars):
        x = event.x
        if x < 8:
            return  # "WORKSPACE" label area
        if x < 14:
            self.active_tab = WorkspaceTab.ARTIFACTS
        elif x < 20:
            self.active_tab = WorkspaceTab.ACTIVITY
        else:
            self.active_tab = WorkspaceTab.BUDGET

    # ── Data feeds ────────────────────────────────────────────────────────────

    def add_artifact(
        self,
        path: str,
        state: ArtifactState,
        diff_lines: list[str] | None = None,
        preview: str = "",
    ) -> None:
        if self._artifacts_panel:
            self._artifacts_panel.add_artifact(path, state, diff_lines, preview)

    def append_activity(self, entry: ActivityEntry) -> None:
        """Add a completed tool call to the Activity log."""
        if self._activity_panel:
            self._activity_panel.append_entry(entry)

    def update_budget(
        self,
        fraction: float,
        used_tokens: int,
        max_tokens: int,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        if self._budget_panel:
            self._budget_panel.update_budget(
                fraction, used_tokens, max_tokens, input_tokens, output_tokens
            )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _update_visibility(self) -> None:
        if self._artifacts_panel:
            self._artifacts_panel.display = self.active_tab == WorkspaceTab.ARTIFACTS
        if self._activity_panel:
            self._activity_panel.display = self.active_tab == WorkspaceTab.ACTIVITY
        if self._budget_panel:
            self._budget_panel.display = self.active_tab == WorkspaceTab.BUDGET

    def _render_header(self) -> None:
        from textual.css.query import NoMatches
        try:
            header = self.query_one("#workspace-header", Static)
        except NoMatches:
            return

        def _tab(label: str, tab: WorkspaceTab) -> str:
            if self.active_tab == tab:
                return f"[reverse #c8a464] {label} [/reverse #c8a464]"
            return f"[dim] {label} [/dim]"

        header.update(
            f"{_tab('Art', WorkspaceTab.ARTIFACTS)} "
            f"{_tab('Act', WorkspaceTab.ACTIVITY)} "
            f"{_tab('Bgt', WorkspaceTab.BUDGET)} "
            f" [dim]F2[/dim]"
        )
