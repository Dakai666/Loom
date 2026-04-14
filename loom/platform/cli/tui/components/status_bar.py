"""
StatusBar component — bottom bar with session stats.

Design:
  [GUARDED]  ctx 45% [▓▓▓▓▓░░░░░]  1.2k in / 340 out  2.3s  3 tools
  grants: 2 active · next expiry [red]3m 12s[/red]

Grants indicator (#112):
  TTL > 10m  → green
  5–10m      → yellow
  < 5m       → red
  all expired → dim / hidden
"""

from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from loom.platform.cli.tui.events import GrantInfo


class StatusBar(Widget):
    """
    Bottom status bar showing context budget and token usage.

    Color-coded context bar: green < 60%, yellow 60-85%, red > 85%.
    Shows trust mode, token counts, elapsed time, tool count, active grants.
    """

    context_fraction: reactive[float] = reactive(0.0)
    input_tokens: reactive[int] = reactive(0)
    output_tokens: reactive[int] = reactive(0)
    elapsed_ms: reactive[float] = reactive(0.0)
    tool_count: reactive[int] = reactive(0)
    active_grants: reactive[int] = reactive(0)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._grants: list[GrantInfo] = []
        self._known_grant_ids: set[str] = set()  # for expiry detection (#112)
        self._refresh_timer = None

    def compose(self) -> ComposeResult:
        yield Static("", id="status-content")

    def on_mount(self) -> None:
        self._update_display()
        # Periodic refresh every 30s for TTL countdown (#112)
        self._refresh_timer = self.set_interval(30, self._tick_grants)

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

    def update_grants(
        self,
        active: int,
        next_expiry_secs: float = 0.0,
        grants: list[GrantInfo] | None = None,
    ) -> None:
        """Update active grants indicator (#108, #112)."""
        self.active_grants = active
        if grants is not None:
            old_ids = self._known_grant_ids
            new_ids = {g.grant_id for g in grants}
            # Detect newly expired grants (were known, now missing)
            expired_ids = old_ids - new_ids
            if expired_ids and old_ids:
                self._notify_expired(expired_ids)
            self._grants = list(grants)
            self._known_grant_ids = new_ids
        self._update_display()

    def _tick_grants(self) -> None:
        """Periodic callback: re-render grants with updated TTL + detect expiry."""
        if not self._grants:
            return
        now = time.time()
        # Check for newly expired grants
        still_active: list[GrantInfo] = []
        expired_ids: set[str] = set()
        for g in self._grants:
            if g.expires_at > 0 and g.expires_at <= now:
                expired_ids.add(g.grant_id)
            else:
                still_active.append(g)
        if expired_ids:
            self._notify_expired(expired_ids)
            self._grants = still_active
            self._known_grant_ids -= expired_ids
            self.active_grants = len(still_active)
        self._update_display()

    def _notify_expired(self, expired_ids: set[str]) -> None:
        """Fire toast notification for expired grants (#112)."""
        # Find matching grant info from our list
        for g in self._grants:
            if g.grant_id in expired_ids:
                selector_hint = f" / {g.selector}" if g.selector else ""
                self.app.notify(
                    f"Scope lease expired: {g.tool_name}{selector_hint}",
                    title="Lease Expired",
                    severity="warning",
                    timeout=5,
                )

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

        # Active grants indicator (#108, #112 — TTL color coding)
        grants_str = self._render_grants()
        if grants_str:
            parts.append(grants_str)

        content.update("  [dim]|[/dim]  ".join(parts))

    def _render_grants(self) -> str:
        """Render grants indicator with TTL-based color coding (#112)."""
        if self.active_grants <= 0 and not self._grants:
            return ""

        now = time.time()

        # Filter still-active grants for display
        active_grants = [
            g for g in self._grants
            if g.expires_at == 0 or g.expires_at > now
        ]

        if not active_grants:
            # All expired — dim indicator
            return "[dim]grants: 0 active[/dim]"

        # Find nearest expiry among active grants
        nearest_secs = 0.0
        for g in active_grants:
            if g.expires_at > 0:
                remaining = g.expires_at - now
                if remaining > 0:
                    if nearest_secs == 0.0 or remaining < nearest_secs:
                        nearest_secs = remaining

        count = len(active_grants)
        if nearest_secs > 0:
            mins = int(nearest_secs // 60)
            secs = int(nearest_secs % 60)
            # Color by TTL threshold (#112) — parchment palette
            if nearest_secs < 300:        # < 5 min → warm red
                expiry_str = f"[#b87060]{mins}m {secs:02d}s[/#b87060]"
            elif nearest_secs < 600:      # 5-10 min → ochre warning
                expiry_str = f"[#c8924a]{mins}m {secs:02d}s[/#c8924a]"
            else:                          # > 10 min → sage green
                expiry_str = f"[#7a9e78]{mins}m[/#7a9e78]"
            return (
                f"[dim]grants:[/dim] [bold]{count}[/bold] active"
                f" [dim]· expiry[/dim] {expiry_str}"
            )
        else:
            # All permanent grants (no TTL)
            return f"[dim]grants:[/dim] [bold]{count}[/bold] active [dim]· permanent[/dim]"
