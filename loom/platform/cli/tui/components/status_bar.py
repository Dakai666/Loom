"""
StatusBar component — color-coded budget visualization.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class StatusBar(Widget):
    """
    Bottom status bar showing context budget and token usage.

    Visual: [#....#....] 45% | 1.2k in / 340 out | 2.3s | 3 tools
    Color-coded: green < 60%, yellow 60-85%, red > 85%
    """

    context_fraction: reactive[float] = reactive(0.0)
    input_tokens: reactive[int] = reactive(0)
    output_tokens: reactive[int] = reactive(0)
    elapsed_ms: reactive[float] = reactive(0.0)
    tool_count: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        yield Static("", id="status-content")

    def update(
        self,
        fraction: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        elapsed_ms: float = 0.0,
        tool_count: int = 0,
    ) -> None:
        """Update status bar values."""
        self.context_fraction = fraction
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.elapsed_ms = elapsed_ms
        self.tool_count = tool_count
        self._render()

    def watch_context_fraction(self, fraction: float) -> None:
        self._render()

    def _render(self) -> None:
        """Render the status bar."""
        from textual.css.query import NoMatches

        try:
            content = self.query_one("#status-content", Static)
        except NoMatches:
            return
        pct = self.context_fraction * 100
        ctx_color = "green" if pct < 60 else "yellow" if pct < 85 else "red"

        bar_len = 10
        filled = int(bar_len * self.context_fraction)
        bar = "#" * filled + "." * (bar_len - filled)

        content.update(
            f"[dim]-[/dim]"
            f"[{ctx_color}]{bar}[/{ctx_color}]"
            f"[dim] context {pct:.1f}%  |  "
            f"{self.input_tokens}in / {self.output_tokens}out  |  "
            f"{self.elapsed_ms / 1000:.1f}s  |  "
            f"{self.tool_count} tool{'s' if self.tool_count != 1 else ''}"
            f"[/dim]"
        )
