"""
MessageSearchModal — find text in the current session's conversation history.

Accessed via F1 command palette → "Search messages".  Kept off the global
keybinding list on purpose, so Ctrl+F remains free for the host IDE / terminal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList
from textual.widgets.option_list import Option

if TYPE_CHECKING:
    from .message_list import MessageBubble, MessageList


_SNIPPET_RADIUS = 40


def _search_bubbles(
    bubbles: list["MessageBubble"], query: str
) -> list[tuple["MessageBubble", str]]:
    """Return (bubble, snippet) pairs whose full content contains `query`."""
    if not query:
        return []
    q = query.lower()
    out: list[tuple[MessageBubble, str]] = []
    for b in bubbles:
        text = b._content  # full message text across segments
        idx = text.lower().find(q)
        if idx < 0:
            continue
        start = max(0, idx - _SNIPPET_RADIUS)
        end = min(len(text), idx + len(query) + _SNIPPET_RADIUS)
        snippet = text[start:end].replace("\n", " ")
        if start > 0:
            snippet = "…" + snippet
        if end < len(text):
            snippet = snippet + "…"
        out.append((b, snippet))
    return out


class MessageSearchModal(ModalScreen["MessageBubble | None"]):
    """Modal search over the in-memory conversation bubbles."""

    DEFAULT_CSS = """
    MessageSearchModal {
        align: center middle;
    }

    #search-dialog {
        background: #242018;
        border: thick #4a4038;
        padding: 1 2;
        width: 80;
        height: 24;
    }

    #search-title {
        color: #c8a464;
        text-style: bold;
        margin-bottom: 1;
    }

    #search-input {
        border: solid #4a4038;
        background: #1c1814;
        color: #e0cfa0;
        margin-bottom: 1;
    }
    #search-input:focus {
        border: solid #c8a464;
    }

    #search-results {
        height: 1fr;
        background: #1c1814;
        border: solid #4a4038;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
    ]

    def __init__(self, message_list: "MessageList") -> None:
        super().__init__()
        self._message_list = message_list
        # Cache the id → bubble map so option selection can retrieve it.
        self._option_map: dict[str, "MessageBubble"] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="search-dialog"):
            yield Label("🔍 Search messages", id="search-title")
            yield Input(placeholder="Type to search…", id="search-input")
            yield OptionList(id="search-results")

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh_results(event.value.strip())

    def _refresh_results(self, query: str) -> None:
        opt_list = self.query_one("#search-results", OptionList)
        opt_list.clear_options()
        self._option_map.clear()
        if not query:
            return
        hits = _search_bubbles(self._message_list._bubbles, query)
        for i, (bubble, snippet) in enumerate(hits):
            key = f"hit-{i}"
            self._option_map[key] = bubble
            role = bubble._role.value
            ts = bubble._created_at.strftime("%H:%M")
            role_colour = {"user": "#d4a853", "assistant": "#a0b898"}.get(role, "#8a7a5e")
            label = (
                f"[{role_colour}]{role:<9}[/{role_colour}] "
                f"[dim]{ts}[/dim]  {snippet}"
            )
            opt_list.add_option(Option(label, id=key))
        if not hits:
            opt_list.add_option(Option("[dim]No matches.[/dim]", disabled=True))

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        key = event.option.id or ""
        bubble = self._option_map.get(key)
        self.dismiss(bubble)

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.dismiss(None)
