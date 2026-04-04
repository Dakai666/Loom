"""
BudgetPanel component — Context Budget visualisation.

Shows:
  - Progress bar with colour zones (green / ochre / terracotta)
  - Absolute token numbers: used / total
  - "Distance to auto-compact" in tokens and estimated turns
  - This turn's input / output token counts

Colour zones:
  < 60%   — #7a9e78 sage green    (healthy)
  60-80%  — #c8924a ochre         (watch out)
  80-95%  — #c8744a warm orange   (compact soon)
  > 95%   — #b87060 terracotta    (critical)
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


_COMPACT_THRESHOLD = 0.80
_CRITICAL_THRESHOLD = 0.95
_BAR_WIDTH = 20


def _bar_color(fraction: float) -> str:
    if fraction < 0.60:
        return "#7a9e78"
    if fraction < 0.80:
        return "#c8924a"
    if fraction < 0.95:
        return "#c8744a"
    return "#b87060"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


class BudgetPanel(Widget):
    """
    Context Budget panel for Workspace.

    Update via update_budget() whenever a BudgetUpdate or TurnDone event fires.
    """

    DEFAULT_CSS = """
    BudgetPanel {
        overflow-y: auto;
        height: 1fr;
        padding: 0 1;
    }
    """

    fraction: reactive[float] = reactive(0.0)
    used_tokens: reactive[int] = reactive(0)
    max_tokens: reactive[int] = reactive(100_000)
    input_tokens: reactive[int] = reactive(0)
    output_tokens: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        yield Static("", id="budget-content")

    def on_mount(self) -> None:
        self._update_display()

    def update_budget(
        self,
        fraction: float,
        used_tokens: int,
        max_tokens: int,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        self.fraction = fraction
        self.used_tokens = used_tokens
        self.max_tokens = max_tokens if max_tokens > 0 else self.max_tokens
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self._update_display()

    def watch_fraction(self, _: float) -> None:
        self._update_display()

    def _update_display(self) -> None:
        from textual.css.query import NoMatches

        try:
            content = self.query_one("#budget-content", Static)
        except NoMatches:
            return

        frac = max(0.0, min(1.0, self.fraction))
        color = _bar_color(frac)
        pct = frac * 100

        # Progress bar
        filled = int(_BAR_WIDTH * frac)
        empty = _BAR_WIDTH - filled
        bar = f"[{color}]{'█' * filled}[/{color}][dim]{'░' * empty}[/dim]"

        # Absolute numbers
        used_str = _fmt_tokens(self.used_tokens)
        max_str = _fmt_tokens(self.max_tokens)

        # Distance to compact
        tokens_to_compact = max(0, int(self.max_tokens * _COMPACT_THRESHOLD) - self.used_tokens)
        tokens_to_critical = max(0, int(self.max_tokens * _CRITICAL_THRESHOLD) - self.used_tokens)

        # Estimate turns remaining (based on last turn's input token delta)
        turns_est = ""
        if self.input_tokens > 0 and tokens_to_compact > 0:
            # input_tokens is total context size after this turn, approximating tokens/turn
            # as a rough fraction of current usage
            avg_per_turn = max(1, self.used_tokens // max(1, self._estimate_turn_count()))
            turns_n = max(1, tokens_to_compact // avg_per_turn)
            turns_est = f" [dim](~{turns_n} turn{'s' if turns_n != 1 else ''})[/dim]"

        lines: list[str] = []

        lines.append("[dim]Context Usage[/dim]")
        lines.append(f"{bar}  [{color}]{pct:.0f}%[/{color}]")
        lines.append(f"[dim]{used_str} / {max_str} tokens[/dim]")
        lines.append("")

        lines.append("[dim]" + "─" * 28 + "[/dim]")

        # Compact threshold
        if frac < _COMPACT_THRESHOLD:
            compact_color = "#c8924a"
            lines.append(
                f"[{compact_color}]⚡ Auto-compact at 80%[/{compact_color}]"
            )
            lines.append(
                f"   [dim]{_fmt_tokens(tokens_to_compact)} tokens remaining[/dim]"
                f"{turns_est}"
            )
        else:
            lines.append("[#b87060]⚡ Compact threshold reached[/#b87060]")
            lines.append(f"   [dim]Run /compact to free context[/dim]")

        lines.append("")

        # Critical threshold
        if frac < _CRITICAL_THRESHOLD:
            crit_color = "#b87060"
            lines.append(
                f"[dim]🔴 Force-compress at 95%[/dim]"
            )
            lines.append(
                f"   [dim]{_fmt_tokens(tokens_to_critical)} tokens remaining[/dim]"
            )
        else:
            lines.append("[#b87060]🔴 Critical — compression imminent[/#b87060]")

        lines.append("")
        lines.append("[dim]" + "─" * 28 + "[/dim]")

        # This turn
        if self.input_tokens > 0:
            lines.append("[dim]This turn[/dim]")
            lines.append(
                f"   [dim]In    {_fmt_tokens(self.input_tokens)} tokens[/dim]"
            )
            lines.append(
                f"   [dim]Out   {_fmt_tokens(self.output_tokens)} tokens[/dim]"
            )

        content.update("\n".join(lines))

    def _estimate_turn_count(self) -> int:
        """Rough estimate of how many turns have elapsed, for turns-remaining calc."""
        # We don't have turn count directly; approximate from output tokens
        # (each turn produces ~200-800 output tokens on average)
        if self.output_tokens > 0:
            return max(1, self.used_tokens // max(1, self.output_tokens))
        return 5  # safe fallback
