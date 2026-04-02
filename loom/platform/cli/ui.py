"""
CLI UI components — Rich Live streaming + prompt_toolkit input.

Event model
-----------
``stream_turn()`` yields a sequence of typed events:

    TextChunk   — partial text from the LLM (stream in progress)
    ToolBegin   — a tool call is about to execute
    ToolEnd     — a tool call finished
    TurnDone    — the full agent turn is complete (all tool loops resolved)

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
from dataclasses import dataclass, field
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style


# ---------------------------------------------------------------------------
# Chat event types
# ---------------------------------------------------------------------------


@dataclass
class TextChunk:
    """A fragment of streaming LLM text."""

    text: str


@dataclass
class ToolBegin:
    """The agent is about to call a tool."""

    name: str
    args: dict[str, Any]
    call_id: str


@dataclass
class ToolEnd:
    """A tool call finished."""

    name: str
    success: bool
    output: str
    duration_ms: float
    call_id: str


@dataclass
class TurnDone:
    """The complete agent turn (including all tool loops) is done."""

    tool_count: int
    input_tokens: int
    output_tokens: int
    elapsed_ms: float


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
    "/compact",
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


def make_prompt_session() -> PromptSession:
    """Create a PromptSession with input history and slash autocomplete."""
    return PromptSession(
        history=InMemoryHistory(),
        completer=SlashCompleter(),
        complete_while_typing=True,
        style=_PT_STYLE,
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


# ASCII spinner frames (cp950 safe)
_SPINNER_FRAMES = ["-", "\\", "|", "/"]

# Verbosity mode (shared state, toggled by Ctrl+O)
_verbose_mode = False


def toggle_verbose_mode() -> bool:
    """Toggle verbose mode, return new state."""
    global _verbose_mode
    _verbose_mode = not _verbose_mode
    return _verbose_mode


def is_verbose_mode() -> bool:
    """Return current verbosity mode."""
    return _verbose_mode


def set_verbose_mode(enabled: bool) -> None:
    """Enable or disable verbose tool output."""
    global _verbose_mode
    _verbose_mode = enabled


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


def tool_end_verbose_line(
    name: str, success: bool, duration_ms: float, output: str
) -> Text:
    """
    Rich Text for a completed tool-call line with full output preview.
    Only used when verbose mode is enabled.
    """
    icon = "[green]ok[/green]" if success else "[red]!![/red]"
    output_preview = output[:120].replace("\n", " ") if output else ""
    if len(output) > 120:
        output_preview += "..."
    return Text.from_markup(
        f"  [dim][[/dim]{icon}[dim]][/dim] "
        f"[dim]{name}[/dim]  "
        f"[{('green' if success else 'red')}]{duration_ms:.0f}ms[/{('green' if success else 'red')}]\n"
        f"       [dim]result:[/dim] {output_preview}"
    )


# Streaming cursor helpers
_SHOW_CURSOR = True


def set_show_cursor(visible: bool) -> None:
    """Enable or disable streaming cursor display."""
    global _SHOW_CURSOR
    _SHOW_CURSOR = visible


def streaming_cursor() -> str:
    """Return the streaming cursor string."""
    return "▌" if _SHOW_CURSOR else ""


def clear_line_escape() -> str:
    """Return ANSI escape to clear the current line."""
    return "\r\033[K"


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
    bar = "▓" * filled + "░" * (bar_len - filled)

    return Text.from_markup(
        f"[dim]-[/dim]"
        f"[{ctx_color}]{bar}[/{ctx_color}]"
        f"[dim] context {pct:.1f}%  |  "
        f"{input_tokens}in / {output_tokens}out  |  "
        f"{elapsed_ms / 1000:.1f}s  |  "
        f"{tool_count} tool{'s' if tool_count != 1 else ''}"
        f"[dim]-[/dim]"
    )


def _format_args(args: dict[str, Any]) -> str:
    """Compact one-line preview of tool arguments."""
    if not args:
        return ""
    parts: list[str] = []
    for k, v in args.items():
        if isinstance(v, str):
            snippet = v[:40].replace("\n", "↵")
            parts.append(f'{k}="{snippet}{"…" if len(v) > 40 else ""}"')
        elif isinstance(v, (dict, list)):
            parts.append(f"{k}={{…}}")
        else:
            parts.append(f"{k}={v!r}")
        if len(parts) >= 3:
            break
    return ", ".join(parts)
