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
from dataclasses import dataclass, field
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import (
    CompleteEvent,
    Completer,
    Completion,
    FuzzyCompleter,
)
from prompt_toolkit.document import Document
from prompt_toolkit.filters import has_focus
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
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
# Slash command catalog — single source of truth for completer + /help
# ---------------------------------------------------------------------------
#
# Each entry: (command, description). Descriptions show as completion meta.
# Keep alphabetically grouped by command root for predictable nav.

SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/auto",                       "toggle run_bash auto-approve (requires strict_sandbox)"),
    ("/compact",                    "compress older context (smart compaction)"),
    ("/help",                       "show command reference"),
    ("/model",                      "show current model + registered providers"),
    ("/model claude-sonnet-4-6",    "switch to Anthropic Claude Sonnet 4.6"),
    ("/model claude-opus-4-7",      "switch to Anthropic Claude Opus 4.7"),
    ("/model MiniMax-M2.7",         "switch to MiniMax-M2.7 (default)"),
    ("/name",                       "rename the current session"),
    ("/new",                        "start a fresh session"),
    ("/pause",                      "toggle HITL pause after each tool batch"),
    ("/personality",                "show active persona + available list"),
    ("/personality off",            "clear active persona"),
    ("/personality adversarial",    "switch persona → adversarial"),
    ("/personality architect",      "switch persona → architect"),
    ("/personality minimalist",     "switch persona → minimalist"),
    ("/personality operator",       "switch persona → operator"),
    ("/personality researcher",     "switch persona → researcher"),
    ("/scope",                      "list active scope grants"),
    ("/scope clear",                "revoke all non-system grants"),
    ("/scope revoke",               "revoke a specific grant by index"),
    ("/sessions",                   "browse and switch sessions"),
    ("/stop",                       "interrupt a running turn (CLI: Ctrl+C)"),
    ("/think",                      "view last turn's reasoning chain"),
]


class SlashCompleter(Completer):
    """Slash-command completer with metadata + prefix matching.

    Wrap with :class:`FuzzyCompleter` for fuzzy match — done in
    :func:`make_prompt_session`.
    """

    def __init__(self, commands: list[tuple[str, str]] | None = None) -> None:
        self._commands = commands if commands is not None else SLASH_COMMANDS

    def get_completions(self, document: Document, _: CompleteEvent):
        # Only complete on the first line, when text starts with "/".
        # Avoids triggering completer on subsequent lines of multi-line input.
        text = document.text_before_cursor
        if "\n" in text:
            return
        if not text.startswith("/"):
            return
        for cmd, desc in self._commands:
            if cmd.startswith(text):
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=cmd,
                    display_meta=desc,
                )


# ---------------------------------------------------------------------------
# prompt_toolkit session factory
# ---------------------------------------------------------------------------

from loom.platform.cli.theme import (
    PARCHMENT_ACCENT,
    PARCHMENT_BG,
    PARCHMENT_BORDER,
    PARCHMENT_ERROR,
    PARCHMENT_MUTED,
    PARCHMENT_SUCCESS,
    PARCHMENT_SURFACE,
    PARCHMENT_TEXT,
)

# prompt_toolkit's Style.from_dict can't read from a Rich Theme — it has
# its own colour vocabulary. Reference PARCHMENT_* constants so the two
# layers stay aligned without manually duplicating hex codes.
_PT_STYLE = Style.from_dict(
    {
        "prompt": f"bold {PARCHMENT_ACCENT}",
        "prompt.continuation": PARCHMENT_MUTED,
        "auto-suggestion": PARCHMENT_BORDER,
        "completion-menu":                         f"bg:{PARCHMENT_SURFACE} {PARCHMENT_TEXT}",
        "completion-menu.completion":              f"bg:{PARCHMENT_SURFACE} {PARCHMENT_TEXT}",
        "completion-menu.completion.current":      f"bg:{PARCHMENT_ACCENT} {PARCHMENT_BG} bold",
        "completion-menu.meta.completion":         f"bg:{PARCHMENT_SURFACE} {PARCHMENT_MUTED}",
        "completion-menu.meta.completion.current": f"bg:{PARCHMENT_ACCENT} {PARCHMENT_BG}",
        "": "",
    }
)

_HISTORY_PATH = Path.home() / ".loom" / "cli_history"


def _build_key_bindings() -> KeyBindings:
    """Build key bindings.

    Defaults provided by prompt_toolkit (we don't override):
      - Ctrl+C / Ctrl+D on empty buffer → exit
      - Up / Down → history navigation (single-line buffer)
      - Tab → completion

    Custom:
      - Ctrl+L              → clear screen
      - Enter               → submit (even in multiline mode)
      - Alt+Enter / Esc,Enter → insert newline
      - Esc (alone, double-tap) → clear buffer
    """
    kb = KeyBindings()

    @kb.add("c-l")
    def clear_screen(event) -> None:
        from rich.console import Console
        _console = Console()
        _console.clear()
        # Re-render the prompt after clear
        event.app.renderer.reset()
        event.app.invalidate()

    @kb.add("enter", filter=has_focus("DEFAULT_BUFFER"))
    def submit_on_enter(event) -> None:
        """Plain Enter → submit (overrides multiline default of inserting newline)."""
        buf = event.current_buffer
        # Empty buffer → no-op (don't submit empty input)
        if not buf.text.strip():
            return
        buf.validate_and_handle()

    @kb.add("escape", "enter", filter=has_focus("DEFAULT_BUFFER"))
    def newline_on_alt_enter(event) -> None:
        """Alt+Enter (sent by terminals as Esc,Enter) → insert newline."""
        event.current_buffer.insert_text("\n")

    return kb


def make_prompt_session(
    *,
    history_path: Path | None = None,
    slash_commands: list[tuple[str, str]] | None = None,
) -> PromptSession:
    """Create a PromptSession with multiline input, file history, and fuzzy slash autocomplete.

    Parameters
    ----------
    history_path : Path | None
        Where to persist input history. Defaults to ``~/.loom/cli_history``.
        Falls back to in-memory history if the path is not writable.
    slash_commands : list[(cmd, desc)] | None
        Override the slash command catalog (e.g., for tests). Defaults to
        :data:`SLASH_COMMANDS`.
    """
    # Resolve history location, with graceful fallback to in-memory.
    history: FileHistory | InMemoryHistory
    path = history_path if history_path is not None else _HISTORY_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(path))
    except OSError:
        history = InMemoryHistory()

    completer = FuzzyCompleter(
        SlashCompleter(slash_commands),
        # Empty pattern produces no fuzzy noise; only fuzzy-match real input.
        enable_fuzzy=True,
    )

    return PromptSession(
        history=history,
        completer=completer,
        complete_while_typing=True,
        auto_suggest=AutoSuggestFromHistory(),
        style=_PT_STYLE,
        key_bindings=_build_key_bindings(),
        # multiline=True deferred: prompt_toolkit's multi-line cursor
        # bookkeeping leaves terminal state that interacts badly with
        # Rich's raw `\r\033[K` clear_line on streaming output (CJK
        # boundary truncation). Revisit alongside Rich render rewrite
        # in PR-D.
        multiline=False,
        mouse_support=False,
    )


# ---------------------------------------------------------------------------
# select_prompt — arrow-key inline selection (PR-A3)
# ---------------------------------------------------------------------------
#
# Replaces single-key ``input("y/n: ")`` confirms with an inline widget
# that supports up/down arrow navigation, Enter to confirm, single-key
# shortcuts (still typeable for muscle memory), and Esc to cancel.
#
# The widget runs as a short-lived prompt_toolkit Application which
# competes for stdin with the main input loop. Callers must ensure the
# main input loop is paused before invoking — see ``confirm_active``
# coordination in ``platform/cli/main.py``.


@dataclass
class SelectOption:
    """One choice in a select_prompt menu."""
    label: str
    value: Any
    shortcut: str | None = None      # single-letter direct selection
    style: str = ""                   # optional Rich-like style applied to label


_SELECT_STYLE = Style.from_dict(
    {
        "select.title":         PARCHMENT_TEXT,
        "select.body":          PARCHMENT_MUTED,
        "select.subtle":        PARCHMENT_MUTED,
        "select.option":        PARCHMENT_TEXT,
        "select.option.cursor": f"bold {PARCHMENT_ACCENT}",
        "select.shortcut":      PARCHMENT_MUTED,
        "select.footer":        f"{PARCHMENT_MUTED} italic",
        "select.deny":          PARCHMENT_ERROR,
        "select.approve":       PARCHMENT_SUCCESS,
    }
)


async def select_prompt(
    *,
    title: str,
    body: str = "",
    options: list[SelectOption],
    default_index: int = 0,
    cancel_value: Any = None,
    footer_hint: str | None = None,
) -> Any:
    """Show an inline arrow-key selection menu and return the chosen value.

    Parameters
    ----------
    title : str
        Single-line header (e.g. tool name + trust level).
    body : str
        Optional secondary description (truncated args, justification, …).
    options : list[SelectOption]
        Choices in display order. Each option's ``shortcut`` (if set) lets
        the user pick it directly without arrow nav.
    default_index : int
        Cursor starts on this option.
    cancel_value : Any
        Returned when the user presses Esc / Ctrl+C.
    footer_hint : str | None
        Override the default ``↑↓ select  ⏎ confirm  esc cancel`` hint.
    """
    cursor = max(0, min(default_index, len(options) - 1))
    shortcut_index = {
        opt.shortcut.lower(): idx
        for idx, opt in enumerate(options)
        if opt.shortcut
    }

    def _render() -> FormattedText:
        lines: list[tuple[str, str]] = []
        if title:
            lines.append(("class:select.title", f"{title}\n"))
        if body:
            lines.append(("class:select.body", f"{body}\n"))
        if title or body:
            lines.append(("", "\n"))

        for idx, opt in enumerate(options):
            is_cursor = idx == cursor
            arrow = " ▸ " if is_cursor else "   "
            style = "class:select.option.cursor" if is_cursor else "class:select.option"
            label = opt.label
            if opt.shortcut and not is_cursor:
                # Embed shortcut hint as suffix in subtle style
                lines.append((style, f"{arrow}{label}"))
                lines.append(("class:select.shortcut", f"  ({opt.shortcut})"))
                lines.append(("", "\n"))
            else:
                lines.append((style, f"{arrow}{label}"))
                if opt.shortcut and is_cursor:
                    lines.append(("class:select.option.cursor", f"  ({opt.shortcut})"))
                lines.append(("", "\n"))

        lines.append(("", "\n"))
        hint = footer_hint or "↑↓ 選擇  ⏎ 確認  esc 取消"
        if shortcut_index:
            shortcuts = "/".join(s for s in shortcut_index)
            hint += f"  ·  直接按 {shortcuts}"
        lines.append(("class:select.footer", hint))
        return FormattedText(lines)

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("c-p")
    def _up(event):
        nonlocal cursor
        cursor = (cursor - 1) % len(options)
        event.app.invalidate()

    @kb.add("down")
    @kb.add("c-n")
    def _down(event):
        nonlocal cursor
        cursor = (cursor + 1) % len(options)
        event.app.invalidate()

    @kb.add("enter")
    def _confirm(event):
        event.app.exit(result=options[cursor].value)

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    def _cancel(event):
        event.app.exit(result=cancel_value)

    # Bind each shortcut key directly
    for shortcut, idx in shortcut_index.items():
        @kb.add(shortcut)
        def _shortcut(event, _idx=idx):  # default-arg captures idx per loop
            event.app.exit(result=options[_idx].value)

    body_window = Window(
        content=FormattedTextControl(_render, focusable=True),
        height=Dimension(min=len(options) + (3 if title or body else 1)),
        wrap_lines=True,
    )

    app: Application = Application(
        layout=Layout(HSplit([body_window])),
        key_bindings=kb,
        style=_SELECT_STYLE,
        full_screen=False,
        mouse_support=False,
    )

    return await app.run_async()


# ---------------------------------------------------------------------------
# Rich display helpers
# ---------------------------------------------------------------------------


def render_header(model: str, db: str) -> Panel:
    """Top-of-session banner — legacy 2-line greeting.

    PR-D4 introduced :func:`render_welcome_signature` which consolidates
    this header + the MemoryIndex Panel into a 3-line mini signature.
    Kept for tests / non-CLI callers that still construct it.
    """
    return Panel(
        Text.from_markup(
            f"[bold loom.accent]Loom[/bold loom.accent]  [loom.muted]v0.3.0[/loom.muted]\n"
            f"model [loom.success]{model}[/loom.success]  |  memory [loom.muted]{db}[/loom.muted]\n"
            f"[loom.muted]Type [bold]exit[/bold] or Ctrl-C to quit  |  "
            f"/personality <name>  |  /compact  |  /help[/loom.muted]"
        ),
        border_style="cyan",
    )


def render_welcome_signature(
    *,
    model: str,
    persona: str | None,
    skill_count: int = 0,
    fact_count: int = 0,
    mcp_count: int = 0,
    episode_count: int = 0,
    relation_count: int = 0,
) -> Text:
    """ASCII signature + stats block for ``loom chat`` startup.

    Replaces the previous render_header Panel + MemoryIndex Panel
    splatter with a compact branded greeting. The full MemoryIndex
    still feeds the LLM's system prompt — this only changes what
    the user sees on startup.

    Format::

           ╱╲╱╲╱╲╱╲╱
          ╱  Loom  ╲       v0.3.x
           ╲╱╲╱╲╱╲╱
        ─────  12 skills · 14k facts · 3 mcp · 47 episodes
              ╲    minimax-m2.7  ·  persona: tarot

    The triple top row is a loose nod to the warp/weft weave that
    gives the project its name; deliberately understated so it
    doesn't dominate the terminal. Stats fields with zero counts
    are silently skipped.
    """
    from loom import __version__

    def _abbrev(n: int) -> str:
        # Strip trailing zero before suffix: 14000 → "14k" not "14.0k"
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "m"
        if n >= 1_000:
            return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
        return str(n)

    stats: list[str] = []
    if skill_count:
        stats.append(f"{_abbrev(skill_count)} skills")
    if fact_count:
        stats.append(f"{_abbrev(fact_count)} facts")
    if mcp_count:
        stats.append(f"{mcp_count} mcp")
    if episode_count:
        stats.append(f"{_abbrev(episode_count)} episodes")
    if relation_count:
        stats.append(f"{_abbrev(relation_count)} relations")
    stats_line = " · ".join(stats) if stats else "fresh session"

    persona_tag = f"  ·  persona: {persona}" if persona else ""

    # Five-line signature: woven mark on top, stats + identity below
    return Text.from_markup(
        "\n"
        "[loom.muted]    ╱╲╱╲╱╲╱╲╱[/loom.muted]\n"
        "[loom.muted]   ╱  [/loom.muted][loom.accent]Loom[/loom.accent]"
        f"[loom.muted]  ╲     v{__version__}[/loom.muted]\n"
        "[loom.muted]    ╲╱╲╱╲╱╲╱[/loom.muted]\n"
        f"[loom.muted] ─────  {stats_line}[/loom.muted]\n"
        f"[loom.muted]      ╲   [/loom.muted][loom.text]{model}[/loom.text]"
        f"[loom.muted]{persona_tag}[/loom.muted]\n"
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
        subtitle_parts.append(f"[loom.muted]persona: {personality}[/loom.muted]")
    subtitle = "  ".join(subtitle_parts)

    if text:
        content = Markdown(text)
        border = "green"
        title = "[bold loom.success]loom[/bold loom.success]"
    else:
        content = Text.from_markup("[loom.muted]thinking…[/loom.muted]" if is_thinking else "")
        border = "dim"
        title = "[loom.muted]loom[/loom.muted]"

    return Panel(
        content,
        title=title,
        subtitle=subtitle,
        border_style=border,
        padding=(0, 1),
    )


# ASCII spinner frames (cp950 safe, Rich-markup safe — no backslash)
_SPINNER_FRAMES = ["-", "~", "|", "+"]

def tool_spinner_line(
    name: str,
    args: dict[str, Any],
    frame_index: int = 0,
    width: int | None = None,
) -> Text:
    """
    Rich Text for a tool-call-in-progress line with ASCII spinner.
    Frame index 0 shows [- ], 1 shows [\\ ], etc.

    When ``width`` is given and the rendered line would overflow it,
    the args body wraps with a hanging indent so continuation lines
    align under the tool name instead of restarting at column 0.
    """
    spinner = _SPINNER_FRAMES[frame_index % len(_SPINNER_FRAMES)]
    args_preview = _format_args(args)
    body_plain = f"{name}{f'({args_preview})' if args_preview else ''}"
    prefix_visual = "  [-] "  # 6 cells; spinner glyph is always 1 wide
    indent = " " * len(prefix_visual)

    out = Text()
    out.append("  [", style="loom.muted")
    out.append(spinner, style="loom.warning")
    out.append("] ", style="loom.muted")

    if width is None:
        # Caller didn't pass width — keep single-line styled form
        out.append(name, style="loom.warning")
        if args_preview:
            out.append(f"({args_preview})")
        return out

    from wcwidth import wcswidth as _ws
    body_cells = _ws(body_plain)
    if body_cells is None or body_cells < 0:
        body_cells = len(body_plain)
    if len(prefix_visual) + body_cells <= width:
        # Fits on one line in display cells (not just code points,
        # so CJK doesn't sneak past and trigger terminal soft-wrap)
        out.append(name, style="loom.warning")
        if args_preview:
            out.append(f"({args_preview})")
        return out

    # Hanging-indent wrap. Prefer breaking at ``, `` (between args)
    # over splitting inside an arg value — the latter splits a
    # ``key="some long value"`` mid-string and looks weird because
    # the closing quote ends up on a different visual line. Only
    # fall back to inner wrap when a single arg is too long for the
    # available width.
    body_width = max(20, width - len(prefix_visual))
    wrapped = _wrap_args_body(body_plain, body_width)

    first = wrapped[0]
    if first.startswith(name):
        out.append(name, style="loom.warning")
        out.append(first[len(name):])
    else:
        # Edge case: name itself longer than body_width — fall back
        # to unstyled split rather than trying to slice a style span
        out.append(first)
    for cont in wrapped[1:]:
        out.append("\n")
        out.append(indent + cont)
    return out


def tool_begin_line(
    name: str, args: dict[str, Any], width: int | None = None
) -> Text:
    """Rich Text for a tool-call-in-progress line (alias for compatibility)."""
    return tool_spinner_line(name, args, 0, width=width)


def tool_running_line(name: str, frame_index: int = 0) -> Text:
    """
    Rich Text for a tool that is currently executing.
    Shows animated spinner without args (args already shown at begin).
    """
    spinner = _SPINNER_FRAMES[frame_index % len(_SPINNER_FRAMES)]
    return Text.from_markup(
        f"  [loom.muted][[/loom.muted][loom.warning]{spinner}[/loom.warning][loom.muted]][/loom.muted] "
        f"[loom.muted]{name} running...[/loom.muted]"
    )


def tool_end_line(
    name: str, success: bool, duration_ms: float, frozen: bool = False
) -> Text:
    """Rich Text for a completed tool-call line.

    Two visual stages share this helper:

    - ``frozen=False`` is the **committed** stage (default): green/red
      accent on the icon, ms count and status word so the freshly-
      finished envelope still draws the eye.
    - ``frozen=True`` is the **frozen** stage: every fragment muted,
      the row sinks into the visual background. Used by the cursor-up
      reblit 3s after ToolEnd when no other content has been printed
      below the row (doc/49 §2 "完成即蒸發").
    """
    if frozen:
        icon_word = "ok" if success else "!!"
        status_word = "done" if success else "failed"
        # Build with Text.append rather than from_markup — bare ``[ok]``
        # in a markup string gets parsed as an (unknown) style tag and
        # silently swallowed, leaving a blank gap where the icon was
        out = Text()
        out.append(
            f"  [{icon_word}] {name}  {duration_ms:.0f}ms  {status_word}",
            style="loom.muted",
        )
        return out
    icon = "[loom.success]ok[/loom.success]" if success else "[loom.error]!![/loom.error]"
    status = "[loom.success]done[/loom.success]" if success else "[loom.error]failed[/loom.error]"
    return Text.from_markup(
        f"  [loom.muted][[/loom.muted]{icon}[loom.muted]][/loom.muted] "
        f"[loom.muted]{name}[/loom.muted]  "
        f"[{('green' if success else 'red')}]{duration_ms:.0f}ms[/{('green' if success else 'red')}]  "
        f"[loom.muted]{status}[/loom.muted]"
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
    return Text.from_markup(f"[bold loom.warning]{streaming_cursor()}[/bold loom.warning]")


def status_bar(
    context_fraction: float,
    input_tokens: int,
    output_tokens: int,
    elapsed_ms: float,
    tool_count: int,
    cache_hit_pct: float = 0.0,
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

    # Extracted into a variable: a conditional expression mid-f-string would
    # bind to adjacent string literals via implicit concat at lex time, eating
    # the segments on either side. See PR #229 review.
    cache_seg = f"cache {cache_hit_pct:.0f}%  |  " if cache_hit_pct > 0 else ""

    return Text.from_markup(
        f"[loom.muted]-[/loom.muted]"
        f"[{ctx_color}]{bar}[/{ctx_color}]"
        f"[loom.muted] context {pct:.1f}%  |  {cache_seg}"
        f"{input_tokens}in / {output_tokens}out  |  "
        f"{elapsed_ms / 1000:.1f}s  |  "
        f"{tool_count} tool{'s' if tool_count != 1 else ''}"
        f"[/loom.muted][loom.muted]-[/loom.muted]"
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


def _wrap_args_body(body: str, width: int) -> list[str]:
    """Wrap a ``name(key=val, key=val, …)`` body for hanging indent.

    Width is measured in **display cells** (wcswidth), not code
    points — otherwise CJK / emoji content undercounts by ~2× and
    the terminal silently soft-wraps without the indent we wanted.

    Greedy packs whole args onto each line, breaking at ``, ``
    boundaries between args (so a ``key="value"`` is never split
    across lines under normal sizing). Only when a single arg's
    rendered form exceeds the available width do we fall back to
    cell-aware char wrapping inside the value.
    """
    from wcwidth import wcswidth as _ws

    def cells(s: str) -> int:
        n = _ws(s)
        return max(0, n) if n is not None else len(s)

    if cells(body) <= width:
        return [body]

    parts = body.split(", ")

    if len(parts) == 1:
        return _wrap_cells(body, width)

    lines: list[str] = []
    current = ""
    for i, p in enumerate(parts):
        sep = "" if i == len(parts) - 1 else ", "
        chunk = p + sep
        if not current:
            current = chunk
            continue
        if cells(current) + cells(chunk) <= width:
            current += chunk
        else:
            lines.append(current)
            current = chunk
    if current:
        lines.append(current)

    final: list[str] = []
    for line in lines:
        if cells(line) <= width:
            final.append(line)
        else:
            final.extend(_wrap_cells(line, width))
    return final


def _wrap_cells(s: str, width: int) -> list[str]:
    """Cell-width-aware char wrap. Prefers ASCII space breaks within
    the current row; falls back to mid-string break when no space is
    available before the row fills (e.g. unbroken CJK runs)."""
    from wcwidth import wcwidth as _wcw

    def cw(ch: str) -> int:
        n = _wcw(ch)
        return max(0, n) if n is not None else 1

    lines: list[str] = []
    cur = ""
    cur_w = 0
    last_space = -1
    for ch in s:
        w = cw(ch)
        if cur_w + w > width and cur:
            if last_space > 0:
                lines.append(cur[:last_space].rstrip())
                rest = cur[last_space:].lstrip()
                cur = rest + ch
                cur_w = sum(cw(c) for c in cur)
                last_space = cur.rfind(" ") if " " in cur else -1
            else:
                lines.append(cur)
                cur = ch
                cur_w = w
                last_space = -1
            continue
        if ch == " ":
            last_space = len(cur)
        cur += ch
        cur_w += w
    if cur:
        lines.append(cur)
    return lines


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
