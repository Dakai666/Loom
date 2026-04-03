"""
MessageList component — conversation history with streaming support.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum

from rich.style import Style
from rich.text import Text as RichText
from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class Role(Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


@dataclass
class MessageItem:
    """A single message in the conversation."""

    role: Role
    content: str
    timestamp: datetime.datetime = field(default_factory=datetime.datetime.now)
    streaming: bool = False


class MessageList(Widget):
    """
    Scrollable list of messages (user + assistant).

    Uses Rich Text objects (not markup strings) for content rendering so
    that arbitrary user/LLM text containing [ ] brackets never triggers
    Textual's markup parser.
    """

    DEFAULT_CSS = """
    MessageList {
        overflow-y: auto;
    }
    """

    messages: reactive[list[MessageItem]] = reactive([], layout=True)
    _current_assistant_buffer: str = ""

    class StreamingText(Message, bubble=True):
        """New streaming text chunk arrived."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def compose(self) -> ComposeResult:
        yield Static("", id="message-content", markup=False)

    def on_mount(self) -> None:
        self.border_title = "conversation"

    def add_message(self, role: Role, content: str) -> None:
        """Add a complete message (user or assistant)."""
        msg = MessageItem(role=role, content=content, streaming=False)
        self.messages = [*self.messages, msg]
        self._current_assistant_buffer = ""
        self._update_display()
        self._scroll_to_bottom()

    def stream_text(self, text: str) -> None:
        """Append streaming text to the last assistant message, or create new one."""
        self._current_assistant_buffer += text
        if not self.messages or self.messages[-1].role != Role.ASSISTANT:
            msg = MessageItem(role=Role.ASSISTANT, content=text, streaming=True)
            self.messages = [*self.messages, msg]
        else:
            self.messages[-1].content = self._current_assistant_buffer
            self.messages = self.messages  # trigger reactivity
        self._update_display()
        self._scroll_to_bottom()

    def finish_streaming(self) -> None:
        """Mark the current streaming assistant message as complete."""
        if self.messages and self.messages[-1].streaming:
            self.messages[-1].streaming = False
            self.messages = self.messages  # trigger reactivity
            self._current_assistant_buffer = ""
            self._update_display()

    def clear(self) -> None:
        """Clear all messages."""
        self.messages = []
        self._current_assistant_buffer = ""
        self._update_display()

    def _update_display(self) -> None:
        """Render all messages as a Rich Text object (no markup parsing)."""
        from textual.css.query import NoMatches

        try:
            content = self.query_one("#message-content", Static)
        except NoMatches:
            return

        if not self.messages:
            # markup=False Static — pass a RichText with dim style
            placeholder = RichText("(no messages yet)", style=Style(dim=True))
            content.update(placeholder)
            return

        # Build one big RichText by concatenating per-message Text objects.
        # Using Rich Text.append() means content is NEVER parsed as markup.
        combined = RichText()

        role_styles = {
            Role.USER:      ("user",      Style(bold=True, color="yellow")),
            Role.ASSISTANT: ("assistant", Style(bold=True, color="green")),
            Role.SYSTEM:    ("system",    Style(dim=True)),
        }

        for i, msg in enumerate(self.messages):
            role_label, role_style = role_styles[msg.role]
            timestamp = msg.timestamp.strftime("%H:%M")

            # timestamp
            combined.append(timestamp, style=Style(dim=True))
            combined.append(" ")
            # role
            combined.append(role_label, style=role_style)
            combined.append(":")

            # streaming cursor
            if msg.streaming:
                combined.append(" ▌", style=Style(bold=True, color="yellow"))

            combined.append("\n")

            # content — plain append, no escaping, no markup parsing
            combined.append(msg.content)

            combined.append("\n\n")

        content.update(combined)

    def _scroll_to_bottom(self) -> None:
        """Auto-scroll to bottom."""
        self.scroll_end(animate=False)
