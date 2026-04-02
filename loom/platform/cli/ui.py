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

_PT_STYLE = Style.from_dict({
    "prompt": "bold cyan",
    "": "",
})


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
        content = Text.from_markup(
            "[dim]thinking…[/dim]" if is_thinking else ""
        )
        border = "dim"
        title = "[dim green]loom[/dim green]"

    return Panel(
        content,
        title=title,
        subtitle=subtitle,
        border_style=border,
        padding=(0, 1),
    )


def tool_begin_line(name: str, args: dict[str, Any]) -> Text:
    """Rich Text for a tool-call-in-progress line."""
    args_preview = _format_args(args)
    return Text.from_markup(
        f"  [dim]~>[/dim] [yellow]{name}[/yellow]({args_preview})"
    )


def tool_end_line(name: str, success: bool, duration_ms: float) -> Text:
    """Rich Text for a completed tool-call line."""
    icon = "[green]ok[/green]" if success else "[red]!![/red]"
    return Text.from_markup(
        f"     {icon} [dim]{name}  {duration_ms:.0f}ms[/dim]"
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
