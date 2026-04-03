"""
ObservabilityPanel component — bottom dock with tool summary + budget.

Shows after TurnDone when tools were used:
    ┌─ tools ──────────────────────────────────────────────────┐
    │  Read (12ms ok)  |  glob (8ms ok)  |  Bash (234ms ok)  │
    └─────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


@dataclass
class ToolSummary:
    """Summarized tool result for observability panel."""

    name: str
    duration_ms: float
    success: bool


class ObservabilityPanel(Widget):
    """
    Bottom dock panel showing tool call summary.

    Appears after TurnDone when tool_count > 0.
    Shows tool name + duration on one line.
    """

    visible: reactive[bool] = reactive(False)
    tools: reactive[list[ToolSummary]] = reactive([])

    def compose(self) -> ComposeResult:
        yield Static("", id="obs-content")

    def show_tools(self, tools: list[ToolSummary]) -> None:
        """Show tool summary panel."""
        self.tools = tools
        self.visible = True
        self._render()

    def hide(self) -> None:
        """Hide the panel."""
        self.visible = False
        self.tools = []
        self._render()

    def watch_visible(self, visible: bool) -> None:
        self.set_class(visible, "visible")

    def _render(self) -> None:
        """Render the observability panel."""
        content = self.query_one("#obs-content", Static)
        if not self.visible or not self.tools:
            content.update("")
            return

        tool_parts = []
        for tool in self.tools:
            icon = "ok" if tool.success else "!!"
            color = "green" if tool.success else "red"
            tool_parts.append(
                f"[dim]{tool.name}[/dim] "
                f"[{color}]{tool.duration_ms:.0f}ms {icon}[/{color}]"
            )

        line = "  |  ".join(tool_parts)
        content.update(
            f"[dim]┌─ tools ──────────────────────────────────────────────"
            f"─────────────┐\n"
            f"│  {line}  │\n"
            f"└────────────────────────────────────────────────────"
            f"───────────────────┘[/{dim}]"
        )
