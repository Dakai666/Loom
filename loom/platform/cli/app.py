"""
LoomApp — persistent prompt_toolkit Application for the CLI chat surface.

PR-D1 (#236). Replaces the per-iteration ``PromptSession.prompt_async``
+ ``patch_stdout`` + three-event stdin coordinator architecture with a
single long-running Application that owns the bottom region of the
terminal (input area + footer + transient confirm/pause overlays).

Design
------
- The Application's layout is ``HSplit([mode_container, footer])``
  where ``mode_container`` is a ``ConditionalContainer`` switch over a
  ``mode`` flag — INPUT / CONFIRM / PAUSE / REDIRECT
- ``mode`` is a simple string held in a single-element list so closures
  see updates. Switching modes calls ``app.invalidate()`` to redraw
- ``request_confirm`` / ``request_pause`` / ``request_redirect_text``
  flip the mode, await a Future, and restore mode in a ``finally``.
  The transient widget rendering happens entirely inside the layout —
  it never reaches the terminal scrollback (the "用過即焚" property
  required by #236)
- Streaming output goes through ``app.run_in_terminal`` so it lands in
  the natural scrollback above the persistent bottom area; no
  ``patch_stdout`` plumbing required

Constraints honoured
--------------------
- Input area is multiline-capable (Alt+Enter newline, Enter submit) —
  this finally re-enables the multi-line input that PR-A had to disable
- Slash commands and abort-on-submit semantics from PR-A preserved
- Confirm widget renders within the layout, not via a nested
  Application — so the "Application is not running" race that PR-A
  worked around with three-event coordination simply cannot occur
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Literal

from prompt_toolkit.application import Application
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import FuzzyCompleter
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style

from loom.platform.cli.theme import (
    PARCHMENT_ACCENT,
    PARCHMENT_BG,
    PARCHMENT_BORDER,
    PARCHMENT_ERROR,
    PARCHMENT_MUTED,
    PARCHMENT_SUCCESS,
    PARCHMENT_SURFACE,
    PARCHMENT_TEXT,
    PARCHMENT_WARNING,
)
from loom.platform.cli.ui import SLASH_COMMANDS, SlashCompleter


Mode = Literal["input", "confirm", "pause", "redirect"]


# ---------------------------------------------------------------------------
# Footer state
# ---------------------------------------------------------------------------


@dataclass
class _ActiveEnvelope:
    """A tool envelope currently in flight — for footer summary."""
    name: str
    started_monotonic: float


@dataclass
class FooterState:
    """Mutable state read by ``render_footer`` on every redraw.

    Fields are written from event handlers (e.g. envelope start/end,
    turn complete updates token_pct) and read inside the render
    callback. ``app.invalidate()`` after writes triggers redraw.
    """

    model: str = ""
    persona: str | None = None
    # Context window utilisation — surfaces in footer when >60%
    token_pct: float = 0.0
    # Tools currently running. footer shows the most recent one's
    # name + elapsed; if multiple, prefix with ``Nx`` count
    active_envelopes: list[_ActiveEnvelope] = field(default_factory=list)
    # Compaction in progress — when True, footer hides everything
    # else and shows ``⚡ 壓縮中…`` so the long pause doesn't look
    # like a hang
    compacting: bool = False
    # Loom is thinking (LLM call dispatched, no stream output yet).
    # Surfaces as a soft animated indicator above the input separator
    # so the user knows their input is being chewed on
    thinking: bool = False
    # Number of active scope grants and seconds until the nearest one
    # expires. Refreshed at turn boundaries (per doc/49 decision —
    # don't tick every second). 0 grants → both fields 0
    grants_active: int = 0
    grants_next_expiry_secs: float = 0.0
    # Last-turn stats. Kept out of scrollback (PR-A printed these
    # inline as the "context X% | cache Y% | A in / B out | Cs | N
    # tools" status_bar; that's noise the user doesn't need to keep
    # scrolling past). All four cleared between turns
    last_turn_cache_hit: float | None = None
    last_turn_input_tokens: int | None = None
    last_turn_output_tokens: int | None = None
    last_turn_elapsed_s: float | None = None
    last_turn_tool_count: int | None = None


# ---------------------------------------------------------------------------
# Confirm / Pause widget state
# ---------------------------------------------------------------------------


@dataclass
class _ConfirmState:
    title: str
    body: str
    options: list[tuple[str, Any, str | None]]  # (label, value, shortcut)
    cursor: int = 0
    future: asyncio.Future | None = None


@dataclass
class _PauseState:
    title: str
    options: list[tuple[str, Any, str | None]]
    cursor: int = 0
    future: asyncio.Future | None = None


@dataclass
class _TaskListState:
    """Snapshot of the agent's TaskList for the floating panel.

    Replaced wholesale on every ``task_write``. Empty ``todos`` means
    hide the panel entirely.
    """
    todos: list[dict] = field(default_factory=list)
    # Auto-collapse when every todo is completed — render a single
    # ``✓ N/N done`` line instead of the full panel. Until expand-toggle
    # is wired (follow-up), the collapsed view is one-shot
    collapsed: bool = False


# ---------------------------------------------------------------------------
# Style — extends prompt_toolkit's class-based style with our palette
# ---------------------------------------------------------------------------

_APP_STYLE = Style.from_dict(
    {
        # Input area — no prompt label; the buffer text is the input
        "input.text":            PARCHMENT_TEXT,
        # Footer — transparent background (follows terminal). User
        # feedback after PR-D1 first run: explicit bg felt obtrusive
        # against varied terminal themes
        "footer":                PARCHMENT_MUTED,
        "footer.brand":          f"{PARCHMENT_ACCENT} bold",
        "footer.budget.ok":      PARCHMENT_MUTED,
        "footer.budget.warn":    PARCHMENT_WARNING,
        "footer.budget.high":    PARCHMENT_ERROR,
        "footer.stats":          PARCHMENT_MUTED,
        "footer.envelope":       PARCHMENT_ACCENT,
        "footer.compaction":     PARCHMENT_WARNING,
        "footer.grant":          PARCHMENT_MUTED,
        # Thinking indicator above the input separator
        "thinking":              PARCHMENT_MUTED,
        "thinking.dot":          PARCHMENT_ACCENT,
        # Floating TaskList panel
        "tasklist.frame":        PARCHMENT_BORDER,
        "tasklist.title":        f"{PARCHMENT_ACCENT} bold",
        "tasklist.done":         PARCHMENT_SUCCESS,
        "tasklist.active":       PARCHMENT_ACCENT,
        "tasklist.pending":      PARCHMENT_MUTED,
        "tasklist.collapsed":    PARCHMENT_SUCCESS,
        # Confirm / Pause widget — also no bg, blend with terminal
        "widget.title":          PARCHMENT_TEXT,
        "widget.body":           PARCHMENT_MUTED,
        "widget.option":         PARCHMENT_TEXT,
        "widget.option.cursor":  f"bold {PARCHMENT_ACCENT}",
        "widget.shortcut":       PARCHMENT_MUTED,
        "widget.footer":         f"italic {PARCHMENT_MUTED}",
        # Auto-suggestion ghost text
        "auto-suggestion":       PARCHMENT_BORDER,
        # Completion menu — keep subtle bg; this is dropdown UI not
        # ambient surface. Dropdowns benefit from contrast
        "completion-menu":                         f"bg:{PARCHMENT_SURFACE} {PARCHMENT_TEXT}",
        "completion-menu.completion":              f"bg:{PARCHMENT_SURFACE} {PARCHMENT_TEXT}",
        "completion-menu.completion.current":      f"bg:{PARCHMENT_ACCENT} {PARCHMENT_BG} bold",
        "completion-menu.meta.completion":         f"bg:{PARCHMENT_SURFACE} {PARCHMENT_MUTED}",
        "completion-menu.meta.completion.current": f"bg:{PARCHMENT_ACCENT} {PARCHMENT_BG}",
    }
)


# ---------------------------------------------------------------------------
# LoomApp
# ---------------------------------------------------------------------------


class LoomApp:
    """Owns the persistent prompt_toolkit Application backing ``loom chat``.

    Construct with :meth:`build`, then call :meth:`run` to start the
    main loop. The caller (``_chat`` in main.py) registers callbacks
    for input submission and runs the streaming-turn loop concurrently.
    """

    def __init__(
        self,
        *,
        history: History,
        on_submit: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._mode: list[Mode] = ["input"]
        self._on_submit = on_submit

        # Buffers
        self._input_buffer = Buffer(
            multiline=True,
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            completer=FuzzyCompleter(SlashCompleter(), enable_fuzzy=True),
            complete_while_typing=True,
        )
        self._redirect_buffer = Buffer(multiline=False)

        # Widget state
        self._confirm_state: _ConfirmState | None = None
        self._pause_state: _PauseState | None = None
        self._redirect_future: asyncio.Future | None = None
        self._tasklist_state = _TaskListState()

        # Footer state
        self.footer = FooterState()

        # Build layout + key bindings + Application
        self._app = self._build_application()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def application(self) -> Application:
        return self._app

    @property
    def mode(self) -> Mode:
        return self._mode[0]

    async def run(self) -> None:
        """Run the application until the user quits."""
        await self._app.run_async()

    def invalidate(self) -> None:
        self._app.invalidate()

    async def print_above(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` above the application's bottom region.

        Use this for streaming output from a turn — Rich console.print,
        Panel rendering, etc. The callback runs synchronously while the
        Application's bottom area is briefly suspended, then redrawn
        below the new output. Result: streaming text flows into the
        natural terminal scrollback while the input + footer stay
        anchored at the bottom.
        """
        await self._app.run_in_terminal(callback)

    async def request_confirm(
        self,
        *,
        title: str,
        body: str,
        options: list[tuple[str, Any, str | None]],
        default_index: int = 0,
        cancel_value: Any = None,
    ) -> Any:
        """Switch to confirm mode and await the user's selection.

        Returns whatever ``options[i][1]`` is for the chosen index, or
        ``cancel_value`` if Esc/Ctrl+C is pressed.
        """
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._confirm_state = _ConfirmState(
            title=title,
            body=body,
            options=options,
            cursor=max(0, min(default_index, len(options) - 1)),
            future=future,
        )
        # Stash cancel_value on the future so the Esc handler can use it
        future._loom_cancel_value = cancel_value  # type: ignore[attr-defined]
        self._mode[0] = "confirm"
        self._app.invalidate()
        try:
            return await future
        finally:
            self._confirm_state = None
            self._mode[0] = "input"
            self._app.invalidate()

    async def request_pause(
        self,
        *,
        title: str,
        options: list[tuple[str, Any, str | None]],
        default_index: int = 0,
        cancel_value: Any = None,
    ) -> Any:
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pause_state = _PauseState(
            title=title,
            options=options,
            cursor=max(0, min(default_index, len(options) - 1)),
            future=future,
        )
        future._loom_cancel_value = cancel_value  # type: ignore[attr-defined]
        self._mode[0] = "pause"
        self._app.invalidate()
        try:
            return await future
        finally:
            self._pause_state = None
            self._mode[0] = "input"
            self._app.invalidate()

    def update_tasklist(self, todos: list[dict]) -> None:
        """Replace the floating task panel snapshot.

        Wired from ``TaskListManager.on_change`` so every ``task_write``
        propagates here. Empty list hides the panel. When every todo is
        ``completed`` we auto-collapse to a one-line summary so a finished
        list doesn't dominate the bottom region.
        """
        self._tasklist_state.todos = list(todos or [])
        if self._tasklist_state.todos and all(
            (t.get("status") or "").lower() == "completed"
            for t in self._tasklist_state.todos
        ):
            self._tasklist_state.collapsed = True
        else:
            self._tasklist_state.collapsed = False
        self._app.invalidate()

    async def request_redirect_text(self) -> str:
        """Switch to redirect mode for free-form text entry (HITL redirect)."""
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._redirect_future = future
        self._mode[0] = "redirect"
        self._app.invalidate()
        try:
            return await future
        finally:
            self._redirect_future = None
            self._redirect_buffer.text = ""
            self._mode[0] = "input"
            self._app.invalidate()

    # ------------------------------------------------------------------
    # Layout builders
    # ------------------------------------------------------------------

    def _build_application(self) -> Application:
        # Mode-aware filters
        is_input = Condition(lambda: self._mode[0] == "input")
        is_confirm = Condition(lambda: self._mode[0] == "confirm")
        is_pause = Condition(lambda: self._mode[0] == "pause")
        is_redirect = Condition(lambda: self._mode[0] == "redirect")

        # Input area — single Window, no separate prompt label.
        # Feedback after PR-D1 first run: the "you ›" label was visually
        # noisy and a separate Window for it caused the bottom area to
        # re-flow during streaming, sometimes attaching the label to
        # the end of the agent's last sentence. Submitted text echoes
        # to scrollback (see _on_submit in main.py) so the input area
        # doesn't need its own label
        input_window = ConditionalContainer(
            Window(
                content=BufferControl(buffer=self._input_buffer),
                wrap_lines=True,
                height=Dimension(min=1, max=10),
                style="class:input.text",
            ),
            filter=is_input,
        )

        # Confirm widget
        confirm_window = ConditionalContainer(
            Window(
                content=FormattedTextControl(self._render_confirm, focusable=True),
                height=Dimension(min=4, max=15),
                wrap_lines=True,
            ),
            filter=is_confirm,
        )

        # Pause widget
        pause_window = ConditionalContainer(
            Window(
                content=FormattedTextControl(self._render_pause, focusable=True),
                height=Dimension(min=4, max=10),
                wrap_lines=True,
            ),
            filter=is_pause,
        )

        # Redirect text input — same simplification: single Window
        redirect_window = ConditionalContainer(
            Window(
                content=BufferControl(buffer=self._redirect_buffer),
                height=1,
                style="class:input.text",
            ),
            filter=is_redirect,
        )

        # Footer — always visible
        footer_window = Window(
            content=FormattedTextControl(self._render_footer),
            height=1,
            style="class:footer",
        )

        # Separators above + below the input region — make the visual
        # boundary between scrollback / input / footer explicit. Filled
        # with a horizontal rule character; styled in the muted-border
        # palette. Two separate Window instances so each can sit at its
        # correct position in the HSplit
        separator_top = Window(
            char="─",
            height=1,
            style=f"fg:{PARCHMENT_BORDER}",
        )
        separator_bottom = Window(
            char="─",
            height=1,
            style=f"fg:{PARCHMENT_BORDER}",
        )

        # Floating TaskList panel — sits above the thinking indicator
        # (highest in the bottom stack) so it's the closest visual
        # neighbour to scrollback. Hidden entirely when no list is
        # active. Height is dynamic; ConditionalContainer collapses to
        # zero when the render emits empty FormattedText
        tasklist_window = ConditionalContainer(
            Window(
                content=FormattedTextControl(self._render_tasklist),
                height=Dimension(min=1, max=12),
                wrap_lines=False,
            ),
            filter=Condition(lambda: bool(self._tasklist_state.todos)),
        )

        # Thinking indicator — appears as its own line *above* the
        # separator (so visually attached to the input region), only
        # while ``footer.thinking`` is True. Mirrors Claude Code-style
        # "● thinking…" status above the prompt rather than tucking
        # it into the footer
        thinking_window = ConditionalContainer(
            Window(
                content=FormattedTextControl(self._render_thinking),
                height=1,
            ),
            filter=Condition(lambda: self.footer.thinking),
        )

        layout = Layout(
            HSplit([
                tasklist_window,
                thinking_window,
                separator_top,
                input_window,
                confirm_window,
                pause_window,
                redirect_window,
                separator_bottom,
                footer_window,
            ]),
            focused_element=self._input_buffer,
        )

        return Application(
            layout=layout,
            key_bindings=self._build_key_bindings(),
            style=_APP_STYLE,
            full_screen=False,
            mouse_support=False,
            erase_when_done=False,
        )

    # ------------------------------------------------------------------
    # Render callbacks
    # ------------------------------------------------------------------

    def _render_footer(self) -> FormattedText:
        parts: list[tuple[str, str]] = []

        # Far left: bold Loom brand mark — anchors the line as Loom's
        # identity. Persona is omitted (welcome screen already announces
        # it; footer real estate goes to live state).
        parts.append(("class:footer.brand", " Loom "))

        if self.footer.model:
            parts.append(("class:footer", f"  {self.footer.model} "))

        s = self.footer

        # If compacting: replace the middle area with the spinner
        # message so the user doesn't think the agent has hung
        if s.compacting:
            parts.append(("class:footer", "  "))
            parts.append(("class:footer.compaction", "⚡ 壓縮中…"))
            return FormattedText(parts)

        # Context % — always visible. Colour ladder: muted under 60%,
        # ochre 60-80%, warning 80-95%, error >95%
        token = (
            "footer.budget.high" if s.token_pct > 95
            else "footer.budget.warn" if s.token_pct > 80
            else "footer.budget.ok"
        )
        parts.append(("class:footer", "  "))
        parts.append((f"class:{token}", f"context {s.token_pct:.1f}%"))

        # Active scope grants — show 🔑 N · M:SS for the nearest
        # expiring lease. Refreshed at turn boundaries (no per-second
        # ticking; doc/49 decision)
        if s.grants_active > 0:
            ttl = s.grants_next_expiry_secs
            if ttl > 0:
                m, sec = divmod(int(ttl), 60)
                ttl_label = f"{m}:{sec:02d}" if m < 60 else f"{m // 60}h{m % 60}m"
            else:
                ttl_label = "∞"  # session-scoped grant, no expiry
            parts.append(("class:footer", "  "))
            parts.append(
                ("class:footer.grant",
                 f"🔑 {s.grants_active}·{ttl_label}")
            )

        # Active envelopes — show the most recent one with elapsed
        # time. When >1 in flight, prefix with count: ``3× ▸ ...``.
        # Updates as ToolBegin / ToolEnd fire (see main.py wiring)
        envs = s.active_envelopes
        if envs:
            import time as _t
            latest = envs[-1]
            elapsed = max(0.0, _t.monotonic() - latest.started_monotonic)
            label = f"{len(envs)}× " if len(envs) > 1 else ""
            label += f"▸ {latest.name} · {elapsed:.1f}s"
            parts.append(("class:footer", "  "))
            parts.append(("class:footer.envelope", label))
        # Thinking indicator no longer rendered here — moved to its
        # own window above the input separator (see _render_thinking)

        # Last-turn stats — only show when no tool is currently in
        # flight (otherwise the active envelope already tells the
        # user "we're busy")
        if not envs:
            stats: list[str] = []
            if s.last_turn_cache_hit is not None:
                stats.append(f"cache {s.last_turn_cache_hit:.0f}%")
            if (s.last_turn_input_tokens is not None
                    and s.last_turn_output_tokens is not None):
                stats.append(
                    f"{s.last_turn_input_tokens}in / {s.last_turn_output_tokens}out"
                )
            if s.last_turn_elapsed_s is not None:
                stats.append(f"{s.last_turn_elapsed_s:.1f}s")
            if s.last_turn_tool_count is not None:
                n = s.last_turn_tool_count
                stats.append(f"{n} tool{'s' if n != 1 else ''}")
            if stats:
                parts.append(("class:footer", "  "))
                parts.append(("class:footer.stats", " · ".join(stats)))

        return FormattedText(parts)

    def _render_thinking(self) -> FormattedText:
        """Animated thinking indicator above the input separator.

        Style: ``● Loom is thinking···`` with the dots cycling on the
        ticker invalidate. Bullet glyph in accent gold so it reads as
        a status pip; the rest in muted parchment so the line stays
        quiet relative to streaming output.
        """
        import time as _t
        phase = int(_t.monotonic() * 2) % 4
        dots = "·" * phase + " " * (3 - phase)
        return FormattedText([
            ("class:thinking.dot", " ● "),
            ("class:thinking", f"Loom is thinking{dots}"),
        ])

    def _render_tasklist(self) -> FormattedText:
        """Floating TaskList panel — agent's todo board (PR-E, #236).

        Hidden when no list is active. When everything is completed,
        collapses to a single ``✓ N/N done`` line so the finished list
        doesn't dominate the bottom region. Otherwise renders a bordered
        block with one line per todo: ✓ done, ▸ in-progress, ○ pending.
        """
        todos = self._tasklist_state.todos
        if not todos:
            return FormattedText([])

        total = len(todos)
        done = sum(
            1 for t in todos
            if (t.get("status") or "").lower() == "completed"
        )

        if self._tasklist_state.collapsed:
            return FormattedText([
                ("class:tasklist.collapsed", f" ✓ {done}/{total} done\n"),
            ])

        parts: list[tuple[str, str]] = []
        title = f" 📋 task list  {done}/{total} "
        # Top border with embedded title
        bar = "─" * max(4, 48 - len(title))
        parts.append(("class:tasklist.frame", "╭─"))
        parts.append(("class:tasklist.title", title))
        parts.append(("class:tasklist.frame", bar + "╮\n"))

        for t in todos:
            status = (t.get("status") or "pending").lower()
            content = (t.get("content") or "").strip()
            # Truncate to fit a reasonable width; full content is in
            # the agent's own state, the panel is just a glance
            if len(content) > 56:
                content = content[:55] + "…"
            if status == "completed":
                glyph, cls = "✓", "class:tasklist.done"
            elif status == "in_progress":
                glyph, cls = "▸", "class:tasklist.active"
            else:
                glyph, cls = "○", "class:tasklist.pending"
            parts.append(("class:tasklist.frame", "│ "))
            parts.append((cls, f"{glyph} {content}"))
            parts.append(("class:tasklist.frame", "\n"))

        parts.append(("class:tasklist.frame",
                      "╰" + "─" * (len(title) + len(bar) + 2) + "╯\n"))
        return FormattedText(parts)

    def _render_confirm(self) -> FormattedText:
        if self._confirm_state is None:
            return FormattedText([])
        state = self._confirm_state
        parts: list[tuple[str, str]] = []
        parts.append(("class:widget.title", f"{state.title}\n"))
        if state.body:
            parts.append(("class:widget.body", f"{state.body}\n"))
        parts.append(("", "\n"))
        for idx, (label, _value, shortcut) in enumerate(state.options):
            arrow = " ▸ " if idx == state.cursor else "   "
            style = (
                "class:widget.option.cursor" if idx == state.cursor
                else "class:widget.option"
            )
            parts.append((style, f"{arrow}{label}"))
            if shortcut:
                parts.append(("class:widget.shortcut", f"  ({shortcut})"))
            parts.append(("", "\n"))
        parts.append(("", "\n"))
        parts.append(
            (
                "class:widget.footer",
                "↑↓ 選擇  ⏎ 確認  esc 取消",
            )
        )
        return FormattedText(parts)

    def _render_pause(self) -> FormattedText:
        if self._pause_state is None:
            return FormattedText([])
        state = self._pause_state
        parts: list[tuple[str, str]] = []
        parts.append(("class:widget.title", f"{state.title}\n"))
        parts.append(("", "\n"))
        for idx, (label, _value, shortcut) in enumerate(state.options):
            arrow = " ▸ " if idx == state.cursor else "   "
            style = (
                "class:widget.option.cursor" if idx == state.cursor
                else "class:widget.option"
            )
            parts.append((style, f"{arrow}{label}"))
            if shortcut:
                parts.append(("class:widget.shortcut", f"  ({shortcut})"))
            parts.append(("", "\n"))
        parts.append(("", "\n"))
        parts.append(
            ("class:widget.footer", "↑↓ 選擇  ⏎ 確認  esc 取消")
        )
        return FormattedText(parts)

    # ------------------------------------------------------------------
    # Key bindings
    # ------------------------------------------------------------------

    def _build_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        # ── Input mode ────────────────────────────────────────────────
        @kb.add("enter", filter=Condition(lambda: self._mode[0] == "input"))
        def _input_submit(event):
            text = self._input_buffer.text
            if not text.strip():
                return
            # Reset buffer immediately so the user sees their input cleared
            self._input_buffer.reset()
            # Schedule the submission callback
            if self._on_submit is not None:
                asyncio.create_task(self._on_submit(text))

        @kb.add("escape", "enter",
                filter=Condition(lambda: self._mode[0] == "input"))
        @kb.add("c-j",
                filter=Condition(lambda: self._mode[0] == "input"))
        def _input_newline(event):
            """Insert newline.

            Two bindings because terminals are inconsistent:
              - ``Esc, Enter`` is the canonical prompt_toolkit form for
                Alt+Enter, requires "Use Option as Meta" enabled in
                Terminal.app / iTerm2 (off by default on macOS — the
                first PR-D1 test caught this)
              - ``Ctrl+J`` sends a literal newline byte across all
                terminals, no profile setting required
            """
            self._input_buffer.insert_text("\n")

        @kb.add("c-d",
                filter=Condition(lambda: self._mode[0] == "input"))
        def _input_eof(event):
            if not self._input_buffer.text:
                event.app.exit()

        @kb.add("c-c",
                filter=Condition(lambda: self._mode[0] == "input"))
        def _input_interrupt(event):
            if self._input_buffer.text:
                self._input_buffer.reset()
            else:
                event.app.exit()

        @kb.add("c-l")
        def _clear_screen(event):
            event.app.renderer.clear()

        # ── Confirm mode ──────────────────────────────────────────────
        is_confirm = Condition(lambda: self._mode[0] == "confirm")

        @kb.add("up", filter=is_confirm)
        @kb.add("c-p", filter=is_confirm)
        def _confirm_up(event):
            if self._confirm_state:
                n = len(self._confirm_state.options)
                self._confirm_state.cursor = (self._confirm_state.cursor - 1) % n
                event.app.invalidate()

        @kb.add("down", filter=is_confirm)
        @kb.add("c-n", filter=is_confirm)
        def _confirm_down(event):
            if self._confirm_state:
                n = len(self._confirm_state.options)
                self._confirm_state.cursor = (self._confirm_state.cursor + 1) % n
                event.app.invalidate()

        @kb.add("enter", filter=is_confirm)
        def _confirm_pick(event):
            state = self._confirm_state
            if state and state.future and not state.future.done():
                _label, value, _shortcut = state.options[state.cursor]
                state.future.set_result(value)

        @kb.add("escape", eager=True, filter=is_confirm)
        @kb.add("c-c", filter=is_confirm)
        def _confirm_cancel(event):
            state = self._confirm_state
            if state and state.future and not state.future.done():
                cancel_value = getattr(state.future, "_loom_cancel_value", None)
                state.future.set_result(cancel_value)

        # Confirm-mode shortcuts: any single letter matches an option whose
        # shortcut == that letter. We bind a generic letter handler that
        # reads ``event.data`` (the typed character) and matches against
        # the dynamic options list.
        @kb.add("<any>", filter=is_confirm)
        def _confirm_shortcut(event):
            state = self._confirm_state
            if not state or not state.future or state.future.done():
                return
            pressed = (event.data or "").lower()
            if not pressed or len(pressed) != 1:
                return
            for _label, value, sc in state.options:
                if sc and sc.lower() == pressed:
                    state.future.set_result(value)
                    return

        # ── Pause mode (HITL) ─────────────────────────────────────────
        is_pause = Condition(lambda: self._mode[0] == "pause")

        @kb.add("up", filter=is_pause)
        @kb.add("c-p", filter=is_pause)
        def _pause_up(event):
            if self._pause_state:
                n = len(self._pause_state.options)
                self._pause_state.cursor = (self._pause_state.cursor - 1) % n
                event.app.invalidate()

        @kb.add("down", filter=is_pause)
        @kb.add("c-n", filter=is_pause)
        def _pause_down(event):
            if self._pause_state:
                n = len(self._pause_state.options)
                self._pause_state.cursor = (self._pause_state.cursor + 1) % n
                event.app.invalidate()

        @kb.add("enter", filter=is_pause)
        def _pause_pick(event):
            state = self._pause_state
            if state and state.future and not state.future.done():
                _label, value, _shortcut = state.options[state.cursor]
                state.future.set_result(value)

        @kb.add("escape", eager=True, filter=is_pause)
        @kb.add("c-c", filter=is_pause)
        def _pause_cancel(event):
            state = self._pause_state
            if state and state.future and not state.future.done():
                cancel_value = getattr(state.future, "_loom_cancel_value", None)
                state.future.set_result(cancel_value)

        @kb.add("<any>", filter=is_pause)
        def _pause_shortcut(event):
            state = self._pause_state
            if not state or not state.future or state.future.done():
                return
            pressed = (event.data or "").lower()
            if not pressed or len(pressed) != 1:
                return
            for _label, value, sc in state.options:
                if sc and sc.lower() == pressed:
                    state.future.set_result(value)
                    return

        # ── Redirect mode (HITL text input) ───────────────────────────
        is_redirect = Condition(lambda: self._mode[0] == "redirect")

        @kb.add("enter", filter=is_redirect)
        def _redirect_submit(event):
            future = self._redirect_future
            if future and not future.done():
                future.set_result(self._redirect_buffer.text)

        @kb.add("escape", eager=True, filter=is_redirect)
        @kb.add("c-c", filter=is_redirect)
        def _redirect_cancel(event):
            future = self._redirect_future
            if future and not future.done():
                future.set_result("")

        return kb


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_loom_app(
    *,
    history_path: "Path | None" = None,
    on_submit: Callable[[str], Awaitable[None]] | None = None,
) -> LoomApp:
    """Construct a :class:`LoomApp` with sensible defaults.

    Parameters
    ----------
    history_path : Path | None
        Where to persist input history. Defaults to ``~/.loom/cli_history``.
    on_submit : async callable, optional
        Called when the user submits text (presses Enter in INPUT mode).
        The string passed has not been stripped — handler decides.
    """
    from pathlib import Path

    history: History
    path = history_path or Path.home() / ".loom" / "cli_history"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(path))
    except OSError:
        history = InMemoryHistory()

    return LoomApp(history=history, on_submit=on_submit)


__all__ = ["LoomApp", "build_loom_app", "FooterState", "Mode"]
