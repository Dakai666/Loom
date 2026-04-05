"""
MessageList component — conversation history with streaming + Markdown rendering.

Architecture: each message is a MessageBubble widget mounted dynamically.
  - Streaming:          update bubble body with plain RichText (safe, no markup parsing)
  - finish_streaming(): replace body with rich.markdown.Markdown (syntax-highlighted,
                        white prose text — visually distinct from streaming cream text)
  - Think content:      if the assistant turn contained a <think> block, a clickable
                        "▸ thinking" indicator is mounted inside the bubble; click opens
                        ThinkModal via a bubbled OpenThinkModal message.
"""

from __future__ import annotations

import datetime
from enum import Enum

from rich.markdown import Markdown
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


_ROLE_COLOR = {
    Role.USER:      "#d4a853",   # warm amber/gold
    Role.ASSISTANT: "#a0b898",   # sage / celadon
    Role.SYSTEM:    "#8a7a5e",   # muted warm grey
}


# ---------------------------------------------------------------------------
# Think indicator
# ---------------------------------------------------------------------------

class ThinkIndicator(Static):
    """
    A small clickable line that appears when the assistant message had
    a <think> reasoning block.  Clicking anywhere on it posts OpenThinkModal.

    Shows: ▸ thinking  [click to expand]
    """

    DEFAULT_CSS = """
    ThinkIndicator {
        height: 1;
        color: #8a7a5e;
    }
    ThinkIndicator:hover {
        color: #c8a464;
    }
    """

    class OpenThinkModal(Message, bubble=True):
        """Posted up to the App to open ThinkModal with the reasoning text."""
        def __init__(self, think_text: str) -> None:
            super().__init__()
            self.think_text = think_text

    def __init__(self, think_text: str) -> None:
        super().__init__("[dim]▸ thinking[/dim]  [dim italic](click to expand)[/dim italic]")
        self._think_text = think_text

    def on_click(self, _event) -> None:
        self.post_message(self.OpenThinkModal(self._think_text))


# ---------------------------------------------------------------------------
# MessageBubble
# ---------------------------------------------------------------------------

class MessageBubble(Widget):
    """
    Single message: role header + optional think indicator + body content.

    During streaming: body = plain RichText + ▌ cursor.
    After finish_stream(): body = Markdown(content, code_theme="gruvbox-dark").
    If think_text is set: a ThinkIndicator is mounted between header and body.
    """

    DEFAULT_CSS = """
    MessageBubble {
        height: auto;
        padding: 0 1 1 1;
    }
    #bubble-header {
        height: 1;
    }
    #bubble-body {
        height: auto;
    }
    """

    def __init__(
        self,
        role: Role,
        content: str = "",
        streaming: bool = False,
        msg_id: str = "",
    ) -> None:
        super().__init__(id=f"bubble-{msg_id}" if msg_id else None)
        self._role = role
        self._content = content
        self._streaming = streaming
        self._created_at = datetime.datetime.now()
        self._think_text: str = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="bubble-header", markup=True)
        yield Static("", id="bubble-body", markup=False)

    def on_mount(self) -> None:
        self._render_header()
        if not self._streaming:
            if self._content.strip():
                self._render_body_markdown()
            else:
                self._render_body_text()
            # Must run after mounting self or at the end of mount
            self.set_timer(0.01, self._scan_and_mount_images)
        else:
            self._render_body_text()

    # ── Streaming API ─────────────────────────────────────────────────────────

    def append_stream(self, text: str) -> None:
        self._content += text
        self._render_header()
        self._render_body_text()

    def finish_stream(self) -> None:
        self._streaming = False
        self._render_header()
        if self._content.strip():
            self._render_body_markdown()
        else:
            self._render_body_text()
            
        self._scan_and_mount_images()

    def _scan_and_mount_images(self) -> None:
        import re
        from pathlib import Path
        from .image_widget import ImageWidget

        # match markdown images ![alt](path)
        matches = re.findall(r'!\[.*?\]\((.*?)\)', self._content)
        for url in matches:
            try:
                # ignore http urls, we only render local files natively right now
                if url.startswith("http"):
                    continue
                p = Path(url).resolve()
                if p.exists() and p.is_file():
                    ext = p.suffix.lower()
                    if ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"]:
                        self.mount(ImageWidget(p))
            except Exception:
                pass

    def set_think_text(self, think_text: str) -> None:
        """
        Called after finish_stream() when the turn had reasoning content.
        Mounts a ThinkIndicator between the header and body.
        """
        if not think_text or self._think_text:
            return  # already set or no content
        self._think_text = think_text
        # Insert the indicator before the body static
        try:
            body = self.query_one("#bubble-body")
            self.mount(ThinkIndicator(think_text), before=body)
        except Exception:
            pass  # widget not ready — skip silently

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _render_header(self) -> None:
        from textual.css.query import NoMatches
        try:
            header = self.query_one("#bubble-header", Static)
        except NoMatches:
            return

        ts = self._created_at.strftime("%H:%M")
        color = _ROLE_COLOR.get(self._role, "#8a7a5e")
        label = self._role.value

        if self._streaming:
            header.update(
                f"[dim]{ts}[/dim]  [bold {color}]{label}[/bold {color}]"
                f"  [bold #d4a853]▌[/bold #d4a853]"
            )
        else:
            header.update(
                f"[dim]{ts}[/dim]  [bold {color}]{label}[/bold {color}]"
            )

    def _render_body_text(self) -> None:
        from textual.css.query import NoMatches
        try:
            body = self.query_one("#bubble-body", Static)
        except NoMatches:
            return
        text = RichText()
        text.append(self._content, style=Style(color="#e0cfa0"))
        body.update(text)

    def _render_body_markdown(self) -> None:
        from textual.css.query import NoMatches
        try:
            body = self.query_one("#bubble-body", Static)
        except NoMatches:
            return
        # code_theme="gruvbox-dark" gives warm, dark code blocks that complement
        # the parchment palette; prose text renders as white (Rich default) which
        # distinguishes completed responses from streaming (#e0cfa0) cream text.
        body.update(Markdown(self._content, code_theme="gruvbox-dark"))


# ---------------------------------------------------------------------------
# MessageList
# ---------------------------------------------------------------------------

class MessageList(Widget):
    """
    Scrollable conversation history.  Each message is a dynamically mounted
    MessageBubble.  Handles OpenThinkModal messages by pushing ThinkModal.
    """

    DEFAULT_CSS = """
    MessageList {
        overflow-y: auto;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._bubbles: list[MessageBubble] = []
        self._streaming_bubble: MessageBubble | None = None
        self._msg_counter: int = 0

    def compose(self) -> ComposeResult:
        return iter([])

    def on_mount(self) -> None:
        self.border_title = "conversation"

    # ── Public API ────────────────────────────────────────────────────────────

    def add_message(self, role: Role, content: str) -> None:
        self._streaming_bubble = None
        self._msg_counter += 1
        bubble = MessageBubble(role, content, streaming=False, msg_id=str(self._msg_counter))
        self._bubbles.append(bubble)
        self.mount(bubble)
        self.scroll_end(animate=False)

    def stream_text(self, text: str) -> None:
        if self._streaming_bubble is None:
            self._msg_counter += 1
            bubble = MessageBubble(
                Role.ASSISTANT, text, streaming=True, msg_id=str(self._msg_counter)
            )
            self._bubbles.append(bubble)
            self._streaming_bubble = bubble
            self.mount(bubble)
        else:
            self._streaming_bubble.append_stream(text)
        self.scroll_end(animate=False)

    def finish_streaming(self) -> None:
        if self._streaming_bubble is not None:
            self._streaming_bubble.finish_stream()
            self._streaming_bubble = None

    def set_last_think(self, think_text: str) -> None:
        """Attach think content to the last assistant bubble."""
        for bubble in reversed(self._bubbles):
            if bubble._role == Role.ASSISTANT:
                bubble.set_think_text(think_text)
                break

    def clear(self) -> None:
        self._streaming_bubble = None
        self._bubbles.clear()
        self._msg_counter = 0
        self.remove_children()

    # ── Think modal relay ─────────────────────────────────────────────────────

    def on_think_indicator_open_think_modal(
        self, event: ThinkIndicator.OpenThinkModal
    ) -> None:
        """Relay ThinkIndicator clicks to the App to open ThinkModal."""
        from loom.platform.cli.tui.components.think_modal import ThinkModal
        self.app.push_screen(ThinkModal(event.think_text))
