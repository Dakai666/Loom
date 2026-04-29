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
class FooterState:
    """Mutable state read by ``render_footer`` on every redraw.

    Fields are written from event handlers (e.g. envelope start/end,
    turn complete updates token_pct) and read inside the render
    callback. ``app.invalidate()`` after writes triggers redraw.
    """

    model: str = ""
    persona: str | None = None
    token_pct: float = 0.0


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


# ---------------------------------------------------------------------------
# Style — extends prompt_toolkit's class-based style with our palette
# ---------------------------------------------------------------------------

_APP_STYLE = Style.from_dict(
    {
        # Input area
        "input.prompt":          f"bold {PARCHMENT_ACCENT}",
        "input.text":            PARCHMENT_TEXT,
        # Footer — minimal in D1; D4 expands
        "footer":                f"bg:{PARCHMENT_SURFACE} {PARCHMENT_MUTED}",
        "footer.brand":          f"bg:{PARCHMENT_SURFACE} {PARCHMENT_ACCENT}",
        "footer.budget.ok":      f"bg:{PARCHMENT_SURFACE} {PARCHMENT_MUTED}",
        "footer.budget.warn":    f"bg:{PARCHMENT_SURFACE} {PARCHMENT_WARNING}",
        "footer.budget.high":    f"bg:{PARCHMENT_SURFACE} {PARCHMENT_ERROR}",
        # Confirm / Pause widget
        "widget.title":          PARCHMENT_TEXT,
        "widget.body":           PARCHMENT_MUTED,
        "widget.option":         PARCHMENT_TEXT,
        "widget.option.cursor":  f"bold {PARCHMENT_ACCENT}",
        "widget.shortcut":       PARCHMENT_MUTED,
        "widget.footer":         f"italic {PARCHMENT_MUTED}",
        # Auto-suggestion ghost text
        "auto-suggestion":       PARCHMENT_BORDER,
        # Completion menu
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

        # Input area — always height ≥ 1, expands as user types up to ~10
        input_window = ConditionalContainer(
            HSplit([
                Window(
                    content=FormattedTextControl(
                        lambda: FormattedText([("class:input.prompt", "you › ")]),
                        focusable=False,
                    ),
                    height=1,
                    dont_extend_height=True,
                ),
                Window(
                    content=BufferControl(buffer=self._input_buffer),
                    wrap_lines=True,
                    height=Dimension(min=1, max=10),
                    style="class:input.text",
                ),
            ]),
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

        # Redirect text input
        redirect_window = ConditionalContainer(
            HSplit([
                Window(
                    content=FormattedTextControl(
                        lambda: FormattedText([("class:input.prompt", "redirect › ")]),
                        focusable=False,
                    ),
                    height=1,
                    dont_extend_height=True,
                ),
                Window(
                    content=BufferControl(buffer=self._redirect_buffer),
                    height=1,
                    style="class:input.text",
                ),
            ]),
            filter=is_redirect,
        )

        # Footer — always visible
        footer_window = Window(
            content=FormattedTextControl(self._render_footer),
            height=1,
            style="class:footer",
        )

        layout = Layout(
            HSplit([
                input_window,
                confirm_window,
                pause_window,
                redirect_window,
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

        # Left edge: model · persona
        left = self.footer.model
        if self.footer.persona:
            left = f"{self.footer.persona} · {left}"
        if left:
            parts.append(("class:footer", f" {left} "))

        # Centre: token budget when >60%
        pct = self.footer.token_pct
        if pct > 60:
            token = (
                "footer.budget.high" if pct > 95
                else "footer.budget.warn" if pct > 80
                else "footer.budget.ok"
            )
            parts.append(("class:footer", "  "))
            parts.append((f"class:{token}", f"⚡ tok {pct:.0f}%"))

        # Right edge: ▎ Loom brand mark — pad with spaces to push it
        # right. We don't know terminal width here so rely on
        # FormattedTextControl's natural alignment.
        # (D4 will refine layout; D1 keeps it simple)
        parts.append(("class:footer", "  "))
        parts.append(("class:footer.brand", "▎ Loom "))

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
        def _input_newline(event):
            """Alt+Enter (Esc,Enter on most terminals) → insert newline."""
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
