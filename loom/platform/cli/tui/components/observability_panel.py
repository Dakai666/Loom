"""
ObservabilityPanel component — compact tool summary after each turn.

Shows after TurnDone when tools were used:
  ✓ read_file 12ms  ✓ list_dir 8ms  ✗ run_bash 234ms
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
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
    Bottom dock panel showing tool call summary after each turn.

    Appears (display: block) after TurnDone when tool_count > 0.
    Single-line compact format — no box drawing.
    """

    visible: reactive[bool] = reactive(False)
    tools: reactive[list[ToolSummary]] = reactive([])

    def compose(self) -> ComposeResult:
        yield Static("", id="obs-content")

    def show_tools(self, tools: list[ToolSummary]) -> None:
        """Show tool summary panel."""
        self.tools = tools
        self.visible = True
        self._update_display()

    def hide(self) -> None:
        """Hide the panel."""
        self.visible = False
        self.tools = []
        self._update_display()

    def watch_visible(self, visible: bool) -> None:
        self.set_class(visible, "visible")

    def _update_display(self) -> None:
        from textual.css.query import NoMatches

        try:
            content = self.query_one("#obs-content", Static)
        except NoMatches:
            return

        if not self.visible or not self.tools:
            content.update("")
            return

        parts: list[str] = []
        for tool in self.tools:
            if tool.success:
                parts.append(
                    f"[green]✓[/green] [dim]{tool.name}[/dim] "
                    f"[green]{tool.duration_ms:.0f}ms[/green]"
                )
            else:
                parts.append(
                    f"[red]✗[/red] [dim]{tool.name}[/dim] "
                    f"[red]{tool.duration_ms:.0f}ms[/red]"
                )

        summary = "  [dim]|[/dim]  ".join(parts)
        content.update(f"[dim]tools:[/dim]  {summary}")
