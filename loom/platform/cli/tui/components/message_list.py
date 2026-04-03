"""
MessageList component — conversation history with streaming support.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from rich.text import Text as RichText
from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.scroll_view import ScrollView
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

    Handles:
    - Appending messages
    - Streaming text accumulation (in-place update)
    - Markdown rendering for assistant messages
    - Auto-scroll to bottom on new content
    """

    messages: reactive[list[MessageItem]] = reactive([], layout=True)
    _current_assistant_buffer: str = ""

    class StreamingText(Message, bubble=True):
        """New streaming text chunk arrived."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def compose(self) -> ComposeResult:
        yield ScrollView(
            Static("", id="message-content"),
            id="message-scroll",
        )

    def on_mount(self) -> None:
        scroll = self.query_one("#message-scroll", ScrollView)
        scroll.border_title = "conversation"
        scroll.scroll_end(animate=False)

    def add_message(self, role: Role, content: str) -> None:
        """Add a complete message (user or assistant)."""
        msg = MessageItem(role=role, content=content, streaming=False)
        self.messages = [*self.messages, msg]
        self._current_assistant_buffer = ""
        self._render()
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
        self._render()
        self._scroll_to_bottom()

    def finish_streaming(self) -> None:
        """Mark the current streaming assistant message as complete."""
        if self.messages and self.messages[-1].streaming:
            self.messages[-1].streaming = False
            self.messages = self.messages  # trigger reactivity
            self._current_assistant_buffer = ""
            self._render()

    def clear(self) -> None:
        """Clear all messages."""
        self.messages = []
        self._current_assistant_buffer = ""
        self._render()

    def _render(self) -> None:
        """Render all messages to the Static widget."""
        content = self.query_one("#message-content", Static)
        lines: list[str] = []

        for msg in self.messages:
            role_tag = {
                Role.USER: "[bold yellow]user[/bold yellow]",
                Role.ASSISTANT: "[bold green]assistant[/bold green]",
                Role.SYSTEM: "[dim]system[/dim]",
            }[msg.role]

            timestamp = msg.timestamp.strftime("%H:%M")
            cursor = " [bold yellow]>[/bold yellow]" if msg.streaming else ""

            if msg.role == Role.USER:
                lines.append(f"[dim]{timestamp}[/dim] {role_tag}: {msg.content}")
            else:
                lines.append(
                    f"[dim]{timestamp}[/dim] {role_tag}:{cursor}\n{msg.content}"
                )
            lines.append("")

        content.update("\n".join(lines) if lines else "[dim](no messages yet)[/dim]")

    def _scroll_to_bottom(self) -> None:
        """Auto-scroll to bottom."""
        scroll = self.query_one("#message-scroll", ScrollView)
        scroll.scroll_end(animate=False)
