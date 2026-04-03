"""
InputArea component — single-line chat input with slash command completion.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.events import Key
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input


class InputArea(Widget):
    """
    Single-line chat input with slash command Tab-completion.

    Handles:
    - Enter to submit (posts Submit message)
    - Tab to complete slash commands
    - Inner Input widget receives focus on mount
    """

    SLASH_COMMANDS = [
        "/personality",
        "/personality off",
        "/personality adversarial",
        "/personality minimalist",
        "/personality architect",
        "/personality researcher",
        "/personality operator",
        "/compact",
        "/verbose",
        "/help",
    ]

    class Submit(Message, bubble=True):
        """User submitted a message."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def compose(self) -> ComposeResult:
        yield Input(
            id="input-text",
            placeholder="Type a message... (/help for commands)",
        )

    def on_mount(self) -> None:
        self.query_one("#input-text", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter key — submit and clear."""
        text = event.value.strip()
        if text:
            self.post_message(self.Submit(text))
            event.input.value = ""

    def on_key(self, event: Key) -> None:
        """Tab — complete slash commands."""
        if event.key != "tab":
            return
        inp = self.query_one("#input-text", Input)
        text = inp.value
        cursor = inp.cursor_position
        text_before = text[:cursor]
        words = text_before.split()
        last_word = words[-1] if words else ""
        if last_word.startswith("/"):
            for cmd in self.SLASH_COMMANDS:
                if cmd.startswith(last_word) and cmd != last_word:
                    before = text_before[: cursor - len(last_word)]
                    after = text[cursor:]
                    inp.value = before + cmd + after
                    inp.cursor_position = len(before) + len(cmd)
                    break
            event.prevent_default()
