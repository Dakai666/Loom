"""
StatusBar component — bottom bar with session stats.

Design:
  [GUARDED]  ctx 45% [▓▓▓▓▓░░░░░]  1.2k in / 340 out  2.3s  3 tools
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class StatusBar(Widget):
    """
    Bottom status bar showing context budget and token usage.

    Color-coded context bar: green < 60%, yellow 60-85%, red > 85%.
    Shows trust mode, token counts, elapsed time, tool count.
    """

    context_fraction: reactive[float] = reactive(0.0)
    input_tokens: reactive[int] = reactive(0)
    output_tokens: reactive[int] = reactive(0)
    elapsed_ms: reactive[float] = reactive(0.0)
    tool_count: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        yield Static("", id="status-content")

    def on_mount(self) -> None:
        self._update_display()

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
        self._update_display()

    def watch_context_fraction(self, fraction: float) -> None:
        self._update_display()

    def _update_display(self) -> None:
        from textual.css.query import NoMatches

        try:
            content = self.query_one("#status-content", Static)
        except NoMatches:
            return

        pct = self.context_fraction * 100
        ctx_color = "green" if pct < 60 else "yellow" if pct < 85 else "red"

        bar_len = 10
        filled = int(bar_len * self.context_fraction)
        bar = "▓" * filled + "░" * (bar_len - filled)

        parts: list[str] = []

        # Context bar
        parts.append(
            f"ctx [{ctx_color}]{bar}[/{ctx_color}] "
            f"[{ctx_color}]{pct:.0f}%[/{ctx_color}]"
        )

        # Tokens — only show after first turn
        if self.input_tokens > 0:
            def _fmt(n: int) -> str:
                return f"{n/1000:.1f}k" if n >= 1000 else str(n)

            parts.append(
                f"[dim]{_fmt(self.input_tokens)} in"
                f" / {_fmt(self.output_tokens)} out[/dim]"
            )

        # Elapsed time
        if self.elapsed_ms > 0:
            parts.append(f"[dim]{self.elapsed_ms / 1000:.1f}s[/dim]")

        # Tool count
        if self.tool_count > 0:
            label = "tool" if self.tool_count == 1 else "tools"
            parts.append(f"[dim]{self.tool_count} {label}[/dim]")

        content.update("  [dim]|[/dim]  ".join(parts))
