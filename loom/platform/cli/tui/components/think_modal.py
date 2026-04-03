"""
ThinkModal — TUI viewer for the last turn's reasoning chain.

Displayed via /think slash command.  Read-only; Escape or Enter closes it.
"""

from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Button, Static
from textual.containers import Vertical
from textual.widgets import TextArea


class ThinkModal(ModalScreen[None]):
    """
    Read-only modal that shows the full <think>…</think> content from the
    last agent turn.

    Layout:
        ┌─ Reasoning chain ─────────────────────────────┐
        │                                               │
        │  <think content here, scrollable>             │
        │                                               │
        │                  [ Close ]                    │
        └───────────────────────────────────────────────┘
    """

    DEFAULT_CSS = """
    ThinkModal {
        align: center middle;
    }

    #think-dialog {
        background: $surface;
        border: thick $primary-darken-2;
        padding: 1 2;
        width: 80;
        height: 24;
    }

    #think-title {
        color: $primary;
        text-style: bold;
        margin-bottom: 1;
    }

    #think-content {
        height: 1fr;
        margin-bottom: 1;
    }

    #think-close-row {
        align: center middle;
        height: 3;
    }
    """

    BINDINGS = [
        ("escape", "close", "Close"),
        ("enter", "close", "Close"),
    ]

    def __init__(self, think_text: str) -> None:
        super().__init__()
        self._think_text = think_text

    def compose(self) -> ComposeResult:
        with Vertical(id="think-dialog"):
            yield Static("  Reasoning chain", id="think-title")
            yield TextArea(
                self._think_text,
                id="think-content",
                read_only=True,
            )
            with Vertical(id="think-close-row"):
                yield Button("Close  [Esc]", id="close-btn", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
