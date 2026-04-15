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
    A small clickable line that represents a <think> reasoning block.
    Clicking anywhere on it posts OpenThinkModal.

    When ``summary`` is provided (inline think blocks during streaming):
        💭 <summary>…  (click to expand)
    When no summary (legacy post-turn mount via set_think_text):
        ▸ thinking  (click to expand)
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

    def __init__(self, think_text: str, *, summary: str = "") -> None:
        if summary:
            # Strip newlines and escape brackets before embedding in Textual markup.
            # Textual 8.x's markup parser fails on \n inside [dim]…[/dim] spans
            # (MarkupError: Expected markup value (found '/dim]\n')).
            safe = (
                summary[:100]
                .replace("\n", " ")
                .replace("\r", " ")
                .replace("[", "\\[")
            )
            label = f"[dim]💭 {safe}{'…' if len(summary) > 100 else ''}[/dim]  [dim italic](click to expand)[/dim italic]"
        else:
            label = "[dim]▸ thinking[/dim]  [dim italic](click to expand)[/dim italic]"
        super().__init__(label)
        self._think_text = think_text

    def on_click(self, _event) -> None:
        self.post_message(self.OpenThinkModal(self._think_text))


# ---------------------------------------------------------------------------
# MessageBubble
# ---------------------------------------------------------------------------

class MessageBubble(Widget):
    """
    Single message: role header + optional think indicator(s) + body content.

    During streaming: body = plain RichText + ▌ cursor.
    After finish_stream(): body = Markdown(content, code_theme="gruvbox-dark").

    Think blocks during streaming are handled inline via ``stream_think()``:
    each call seals the current body segment, mounts a clickable ThinkIndicator,
    then opens a new body Static for subsequent content.  This lets the user see
    and click reasoning summaries without waiting for TurnDone.
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
        # _segments holds text per body section; segment[-1] is the active one.
        self._segments: list[str] = [content]
        self._streaming = streaming
        self._created_at = datetime.datetime.now()
        self._think_text: str = ""
        self._has_inline_thinks: bool = False  # True once stream_think() is called
        # Sealed body Statics with their corresponding text segment, for Markdown
        # rendering at finish_stream() time.
        self._sealed_bodies: list[tuple[Static, str]] = []
        # Active body widget (updated by _render_body_text).
        self._current_body: Static | None = None

    @property
    def _content(self) -> str:
        """Full text content — all segments joined (backward compat)."""
        return "".join(self._segments)

    def compose(self) -> ComposeResult:
        yield Static("", id="bubble-header", markup=True)
        body = Static("", id="bubble-body", markup=False)
        self._current_body = body
        yield body

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
        self._segments[-1] += text
        self._render_header()
        self._render_body_text()

    def stream_think(self, summary: str, full: str) -> None:
        """
        Insert an inline think block at the current streaming position.

        Seals the current body segment (keeps its RichText content), mounts a
        clickable ThinkIndicator summarising the reasoning, then opens a fresh
        body Static that receives all subsequent ``append_stream()`` calls.
        """
        self._has_inline_thinks = True
        # Record the sealed segment + its widget for Markdown rendering later.
        if self._current_body is not None:
            self._sealed_bodies.append((self._current_body, self._segments[-1]))
        # Mount ThinkIndicator after the current body.
        indicator = ThinkIndicator(full, summary=summary)
        self.mount(indicator)
        # Open a new segment + new body Static for continued streaming.
        self._segments.append("")
        new_body = Static("", markup=False)
        self._current_body = new_body
        self.mount(new_body)
        self.scroll_visible()

    def finish_stream(self) -> None:
        self._streaming = False
        self._render_header()
        if self._sealed_bodies:
            # Multi-segment turn: render each sealed segment as Markdown, then
            # render the final (active) segment.
            for body_widget, seg_text in self._sealed_bodies:
                if seg_text.strip():
                    body_widget.update(Markdown(seg_text, code_theme="gruvbox-dark"))
            last_seg = self._segments[-1]
            if last_seg.strip() and self._current_body is not None:
                self._current_body.update(
                    Markdown(last_seg, code_theme="gruvbox-dark")
                )
        else:
            # Single-segment turn (no inline think blocks) — original behaviour.
            if self._content.strip():
                self._render_body_markdown()
            else:
                self._render_body_text()
        self._scan_and_mount_images()

    def _scan_and_mount_images(self) -> None:
        import re
        from pathlib import Path
        from urllib.parse import urlparse
        from urllib.request import url2pathname
        from .image_widget import ImageWidget

        # match markdown images ![alt](path)
        matches = re.findall(r'!\[.*?\]\((.*?)\)', self._content)
        for url in matches:
            try:
                if url.startswith("http"):
                    continue
                
                if url.startswith("file://"):
                    parsed = urlparse(url)
                    # Handle Windows drive letters properly
                    local_path = url2pathname(parsed.path)
                    p = Path(local_path).resolve()
                else:
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

        Skipped when inline think blocks have already been mounted via
        ``stream_think()`` to avoid duplicate indicators.
        """
        if not think_text or self._think_text or self._has_inline_thinks:
            return  # already set, no content, or inline thinks handle it
        self._think_text = think_text
        # Insert the indicator before the first body static
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
        body = self._current_body
        if body is None:
            from textual.css.query import NoMatches
            try:
                body = self.query_one("#bubble-body", Static)
                self._current_body = body
            except NoMatches:
                return
        text = RichText()
        text.append(self._segments[-1], style=Style(color="#e0cfa0"))
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

    def stream_think(self, summary: str, full: str) -> None:
        """Insert an inline think indicator into the current streaming bubble."""
        if self._streaming_bubble is None:
            # No active bubble yet — create one so the indicator has a home.
            self._msg_counter += 1
            bubble = MessageBubble(
                Role.ASSISTANT, "", streaming=True, msg_id=str(self._msg_counter)
            )
            self._bubbles.append(bubble)
            self._streaming_bubble = bubble
            self.mount(bubble)
        self._streaming_bubble.stream_think(summary, full)
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
