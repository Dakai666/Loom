"""
InputArea component — multi-line chat input with slash command completion.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.events import Key
from textual.message import Message
from textual.widget import Widget
from textual.widgets import OptionList, TextArea
from textual.widgets.option_list import Option


_NEWLINE_KEYS = ("alt+enter", "shift+enter", "ctrl+j", "ctrl+o")


class ChatTextArea(TextArea):
    """
    TextArea tuned for chat input.

    Key handling depends on whether the slash-popup (sibling OptionList inside
    InputArea) is visible:

    - Popup visible:
        Up / Down      → move popup selection
        Enter / Tab    → accept highlighted suggestion
        Escape         → close popup (no TextArea interrupt)
    - Popup hidden:
        Enter          → emit Submit, clear TextArea
        Alt+Enter / Shift+Enter / Ctrl+J / Ctrl+O → insert newline
        (multiple aliases because macOS terminals route Option differently)
        Tab            → slash command completion (cycles exact prefix match)
        Ctrl+V / Cmd+V → native paste, preserves newlines
    """

    def on_key(self, event: Key) -> None:
        parent = self.parent
        popup_visible = (
            isinstance(parent, InputArea) and parent._popup_visible()
        )

        if popup_visible:
            assert isinstance(parent, InputArea)
            if event.key == "up":
                parent._popup_move(-1)
                event.prevent_default(); event.stop(); return
            if event.key == "down":
                parent._popup_move(1)
                event.prevent_default(); event.stop(); return
            if event.key in ("enter", "tab"):
                if parent._accept_popup():
                    event.prevent_default(); event.stop(); return
            if event.key == "escape":
                parent._hide_popup()
                event.prevent_default(); event.stop(); return

        # History navigation: Up/Down when text is empty OR already navigating.
        # Once the user edits a recalled entry, nav mode clears and arrows
        # revert to native cursor movement.
        if isinstance(parent, InputArea) and event.key in ("up", "down"):
            if parent._history and (not self.text or parent._nav_active):
                if event.key == "up":
                    parent._history_prev()
                else:
                    parent._history_next()
                event.prevent_default(); event.stop(); return

        if event.key == "enter":
            text = self.text.strip()
            if text:
                if isinstance(parent, InputArea):
                    parent.record_submission(text)
                self.post_message(InputArea.Submit(text))
                self.load_text("")
            event.prevent_default(); event.stop()
        elif event.key in _NEWLINE_KEYS:
            self.insert("\n")
            event.prevent_default(); event.stop()


class InputArea(Widget):
    """
    Multi-line chat input with slash command popup + Tab completion.
    """

    SLASH_COMMANDS = [
        "/new",
        "/sessions",
        "/model",
        "/personality",
        "/personality off",
        "/personality adversarial",
        "/personality minimalist",
        "/personality architect",
        "/personality researcher",
        "/personality operator",
        "/tier",
        "/tier 1",
        "/tier 2",
        "/compact",
        "/auto",
        "/pause",
        "/stop",
        "/scope",
        "/scope revoke",
        "/scope clear",
        "/help",
    ]

    DEFAULT_CSS = """
    InputArea {
        height: auto;
    }

    #slash-popup {
        height: auto;
        max-height: 8;
        background: #242018;
        border: solid #c8a464;
        display: none;
    }

    #slash-popup.visible {
        display: block;
    }

    #slash-popup > .option-list--option-highlighted {
        background: #c8a464;
        color: #1c1814;
    }
    """

    class Submit(Message, bubble=True):
        """User submitted a message."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._history: list[str] = []
        self._history_idx: int | None = None  # index into _history; None = not browsing
        self._nav_active: bool = False
        self._suppress_change: bool = False   # True while we programmatically load history

    def compose(self) -> ComposeResult:
        yield OptionList(id="slash-popup")
        yield ChatTextArea(
            id="input-text",
            soft_wrap=True,
            show_line_numbers=False,
            tab_behavior="focus",
        )

    def on_mount(self) -> None:
        self.query_one("#input-text", ChatTextArea).focus()

    # ── History ──────────────────────────────────────────────────────────────

    _HISTORY_MAX = 200

    def record_submission(self, text: str) -> None:
        """Append a sent message to history (called from Submit handler)."""
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
            if len(self._history) > self._HISTORY_MAX:
                self._history = self._history[-self._HISTORY_MAX:]
        self._history_idx = None
        self._nav_active = False

    def _load_history_entry(self, idx: int) -> None:
        ta = self.query_one("#input-text", ChatTextArea)
        entry = self._history[idx]
        self._suppress_change = True
        try:
            ta.load_text(entry)
        finally:
            self._suppress_change = False
        ta.cursor_location = _offset_to_location(entry, len(entry))
        self._history_idx = idx
        self._nav_active = True

    def _history_prev(self) -> None:
        if not self._history:
            return
        if self._history_idx is None:
            self._load_history_entry(len(self._history) - 1)
        elif self._history_idx > 0:
            self._load_history_entry(self._history_idx - 1)

    def _history_next(self) -> None:
        if self._history_idx is None:
            return
        if self._history_idx < len(self._history) - 1:
            self._load_history_entry(self._history_idx + 1)
        else:
            # past newest — clear and exit nav
            ta = self.query_one("#input-text", ChatTextArea)
            self._suppress_change = True
            try:
                ta.load_text("")
            finally:
                self._suppress_change = False
            self._history_idx = None
            self._nav_active = False

    # ── Popup state ──────────────────────────────────────────────────────────

    def _popup(self) -> OptionList:
        return self.query_one("#slash-popup", OptionList)

    def _popup_visible(self) -> bool:
        return self._popup().has_class("visible")

    def _hide_popup(self) -> None:
        self._popup().remove_class("visible")

    def _popup_move(self, delta: int) -> None:
        popup = self._popup()
        count = popup.option_count
        if count == 0:
            return
        cur = popup.highlighted or 0
        popup.highlighted = (cur + delta) % count

    # ── Slash word resolution ────────────────────────────────────────────────

    def _slash_context(self, ta: ChatTextArea) -> tuple[str, str, str] | None:
        """
        Return (head, slash_word, tail) if the cursor-adjacent word starts with
        '/'.  Otherwise None.
        """
        text = ta.text
        row, col = ta.cursor_location
        lines = text.split("\n")
        current_line = lines[row] if row < len(lines) else ""
        before = "\n".join(lines[:row] + [current_line[:col]])
        tail = text[len(before):]
        # last whitespace-separated chunk
        sep_idx = max(before.rfind(" "), before.rfind("\n"), before.rfind("\t"))
        word = before[sep_idx + 1:]
        if not word.startswith("/"):
            return None
        head = before[: sep_idx + 1]
        return head, word, tail

    def _refresh_popup(self) -> None:
        ta = self.query_one("#input-text", ChatTextArea)
        popup = self._popup()
        ctx = self._slash_context(ta)
        if ctx is None:
            popup.remove_class("visible")
            return
        _, word, _ = ctx
        matches = [c for c in self.SLASH_COMMANDS if c.startswith(word)]
        if not matches:
            popup.remove_class("visible")
            return
        popup.clear_options()
        for cmd in matches:
            popup.add_option(Option(cmd, id=cmd))
        popup.add_class("visible")
        popup.highlighted = 0

    def _accept_popup(self) -> bool:
        popup = self._popup()
        if not popup.has_class("visible") or popup.option_count == 0:
            return False
        idx = popup.highlighted or 0
        opt = popup.get_option_at_index(idx)
        cmd = opt.id or ""
        ta = self.query_one("#input-text", ChatTextArea)
        ctx = self._slash_context(ta)
        if ctx is None:
            popup.remove_class("visible")
            return False
        head, _, tail = ctx
        new_text = head + cmd + tail
        ta.load_text(new_text)
        ta.cursor_location = _offset_to_location(new_text, len(head + cmd))
        popup.remove_class("visible")
        return True

    # ── Event hooks ──────────────────────────────────────────────────────────

    def on_text_area_changed(self, _event: TextArea.Changed) -> None:
        """Re-filter popup whenever the TextArea content changes."""
        # User-typed edits exit history nav mode; programmatic loads don't.
        if not self._suppress_change and self._nav_active:
            self._nav_active = False
            self._history_idx = None
        self._refresh_popup()

    def on_key(self, event: Key) -> None:
        """Tab at the InputArea level — complete slash commands when popup
        is not handling it (e.g. no popup yet because first keystroke)."""
        if event.key != "tab":
            return
        if self._popup_visible():
            return  # ChatTextArea handles it
        ta = self.query_one("#input-text", ChatTextArea)
        ctx = self._slash_context(ta)
        if ctx is None:
            return
        head, word, tail = ctx
        for cmd in self.SLASH_COMMANDS:
            if cmd.startswith(word) and cmd != word:
                ta.load_text(head + cmd + tail)
                ta.cursor_location = _offset_to_location(ta.text, len(head + cmd))
                break
        event.prevent_default()
        event.stop()


def _offset_to_location(text: str, offset: int) -> tuple[int, int]:
    """Convert a flat char offset into a (row, col) TextArea location."""
    row = 0
    col = 0
    for ch in text[:offset]:
        if ch == "\n":
            row += 1
            col = 0
        else:
            col += 1
    return row, col
