"""
ExecutionDashboard — envelope-centric execution view.

Replaces SwarmDashboard (Issue #107, TUI Phase A).
Shows the current envelope's header, level-based node list,
and a summary of recently completed envelopes.

Phase B (#108/#109):
- Node selection with up/down keys, Enter to expand detail pane
- ⏳ awaiting_confirm state rendering
- Node Detail Pane: state history, auth info, args, output
- Click ⏳ node → scroll MessageList to InlineConfirmWidget

TODO: SwarmDashboard (swarm_dashboard.py) is preserved for backward
compatibility.  Remove it once this component is confirmed stable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rich.markup import escape as markup_escape
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static

if TYPE_CHECKING:
    from loom.core.events import ExecutionEnvelopeView, ExecutionNodeView


# ── Node state → icon mapping (Parchment palette) ────────────────────────
_STATE_ICONS: dict[str, tuple[str, str]] = {
    # state → (icon, rich colour tag)
    "declared":          ("·",  "dim"),
    "awaiting_confirm":  ("⏳", "#c8924a"),
    "authorized":        ("·",  "dim"),
    "prepared":          ("·",  "dim"),
    "executing":         ("⟳", "#c8a464"),
    "observed":          ("✓", "#7a9e78"),
    "validated":         ("✓", "#7a9e78"),
    "committed":         ("✓", "#7a9e78"),
    "memorialized":      ("✓", "#7a9e78"),
    "denied":            ("⊘", "#b87060"),
    "aborted":           ("⊘", "#b87060"),
    "timed_out":         ("✗", "#b87060"),
    "reverting":         ("↩", "#c8924a"),
    "reverted":          ("↩", "#c8924a"),
}


@dataclass
class _EnvelopeHistory:
    """Lightweight record for the RECENT section."""
    envelope_id: str
    node_count: int
    fail_count: int
    elapsed_ms: float


class ExecutionDashboard(VerticalScroll):
    """Envelope-aware execution dashboard.

    Sections:
    1. Envelope Header — id, node count, group count, elapsed, status counters
    2. Level List — per-level node rows with state icons (selectable)
    3. Node Detail — expanded view of selected node
    4. Recent Envelopes — last 5 completed envelopes summary
    """

    DEFAULT_CSS = """
    ExecutionDashboard {
        overflow-y: auto;
        height: 1fr;
    }
    #exec-header {
        height: auto;
        padding-bottom: 0;
        margin-bottom: 0;
    }
    #exec-levels {
        height: auto;
        padding-bottom: 0;
        margin-bottom: 0;
    }
    #exec-detail {
        height: auto;
        padding: 0;
        margin-bottom: 1;
    }
    #exec-recent {
        height: auto;
    }
    """

    # ── Bubble messages ─────────────────────────────────────────────────

    class ScrollToConfirm(Message, bubble=True):
        """Request MessageList to scroll to the InlineConfirmWidget for this call_id."""
        def __init__(self, call_id: str) -> None:
            super().__init__()
            self.call_id = call_id

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._current_view: ExecutionEnvelopeView | None = None
        self._history: list[_EnvelopeHistory] = []
        self._selected_idx: int = -1  # -1 = no selection
        self._detail_expanded: bool = False

    def compose(self) -> ComposeResult:
        yield Static("", id="exec-header")
        yield Static("", id="exec-levels")
        yield Static("", id="exec-detail")
        yield Static("[dim]No envelopes yet.[/dim]", id="exec-recent")

    # ── Public API (called from WorkspacePanel → App) ───────────────────

    def on_envelope_started(self, view: "ExecutionEnvelopeView") -> None:
        """A new tool batch envelope was created."""
        self._current_view = view
        self._selected_idx = -1
        self._detail_expanded = False
        self._refresh_display()

    def on_envelope_updated(self, view: "ExecutionEnvelopeView") -> None:
        """A node finished inside the current envelope."""
        self._current_view = view
        self._refresh_display()

    def on_envelope_completed(self, view: "ExecutionEnvelopeView") -> None:
        """All nodes in the envelope reached terminal states."""
        self._current_view = view

        # Archive into history
        fail_count = sum(
            1 for n in view.nodes
            if n.state in ("denied", "aborted", "timed_out", "reverted")
        )
        self._history.append(_EnvelopeHistory(
            envelope_id=view.envelope_id,
            node_count=view.node_count,
            fail_count=fail_count,
            elapsed_ms=view.elapsed_ms,
        ))
        # Keep only last 5
        if len(self._history) > 5:
            self._history = self._history[-5:]

        self._refresh_display()

    def clear(self) -> None:
        """Reset dashboard state."""
        self._current_view = None
        self._history = []
        self._selected_idx = -1
        self._detail_expanded = False
        self._refresh_display()

    # ── Keyboard navigation (#108) ──────────────────────────────────────

    def key_up(self) -> None:
        """Move selection up in Level List."""
        if self._current_view is None or not self._current_view.nodes:
            return
        if self._selected_idx <= 0:
            self._selected_idx = 0
        else:
            self._selected_idx -= 1
        self._refresh_display()

    def key_down(self) -> None:
        """Move selection down in Level List."""
        if self._current_view is None or not self._current_view.nodes:
            return
        max_idx = len(self._current_view.nodes) - 1
        if self._selected_idx < max_idx:
            self._selected_idx += 1
        self._refresh_display()

    def key_enter(self) -> None:
        """Toggle detail pane for selected node, or scroll to confirm widget."""
        if self._current_view is None or self._selected_idx < 0:
            return
        nodes = self._current_view.nodes
        if self._selected_idx >= len(nodes):
            return

        node = nodes[self._selected_idx]

        # ⏳ node → scroll to confirm widget (#109)
        if node.state == "awaiting_confirm" and node.call_id:
            self.post_message(self.ScrollToConfirm(call_id=node.call_id))
            return

        self._detail_expanded = not self._detail_expanded
        self._refresh_display()

    def key_escape(self) -> None:
        """Close detail pane or deselect."""
        if self._detail_expanded:
            self._detail_expanded = False
            self._refresh_display()
        elif self._selected_idx >= 0:
            self._selected_idx = -1
            self._refresh_display()

    # ── Internal rendering ───────────────────────────────────────────────

    def _refresh_display(self) -> None:
        from textual.css.query import NoMatches
        try:
            header_w = self.query_one("#exec-header", Static)
            levels_w = self.query_one("#exec-levels", Static)
            detail_w = self.query_one("#exec-detail", Static)
            recent_w = self.query_one("#exec-recent", Static)
        except NoMatches:
            return

        view = self._current_view
        if view is None and not self._history:
            header_w.update("")
            levels_w.update("")
            detail_w.update("")
            recent_w.update("[dim]No envelopes yet.[/dim]")
            return

        # ── Header ─────────────────────────────────────────────────
        if view is not None:
            header_w.update(self._render_header(view))
            levels_w.update(self._render_levels(view))
            # ── Detail pane ────────────────────────────────────────
            if self._detail_expanded and 0 <= self._selected_idx < len(view.nodes):
                detail_w.update(self._render_detail(view.nodes[self._selected_idx]))
            else:
                detail_w.update("")
        else:
            header_w.update("")
            levels_w.update("")
            detail_w.update("")

        # ── Recent history ─────────────────────────────────────────
        recent_w.update(self._render_recent())

    def _render_header(self, view: "ExecutionEnvelopeView") -> str:
        """Render the envelope header — plain text, no box drawing."""
        elapsed = self._fmt_dur(view.elapsed_ms)

        # Status icon
        if view.status == "completed":
            status_icon = "[#7a9e78]✓[/#7a9e78]"
        elif view.status == "failed":
            status_icon = "[#b87060]✗[/#b87060]"
        else:
            status_icon = "[#c8a464]⟳[/#c8a464]"

        # Count nodes by category
        running = sum(1 for n in view.nodes if n.state == "executing")
        confirming = sum(1 for n in view.nodes if n.state == "awaiting_confirm")
        blocked = sum(1 for n in view.nodes if n.state in ("denied", "aborted"))
        failed = sum(1 for n in view.nodes if n.state in ("timed_out", "reverted"))
        done = sum(
            1 for n in view.nodes
            if n.state in ("observed", "validated", "committed", "memorialized")
        )

        lines = [
            f"{status_icon} Exec Env [bold]{view.envelope_id}[/bold]"
            f" · {view.node_count} node{'s' if view.node_count != 1 else ''}"
            f" · {elapsed}",
        ]

        # Status counters — only show non-zero
        counters = []
        if done:
            counters.append(f"[#7a9e78]done: {done}[/#7a9e78]")
        if running:
            counters.append(f"[#c8a464]running: {running}[/#c8a464]")
        if confirming:
            counters.append(f"[#c8924a]confirm: {confirming}[/#c8924a]")
        if blocked:
            counters.append(f"[#b87060]blocked: {blocked}[/#b87060]")
        if failed:
            counters.append(f"[#b87060]failed: {failed}[/#b87060]")
        if counters:
            lines.append(f"  {'  '.join(counters)}")

        return "\n".join(lines)

    def _render_levels(self, view: "ExecutionEnvelopeView") -> str:
        """Render per-level node rows with state icons. Selected node highlighted."""
        if not view.nodes:
            return ""

        lines: list[str] = []
        # Build lookup by node_id
        node_map = {n.node_id: n for n in view.nodes}
        flat_idx = 0

        for level_idx, level_ids in enumerate(view.levels):
            for node_id in level_ids:
                node: ExecutionNodeView | None = node_map.get(node_id)
                if node is None:
                    continue
                icon, colour = _STATE_ICONS.get(node.state, ("?", "dim"))
                name = markup_escape(node.tool_name)
                name_col = f"{name:<14}"

                # Duration or placeholder
                if node.state == "awaiting_confirm":
                    dur = "[#c8924a]awaiting confirm…[/#c8924a]"
                elif node.state == "executing":
                    dur = "[dim]…[/dim]"
                elif node.duration_ms > 0:
                    dur = f"[dim]{self._fmt_dur(node.duration_ms)}[/dim]"
                else:
                    dur = "[dim]waiting[/dim]"

                # Trust badge
                trust = ""
                if node.trust_level in ("GUARDED", "CRITICAL"):
                    trust_color = "#c8924a" if node.trust_level == "GUARDED" else "#b87060"
                    trust = f" [{trust_color}]{node.trust_level[0]}[/{trust_color}]"

                # Error snippet (if failed)
                error = ""
                if node.error_snippet:
                    err_safe = markup_escape(node.error_snippet[:40])
                    error = f"\n  [dim]└ {err_safe}[/dim]"

                # Selection highlight
                selected = flat_idx == self._selected_idx
                if selected:
                    row = (
                        f"[reverse] [{colour}]{icon}[/{colour}] "
                        f"{name_col}{trust} {dur} [/reverse]{error}"
                    )
                else:
                    row = (
                        f"[{colour}]{icon}[/{colour}] "
                        f"[dim]{name_col}[/dim]{trust} {dur}{error}"
                    )
                lines.append(row)
                flat_idx += 1

        # Navigation hint
        if view.nodes:
            lines.append("[dim]↑↓ select · Enter detail · Esc back[/dim]")

        return "\n".join(lines)

    def _render_detail(self, node: "ExecutionNodeView") -> str:
        """Render expanded Node Detail Pane (#108)."""
        lines: list[str] = []
        name = markup_escape(node.tool_name)
        icon, colour = _STATE_ICONS.get(node.state, ("?", "dim"))

        lines.append(f"[dim]── Detail ──────────────────────────[/dim]")
        lines.append(f"[bold]{name}[/bold]  [{colour}]{icon} {node.state}[/{colour}]")

        # Trust level + capabilities
        trust_str = node.trust_level
        if node.trust_level == "GUARDED":
            trust_str = f"[#c8924a]{node.trust_level}[/#c8924a]"
        elif node.trust_level == "CRITICAL":
            trust_str = f"[#b87060]{node.trust_level}[/#b87060]"
        else:
            trust_str = f"[#7a9e78]{node.trust_level}[/#7a9e78]"
        caps = ", ".join(node.capabilities) if node.capabilities else "none"
        lines.append(f"  Trust: {trust_str}  Caps: [dim]{caps}[/dim]")

        # Authorization info
        if node.auth_decision:
            auth_label = node.auth_decision.upper()
            if node.auth_decision == "deny":
                auth_line = f"  Auth: [#b87060]{auth_label}[/#b87060]"
            elif node.auth_decision == "scope":
                remaining = ""
                if node.auth_expires > 0:
                    secs_left = node.auth_expires - time.time()
                    if secs_left > 0:
                        mins = int(secs_left // 60)
                        secs = int(secs_left % 60)
                        remaining = f"  [dim](remaining: {mins}m {secs:02d}s)[/dim]"
                    else:
                        remaining = "  [#b87060](expired)[/#b87060]"
                auth_line = f"  Auth: [#6a7a9e]SCOPE (lease)[/#6a7a9e]{remaining}"
            elif node.auth_decision == "auto":
                auth_line = f"  Auth: [#7a6a9e]AUTO (permanent)[/#7a6a9e]"
            else:
                auth_line = f"  Auth: [#7a9e78]ONCE[/#7a9e78]"
            lines.append(auth_line)
            if node.auth_selector:
                lines.append(f"  Selector: [dim]{markup_escape(node.auth_selector)}[/dim]")

        # Duration
        if node.duration_ms > 0:
            lines.append(f"  Duration: {self._fmt_dur(node.duration_ms)}")

        # Full args
        if node.full_args:
            lines.append(f"  [dim]Args:[/dim]")
            for k, v in node.full_args.items():
                v_str = str(v)
                if len(v_str) > 80:
                    v_str = v_str[:77] + "..."
                lines.append(f"    [dim]{markup_escape(k)}:[/dim] {markup_escape(v_str)}")

        # Output preview
        if node.output_preview:
            preview = markup_escape(node.output_preview[:120])
            lines.append(f"  [dim]Output:[/dim] {preview}")

        # State history timeline
        if node.state_history:
            lines.append(f"  [dim]History:[/dim]")
            for entry in node.state_history:
                ts = entry.get("ts", "")
                # Show only HH:MM:SS from ISO timestamp
                if "T" in ts:
                    ts = ts.split("T")[1][:8]
                from_s = entry.get("from", "?")
                to_s = entry.get("to", "?")
                reason = entry.get("reason", "")
                reason_str = f" [dim]({markup_escape(reason[:40])})[/dim]" if reason else ""
                lines.append(f"    [dim]{ts}[/dim] {from_s} → {to_s}{reason_str}")

        # Error
        if node.error_snippet:
            err_safe = markup_escape(node.error_snippet)
            lines.append(f"  [#b87060]Error: {err_safe}[/#b87060]")

        return "\n".join(lines)

    def _render_recent(self) -> str:
        """Render the RECENT section with completed envelope summaries."""
        if not self._history:
            return "[dim]No completed envelopes.[/dim]"

        lines: list[str] = []
        lines.append(f"[dim]── RECENT ({len(self._history)}) ──────────────────[/dim]")

        for h in reversed(self._history):
            elapsed = self._fmt_dur(h.elapsed_ms)
            if h.fail_count > 0:
                status = f"[#b87060]✗[/#b87060]"
                detail = f"{h.node_count} actions · {h.fail_count} failed · {elapsed}"
            else:
                status = f"[#7a9e78]✓[/#7a9e78]"
                detail = f"{h.node_count} actions · {elapsed}"
            lines.append(f"{status} [dim]{h.envelope_id}  {detail}[/dim]")

        return "\n".join(lines)

    @staticmethod
    def _fmt_dur(ms: float) -> str:
        return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"
