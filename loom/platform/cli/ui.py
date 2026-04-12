"""
CLI UI components — Rich Live streaming + prompt_toolkit input.

Event model
-----------
``stream_turn()`` yields a sequence of typed events defined in
``loom.core.events``.  They are re-exported here for backwards compatibility.

Display strategy
----------------
- prompt_toolkit PromptSession handles input history and slash-command
  autocomplete.
- Rich Live handles the streaming response panel.  Tool call rows are
  printed above the live area via console.print() so they persist.
- The live panel is a Rich Panel whose subtitle carries the context %
  budget — no extra renderable composition needed.
"""

from __future__ import annotations

import json
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

# ---------------------------------------------------------------------------
# Chat event types — defined in loom.core.events, re-exported for compat
# ---------------------------------------------------------------------------

from loom.core.events import (  # noqa: E402, F401
    ActionRolledBack,
    ActionStateChange,
    CompressDone,
    TextChunk,
    ThinkCollapsed,
    ToolBegin,
    ToolEnd,
    TurnDone,
    TurnDropped,
    TurnPaused,
)


# ---------------------------------------------------------------------------
# Slash command autocomplete
# ---------------------------------------------------------------------------

_SLASH_WORDS = [
    "/personality",
    "/personality off",
    "/personality adversarial",
    "/personality minimalist",
    "/personality architect",
    "/personality researcher",
    "/personality operator",
    "/think",
    "/new",
    "/compact",
    "/scope",
    "/scope revoke",
    "/scope clear",
    "/pause",
    "/stop",
    "/help",
]


class SlashCompleter(WordCompleter):
    def __init__(self) -> None:
        super().__init__(
            _SLASH_WORDS,
            match_middle=False,
            sentence=True,
        )


# ---------------------------------------------------------------------------
# prompt_toolkit session factory
# ---------------------------------------------------------------------------

_PT_STYLE = Style.from_dict(
    {
        "prompt": "bold cyan",
        "": "",
    }
)


def _build_key_bindings() -> KeyBindings:
    """Build key bindings for Ctrl+L (clear)."""
    kb = KeyBindings()

    @kb.add("c-l")
    def clear_screen(_) -> None:
        """Ctrl+L: clear terminal screen."""
        from rich.console import Console

        _console = Console()
        _console.clear()

    return kb


def make_prompt_session() -> PromptSession:
    """Create a PromptSession with input history, slash autocomplete, and key bindings."""
    return PromptSession(
        history=InMemoryHistory(),
        completer=SlashCompleter(),
        complete_while_typing=True,
        style=_PT_STYLE,
        key_bindings=_build_key_bindings(),
    )


# ---------------------------------------------------------------------------
# Rich display helpers
# ---------------------------------------------------------------------------


def render_header(model: str, db: str) -> Panel:
    """Top-of-session banner."""
    return Panel(
        Text.from_markup(
            f"[bold cyan]Loom[/bold cyan]  [dim]v0.3.0[/dim]\n"
            f"model [green]{model}[/green]  |  memory [dim]{db}[/dim]\n"
            f"[dim]Type [bold]exit[/bold] or Ctrl-C to quit  |  "
            f"/personality <name>  |  /compact  |  /help[/dim]"
        ),
        border_style="cyan",
    )


def response_panel(
    text: str,
    *,
    is_thinking: bool = False,
    budget_fraction: float = 0.0,
    personality: str | None = None,
) -> Panel:
    """
    Return the main assistant-response panel used inside the Live context.

    ``text``           — accumulated streaming text so far
    ``is_thinking``    — show "thinking…" when no text yet
    ``budget_fraction``— 0–1 fraction of context window used
    ``personality``    — active personality name (shown in subtitle)
    """
    pct = budget_fraction * 100
    ctx_color = "green" if pct < 60 else "yellow" if pct < 85 else "red"
    subtitle_parts = [f"[{ctx_color}]context {pct:.1f}%[/{ctx_color}]"]
    if personality:
        subtitle_parts.append(f"[dim]persona: {personality}[/dim]")
    subtitle = "  ".join(subtitle_parts)

    if text:
        content = Markdown(text)
        border = "green"
        title = "[bold green]loom[/bold green]"
    else:
        content = Text.from_markup("[dim]thinking…[/dim]" if is_thinking else "")
        border = "dim"
        title = "[dim green]loom[/dim green]"

    return Panel(
        content,
        title=title,
        subtitle=subtitle,
        border_style=border,
        padding=(0, 1),
    )


# ASCII spinner frames (cp950 safe, Rich-markup safe — no backslash)
_SPINNER_FRAMES = ["-", "~", "|", "+"]

def tool_spinner_line(name: str, args: dict[str, Any], frame_index: int = 0) -> Text:
    """
    Rich Text for a tool-call-in-progress line with ASCII spinner.
    Frame index 0 shows [- ], 1 shows [\\ ], etc.
    """
    spinner = _SPINNER_FRAMES[frame_index % len(_SPINNER_FRAMES)]
    args_preview = _format_args(args)
    return Text.from_markup(
        f"  [dim][[/dim][yellow]{spinner}[/yellow][dim]][/dim] "
        f"[yellow]{name}[/yellow]"
        f"{f'({args_preview})' if args_preview else ''}"
    )


def tool_begin_line(name: str, args: dict[str, Any]) -> Text:
    """Rich Text for a tool-call-in-progress line (alias for compatibility)."""
    return tool_spinner_line(name, args, 0)


def tool_running_line(name: str, frame_index: int = 0) -> Text:
    """
    Rich Text for a tool that is currently executing.
    Shows animated spinner without args (args already shown at begin).
    """
    spinner = _SPINNER_FRAMES[frame_index % len(_SPINNER_FRAMES)]
    return Text.from_markup(
        f"  [dim][[/dim][yellow]{spinner}[/yellow][dim]][/dim] "
        f"[dim]{name} running...[/dim]"
    )


def tool_end_line(name: str, success: bool, duration_ms: float) -> Text:
    """Rich Text for a completed tool-call line."""
    icon = "[green]ok[/green]" if success else "[red]!![/red]"
    status = "[green]done[/green]" if success else "[red]failed[/red]"
    return Text.from_markup(
        f"  [dim][[/dim]{icon}[dim]][/dim] "
        f"[dim]{name}[/dim]  "
        f"[{('green' if success else 'red')}]{duration_ms:.0f}ms[/{('green' if success else 'red')}]  "
        f"[dim]{status}[/dim]"
    )


# Streaming cursor helpers
_SHOW_CURSOR = True


def set_show_cursor(visible: bool) -> None:
    """Enable or disable streaming cursor display."""
    global _SHOW_CURSOR
    _SHOW_CURSOR = visible


def streaming_cursor() -> str:
    """Return the streaming cursor string (ASCII-safe)."""
    return ">" if _SHOW_CURSOR else ""


def clear_line_escape() -> str:
    """Return ANSI escape to clear the current line."""
    return "\r\033[K"


def clear_line() -> None:
    """
    Write \\r\\033[K directly to stdout, bypassing Rich Console.

    Rich's ``Console.print`` strips the \\r (carriage return), which
    prevents the cursor from returning to column 0.  Writing directly
    to ``sys.stdout`` preserves the full escape sequence.
    """
    import sys
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()


def render_cursor() -> Text:
    """Return Rich Text with just the cursor."""
    return Text.from_markup(f"[bold yellow]{streaming_cursor()}[/bold yellow]")


def status_bar(
    context_fraction: float,
    input_tokens: int,
    output_tokens: int,
    elapsed_ms: float,
    tool_count: int,
) -> Text:
    """
    Render the closing status bar after a turn completes.
    Color-coded context bar with token counts and timing.
    """
    pct = context_fraction * 100
    ctx_color = "green" if pct < 60 else "yellow" if pct < 85 else "red"

    bar_len = 10
    filled = int(bar_len * context_fraction)
    bar = "#" * filled + "." * (bar_len - filled)

    return Text.from_markup(
        f"[dim]-[/dim]"
        f"[{ctx_color}]{bar}[/{ctx_color}]"
        f"[dim] context {pct:.1f}%  |  "
        f"{input_tokens}in / {output_tokens}out  |  "
        f"{elapsed_ms / 1000:.1f}s  |  "
        f"{tool_count} tool{'s' if tool_count != 1 else ''}"
        f"[/dim][dim]-[/dim]"
    )


def _smart_truncate(value: str, max_len: int = 80) -> str:
    """
    Truncate a string, preserving start and end for readability.

    For path-like values (containing /), keeps the first component and
    last components visible: ``/Users/…/cli/tools.py``.
    For other strings, keeps the first ``max_len`` characters.
    """
    if len(value) <= max_len:
        return value
    # Path-like: keep leading prefix + trailing meaningful portion
    if "/" in value or "\\" in value:
        # Reserve space for "…" connector
        head_budget = max_len // 3
        tail_budget = max_len - head_budget - 1  # -1 for "…"
        return value[:head_budget] + "…" + value[-tail_budget:]
    return value[:max_len] + "…"


def _format_args(args: dict[str, Any], max_value_len: int = 80) -> str:
    """Compact one-line preview of tool arguments."""
    if not args:
        return ""
    parts: list[str] = []
    for k, v in args.items():
        if isinstance(v, str):
            snippet = _smart_truncate(v.replace("\n", "↵"), max_value_len)
            parts.append(f'{k}="{snippet}"')
        elif isinstance(v, (dict, list)):
            parts.append(f"{k}={{…}}")
        else:
            parts.append(f"{k}={v!r}")
        if len(parts) >= 3:
            break
    return ", ".join(parts)
