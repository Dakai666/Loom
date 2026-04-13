"""
ExecutionDashboard — envelope-centric execution view.

Replaces SwarmDashboard (Issue #107, TUI Phase A).
Shows the current envelope's header, level-based node list,
and a summary of recently completed envelopes.

TODO: SwarmDashboard (swarm_dashboard.py) is preserved for backward
compatibility.  Remove it once this component is confirmed stable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rich.markup import escape as markup_escape
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

if TYPE_CHECKING:
    from loom.core.events import ExecutionEnvelopeView, ExecutionNodeView


# ── Node state → icon mapping (Parchment palette) ────────────────────────
_STATE_ICONS: dict[str, tuple[str, str]] = {
    # state → (icon, rich colour tag)
    "declared":      ("·",  "dim"),
    "authorized":    ("·",  "dim"),
    "prepared":      ("·",  "dim"),
    "executing":     ("⟳", "#c8a464"),
    "observed":      ("✓", "#7a9e78"),
    "validated":     ("✓", "#7a9e78"),
    "committed":     ("✓", "#7a9e78"),
    "memorialized":  ("✓", "#7a9e78"),
    "denied":        ("⊘", "#b87060"),
    "aborted":       ("⊘", "#b87060"),
    "timed_out":     ("✗", "#b87060"),
    "reverting":     ("↩", "#c8924a"),
    "reverted":      ("↩", "#c8924a"),
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
    2. Level List — per-level node rows with state icons
    3. Recent Envelopes — last 5 completed envelopes summary
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
        margin-bottom: 1;
    }
    #exec-recent {
        height: auto;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._current_view: ExecutionEnvelopeView | None = None
        self._history: list[_EnvelopeHistory] = []

    def compose(self) -> ComposeResult:
        yield Static("", id="exec-header")
        yield Static("", id="exec-levels")
        yield Static("[dim]No envelopes yet.[/dim]", id="exec-recent")

    # ── Public API (called from WorkspacePanel → App) ───────────────────

    def on_envelope_started(self, view: "ExecutionEnvelopeView") -> None:
        """A new tool batch envelope was created."""
        self._current_view = view
        self._render()

    def on_envelope_updated(self, view: "ExecutionEnvelopeView") -> None:
        """A node finished inside the current envelope."""
        self._current_view = view
        self._render()

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

        self._render()
        # Clear current view after rendering so the completed state is visible
        # until the next envelope starts.

    def clear(self) -> None:
        """Reset dashboard state."""
        self._current_view = None
        self._history = []
        self._render()

    # ── Internal rendering ───────────────────────────────────────────────

    def _render(self) -> None:
        from textual.css.query import NoMatches
        try:
            header_w = self.query_one("#exec-header", Static)
            levels_w = self.query_one("#exec-levels", Static)
            recent_w = self.query_one("#exec-recent", Static)
        except NoMatches:
            return

        view = self._current_view
        if view is None and not self._history:
            header_w.update("")
            levels_w.update("")
            recent_w.update("[dim]No envelopes yet.[/dim]")
            return

        # ── Header ─────────────────────────────────────────────────
        if view is not None:
            header_w.update(self._render_header(view))
            levels_w.update(self._render_levels(view))
        else:
            header_w.update("")
            levels_w.update("")

        # ── Recent history ─────────────────────────────────────────
        recent_w.update(self._render_recent())

    def _render_header(self, view: "ExecutionEnvelopeView") -> str:
        """Render the envelope header block."""
        elapsed = self._fmt_dur(view.elapsed_ms)

        # Status colour
        if view.status == "completed":
            status_icon = "[#7a9e78]✓[/#7a9e78]"
        elif view.status == "failed":
            status_icon = "[#b87060]✗[/#b87060]"
        else:
            status_icon = "[#c8a464]⟳[/#c8a464]"

        # Count nodes by category
        running = sum(1 for n in view.nodes if n.state == "executing")
        blocked = sum(1 for n in view.nodes if n.state in ("denied", "aborted"))
        failed = sum(1 for n in view.nodes if n.state in ("timed_out", "reverted"))
        done = sum(
            1 for n in view.nodes
            if n.state in ("observed", "validated", "committed", "memorialized")
        )

        lines = [
            f"╭─ {status_icon} Envelope [bold]{view.envelope_id}[/bold]"
            f" · {view.node_count} node{'s' if view.node_count != 1 else ''}"
            f" · {elapsed} ─╮",
        ]

        # Status counters — only show non-zero
        counters = []
        if done:
            counters.append(f"[#7a9e78]done: {done}[/#7a9e78]")
        if running:
            counters.append(f"[#c8a464]running: {running}[/#c8a464]")
        if blocked:
            counters.append(f"[#b87060]blocked: {blocked}[/#b87060]")
        if failed:
            counters.append(f"[#b87060]failed: {failed}[/#b87060]")
        if counters:
            lines.append(f"│ {'  '.join(counters)}")

        lines.append(f"╰{'─' * 40}╯")
        return "\n".join(lines)

    def _render_levels(self, view: "ExecutionEnvelopeView") -> str:
        """Render per-level node rows with state icons."""
        if not view.nodes:
            return ""

        lines: list[str] = []
        # Build lookup by node_id
        node_map = {n.node_id: n for n in view.nodes}

        for level_idx, level_ids in enumerate(view.levels):
            for node_id in level_ids:
                node: ExecutionNodeView | None = node_map.get(node_id)
                if node is None:
                    continue
                icon, colour = _STATE_ICONS.get(node.state, ("?", "dim"))
                name = markup_escape(node.tool_name)
                name_col = f"{name:<14}"

                # Duration or placeholder
                if node.state == "executing":
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

                lines.append(
                    f"[{colour}]{icon}[/{colour}] [dim]{name_col}[/dim]{trust} {dur}{error}"
                )

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
