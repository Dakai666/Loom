"""
InputArea component — multiline input with slash command completion.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.events import Key
from textual.message import Message
from textual.widgets import TextArea


class InputArea(TextArea):
    """
    Multiline text input with slash command completion.

    Handles:
    - Tab completion for slash commands (/personality, /compact, etc.)
    - Enter to submit
    - Multiline input (Shift+Enter for newline)
    - History navigation (up/down) — delegated to parent app
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
        yield TextArea(
            id="input-text",
            placeholder="Type a message... (/help for commands)",
            multiline=False,
            tab_behavior="indent",
        )

    def on_mount(self) -> None:
        ta = self.query_one("#input-text", TextArea)
        ta.focus()

    def _on_key(self, event: Key) -> None:
        """Handle key events for submission and completion."""
        ta = self.query_one("#input-text", TextArea)
        text = ta.text

        if event.key == "enter":
            if text.strip():
                self.post_message(self.Submit(text.strip()))
                ta.text = ""
            return

        if event.key == "tab":
            # Simple slash command completion
            cursor = ta.cursor_position
            text_before = text[:cursor]
            last_word = text_before.split()[-1] if text_before.split() else ""
            if last_word.startswith("/"):
                for cmd in self.SLASH_COMMANDS:
                    if cmd.startswith(last_word) and cmd != last_word:
                        # Replace the incomplete word
                        before = text_before[: cursor - len(last_word)]
                        after = text[cursor:]
                        ta.text = before + cmd + after
                        ta.cursor_position = len(before) + len(cmd)
                        break
