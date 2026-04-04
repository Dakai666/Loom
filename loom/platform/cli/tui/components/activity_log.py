"""
ActivityLog component — live tool-call timeline for the current session.

Replaces the hollow KnowledgeGraph with actual, useful data: every tool
call this session, in chronological order, with duration and error details.

Visual example:
  7 calls  ·  2.3s total
  ──────────────────────────────
  ✓ read_file      middleware.py  45ms
  ✓ list_dir       loom/core/     12ms
  ✗ run_bash       pytest        234ms
    └ Exit 1: 3 tests failed
  ✓ run_bash       pytest -x     1.2s
  ⟳ read_file      tasks/...  running
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from rich.markup import escape as markup_escape
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static


@dataclass
class ActivityEntry:
    """A single completed (or running) tool call."""

    name: str
    args_preview: str        # first meaningful arg, truncated
    success: bool
    duration_ms: float       # 0 if still running
    running: bool = False
    error_snippet: str = ""  # first line of error, if any
    expanded: bool = False   # whether error details are shown
    timestamp: datetime.datetime = field(default_factory=datetime.datetime.now)


class ActivityLog(Widget):
    """
    Scrollable list of tool calls for the current session.

    Click on a failed entry to expand/collapse error details.
    """

    DEFAULT_CSS = """
    ActivityLog {
        overflow-y: auto;
        height: 1fr;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._entries: list[ActivityEntry] = []
        self._total_ms: float = 0.0

    def compose(self) -> ComposeResult:
        yield Static("", id="activity-content")

    def on_mount(self) -> None:
        self._update_display()

    def append_entry(self, entry: ActivityEntry) -> None:
        """Add a completed tool call entry."""
        self._entries.append(entry)
        if not entry.running:
            self._total_ms += entry.duration_ms
        self._update_display()

    def update_running(self, call_id_hint: str, name: str, args_preview: str) -> None:
        """Add a 'currently running' placeholder (replaced by append_entry on complete)."""
        # Remove any existing running entry for same tool
        self._entries = [e for e in self._entries if not e.running]
        self._entries.append(ActivityEntry(
            name=name,
            args_preview=args_preview,
            success=True,
            duration_ms=0,
            running=True,
        ))
        self._update_display()

    def complete_running(self, name: str, success: bool, duration_ms: float, error_snippet: str = "") -> None:
        """Replace the running placeholder with the completed result."""
        for e in reversed(self._entries):
            if e.running and e.name == name:
                e.running = False
                e.success = success
                e.duration_ms = duration_ms
                e.error_snippet = error_snippet
                e.expanded = not success  # auto-expand failures
                self._total_ms += duration_ms
                break
        self._update_display()

    def clear(self) -> None:
        self._entries = []
        self._total_ms = 0.0
        self._update_display()

    def on_click(self, event) -> None:
        """Toggle expand/collapse on failed entries."""
        # Determine which entry was clicked based on row position.
        # Each entry occupies 1 row (2 if expanded with error).
        # We use the click's y offset relative to the widget.
        try:
            y = event.y
        except AttributeError:
            return

        row = 0
        # Skip header (2 rows) + separator (1 row) = 3 rows
        header_rows = 3 if self._entries else 1
        row_offset = y - header_rows
        if row_offset < 0:
            return

        current_row = 0
        for entry in self._entries:
            if current_row == row_offset:
                if entry.error_snippet or not entry.success:
                    entry.expanded = not entry.expanded
                    self._update_display()
                return
            current_row += 1
            if entry.expanded and entry.error_snippet:
                # The expanded error line occupies one extra row
                if current_row == row_offset:
                    entry.expanded = False
                    self._update_display()
                    return
                current_row += 1

    def _update_display(self) -> None:
        from textual.css.query import NoMatches

        try:
            content = self.query_one("#activity-content", Static)
        except NoMatches:
            return

        if not self._entries:
            content.update(
                "[dim]No tool calls yet.[/dim]\n\n"
                "[dim]Tool activity appears here\nas the agent works.[/dim]"
            )
            return

        lines: list[str] = []

        # Header summary
        count = len(self._entries)
        total_s = self._total_ms / 1000.0
        if total_s >= 1.0:
            total_str = f"{total_s:.1f}s"
        else:
            total_str = f"{self._total_ms:.0f}ms"
        lines.append(
            f"[dim]{count} call{'s' if count != 1 else ''}  ·  {total_str} total[/dim]"
        )
        lines.append("[dim]" + "─" * 32 + "[/dim]")

        for entry in self._entries:
            name_safe = markup_escape(entry.name)
            args_safe = markup_escape(entry.args_preview[:22]) if entry.args_preview else ""
            # Pad name to fixed width for alignment
            name_col = f"{name_safe:<14}"

            if entry.running:
                lines.append(
                    f"[#c8a464]⟳[/#c8a464] [dim]{name_col}[/dim] "
                    f"[dim]{args_safe}[/dim]  [dim]running[/dim]"
                )
            elif entry.success:
                dur = self._fmt_dur(entry.duration_ms)
                lines.append(
                    f"[#7a9e78]✓[/#7a9e78] [dim]{name_col}[/dim] "
                    f"[dim]{args_safe}[/dim]  [#7a9e78]{dur}[/#7a9e78]"
                )
            else:
                dur = self._fmt_dur(entry.duration_ms)
                expand_icon = "▼" if entry.expanded else "▶"
                lines.append(
                    f"[#b87060]✗[/#b87060] [dim]{name_col}[/dim] "
                    f"[dim]{args_safe}[/dim]  [#b87060]{dur}[/#b87060] "
                    f"[dim]{expand_icon}[/dim]"
                )
                if entry.expanded and entry.error_snippet:
                    err_safe = markup_escape(entry.error_snippet[:50])
                    lines.append(f"  [dim]└ {err_safe}[/dim]")

        content.update("\n".join(lines))

    def _fmt_dur(self, ms: float) -> str:
        if ms >= 1000:
            return f"{ms / 1000:.1f}s"
        return f"{ms:.0f}ms"
