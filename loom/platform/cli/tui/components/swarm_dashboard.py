"""
SwarmDashboard component — replaces ActivityLog.
Displays a Grid-like UI for active background agents/tasks, 
and a historical timeline below.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from rich.markup import escape as markup_escape
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static
from textual.containers import VerticalScroll

@dataclass
class ActivityEntry:
    name: str
    args_preview: str
    success: bool
    duration_ms: float
    running: bool = False
    error_snippet: str = ""
    expanded: bool = False
    timestamp: datetime.datetime = field(default_factory=datetime.datetime.now)


class SwarmDashboard(VerticalScroll):
    """
    Shows currently active parallel tool/sub-agent contexts,
    followed by a historical timeline.
    """
    DEFAULT_CSS = """
    SwarmDashboard {
        overflow-y: auto;
        height: 1fr;
    }
    #swarm-active {
        height: auto;
        border-bottom: solid #4a4038;
        padding-bottom: 0;
        margin-bottom: 1;
        display: none;
    }
    #swarm-active.has_active {
        display: block;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._entries: list[ActivityEntry] = []
        self._total_ms: float = 0.0

    def compose(self) -> ComposeResult:
        yield Static(id="swarm-active")
        yield Static(id="swarm-history")

    def on_mount(self) -> None:
        self._update_display()

    def append_entry(self, entry: ActivityEntry) -> None:
        self._entries.append(entry)
        if not entry.running:
            self._total_ms += entry.duration_ms
        self._update_display()

    def update_running(self, call_id_hint: str, name: str, args_preview: str) -> None:
        # Replace existing running entries (for now) to mimic current harness behavior
        self._entries = [e for e in self._entries if not e.running]
        self._entries.append(ActivityEntry(
            name=name, args_preview=args_preview, success=True, duration_ms=0, running=True
        ))
        self._update_display()

    def complete_running(self, name: str, success: bool, duration_ms: float, error_snippet: str = "") -> None:
        for e in reversed(self._entries):
            if e.running and e.name == name:
                e.running = False
                e.success = success
                e.duration_ms = duration_ms
                e.error_snippet = error_snippet
                e.expanded = not success
                self._total_ms += duration_ms
                break
        self._update_display()

    def clear(self) -> None:
        self._entries = []
        self._total_ms = 0.0
        self._update_display()

    def on_click(self, event) -> None:
        try:
            y = event.y
        except AttributeError:
            return
        
        if self.query_one("#swarm-active").has_class("has_active"):
            running_count = sum(1 for e in self._entries if e.running)
            header_offset = 3 + (running_count * 3)
        else:
            header_offset = 3

        row_offset = y - header_offset
        if row_offset < 0: return

        current_row = 0
        for entry in self._entries:
            if entry.running: continue
            if current_row == row_offset:
                if entry.error_snippet or not entry.success:
                    entry.expanded = not entry.expanded
                    self._update_display()
                return
            current_row += 1
            if entry.expanded and entry.error_snippet:
                if current_row == row_offset:
                    entry.expanded = False
                    self._update_display()
                    return
                current_row += 1

    def _update_display(self) -> None:
        from textual.css.query import NoMatches
        try:
            active_container = self.query_one("#swarm-active", Static)
            history_container = self.query_one("#swarm-history", Static)
        except NoMatches:
            return

        running = [e for e in self._entries if e.running]
        completed = [e for e in self._entries if not e.running]

        if running:
            active_container.add_class("has_active")
            node_str = "[dim]◉ ACTIVE SWARM NODES[/dim]\n\n"
            for r in running:
                name_safe = markup_escape(r.name)
                args_safe = markup_escape(r.args_preview[:16]) if r.args_preview else "..."
                # Inverse colors block for the node card
                node_str += f"[#242018 on #c8a464] ⟳ {name_safe} [/]\n[dim]   {args_safe}[/]\n\n"
            active_container.update(node_str)
        else:
            active_container.remove_class("has_active")
            active_container.update("")

        if not completed:
            history_container.update("[dim]No tasks completed yet.[/dim]")
            return

        lines: list[str] = []
        count = len(completed)
        total_s = self._total_ms / 1000.0
        total_str = f"{total_s:.1f}s" if total_s >= 1.0 else f"{self._total_ms:.0f}ms"
        lines.append(f"[dim]◈ HISTORY ({count} calls · {total_str})[/dim]")
        lines.append("[dim]" + "─" * 28 + "[/dim]")

        for entry in completed:
            name_safe = markup_escape(entry.name)
            args_safe = markup_escape(entry.args_preview[:12]) if entry.args_preview else ""
            name_col = f"{name_safe:<14}"

            if entry.success:
                dur = self._fmt_dur(entry.duration_ms)
                lines.append(f"[#7a9e78]✓[/#7a9e78] [dim]{name_col}[/dim] [dim]{args_safe}[/dim] [#7a9e78]{dur}[/#7a9e78]")
            else:
                dur = self._fmt_dur(entry.duration_ms)
                expand_icon = "▼" if entry.expanded else "▶"
                lines.append(f"[#b87060]✗[/#b87060] [dim]{name_col}[/dim] [dim]{args_safe}[/dim] [#b87060]{dur}[/#b87060] [dim]{expand_icon}[/dim]")
                if entry.expanded and entry.error_snippet:
                    err_safe = markup_escape(entry.error_snippet[:40])
                    lines.append(f"  [dim]└ {err_safe}[/dim]")

        history_container.update("\n".join(lines))

    def _fmt_dur(self, ms: float) -> str:
        return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"
