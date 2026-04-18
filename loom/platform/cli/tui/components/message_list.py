"""
MessageList component — conversation history with streaming + Markdown rendering.

Architecture: each message is a MessageBubble widget mounted dynamically.
  - Streaming:          update bubble body with plain RichText (safe, no markup parsing)
  - finish_streaming(): replace body with rich.markdown.Markdown (syntax-highlighted,
                        white prose text — visually distinct from streaming cream text)
  - Think content:      if the assistant turn contained a <think> block, a clickable
                        "▸ thinking" indicator is mounted inside the bubble; click
                        toggles an inline panel with the full reasoning text.
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
    Clickable one-line header representing a <think> reasoning block.
    Click toggles an inline sibling panel showing the full reasoning text.

    Collapsed label:
        ▸ 💭 <summary>…  (click to expand)
        ▸ thinking         ← when no summary (post-turn)
    Expanded label:
        ▾ 💭 <summary>…  (click to collapse)
    """

    DEFAULT_CSS = """
    ThinkIndicator {
        height: 1;
        color: #8a7a5e;
    }
    ThinkIndicator:hover {
        color: #c8a464;
    }
    .think-panel {
        color: #8a7a5e;
        background: #1c1814;
        border-left: solid #4a4038;
        padding: 0 2;
        margin: 0 0 1 2;
        height: auto;
    }
    """

    def __init__(self, think_text: str, *, summary: str = "") -> None:
        self._think_text = think_text
        self._summary = summary
        self._expanded = False
        self._panel: Static | None = None
        super().__init__(self._render_label())

    def _render_label(self) -> str:
        arrow = "▾" if self._expanded else "▸"
        hint = "click to collapse" if self._expanded else "click to expand"
        if self._summary:
            # Strip newlines and escape brackets before embedding in Textual markup.
            # Textual 8.x's markup parser fails on \n inside [dim]…[/dim] spans.
            safe = (
                self._summary[:100]
                .replace("\n", " ")
                .replace("\r", " ")
                .replace("[", "\\[")
            )
            ellipsis = "…" if len(self._summary) > 100 else ""
            return (
                f"[dim]{arrow} 💭 {safe}{ellipsis}[/dim]  "
                f"[dim italic]({hint})[/dim italic]"
            )
        return (
            f"[dim]{arrow} thinking[/dim]  "
            f"[dim italic]({hint})[/dim italic]"
        )

    def on_click(self, _event) -> None:
        self._expanded = not self._expanded
        self.update(self._render_label())
        parent = self.parent
        if self._expanded:
            if parent is not None and self._panel is None:
                panel = Static(self._think_text, classes="think-panel", markup=False)
                try:
                    parent.mount(panel, after=self)
                    self._panel = panel
                except Exception:
                    pass
        else:
            if self._panel is not None:
                try:
                    self._panel.remove()
                except Exception:
                    pass
                self._panel = None


# ---------------------------------------------------------------------------
# Copy indicator
# ---------------------------------------------------------------------------

class CopyIndicator(Static):
    """
    Unobtrusive 📋 icon at the end of long MessageBubbles.
    Clicking posts a Copy message carrying the bubble's full text.
    """

    DEFAULT_CSS = """
    CopyIndicator {
        height: 1;
        width: 2;
        color: #4a4038;
    }
    CopyIndicator:hover {
        color: #c8a464;
    }
    """

    class Copy(Message, bubble=True):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, get_text) -> None:
        super().__init__("📋")
        self._get_text = get_text

    def on_click(self, _event) -> None:
        text = self._get_text() or ""
        if text:
            self.post_message(self.Copy(text))


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
            self._mount_copy_indicator()
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
        self._mount_copy_indicator()

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

    _COPY_MIN_CHARS = 80  # skip short bubbles; reserve indicator for substantial content

    def _mount_copy_indicator(self) -> None:
        """Append a click-to-copy indicator once per bubble, only when worthwhile."""
        if self._role == Role.SYSTEM:
            return
        content = self._content.strip()
        if len(content) < self._COPY_MIN_CHARS:
            return
        if getattr(self, "_copy_mounted", False):
            return
        try:
            self.mount(CopyIndicator(lambda: self._content))
            self._copy_mounted = True
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
    MessageBubble.  Relays CopyIndicator.Copy messages to the system clipboard.
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

    def on_copy_indicator_copy(self, event: "CopyIndicator.Copy") -> None:
        """Relay copy clicks to the system clipboard."""
        from loom.platform.cli.tui.clipboard import copy_text
        try:
            copy_text(self.app, event.text)
            self.app.notify(f"Copied {len(event.text)} chars", timeout=1.5)
        except Exception:
            self.app.notify("Copy failed", severity="warning", timeout=1.5)
