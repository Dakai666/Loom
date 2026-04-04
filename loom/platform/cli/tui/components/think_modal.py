"""
ThinkModal — TUI viewer for the last turn's reasoning chain.

Opens via /think slash command OR clicking the ▸ thinking indicator in a message.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea


class ThinkModal(ModalScreen[None]):
    """
    Read-only modal showing the full <think>…</think> reasoning block.
    Escape or Enter or clicking Close dismisses it.
    """

    DEFAULT_CSS = """
    ThinkModal {
        align: center middle;
    }

    #think-dialog {
        background: #242018;
        border: thick #c8a464;
        padding: 1 2;
        width: 80;
        height: 24;
    }

    #think-title {
        color: #c8a464;
        text-style: bold;
        margin-bottom: 1;
    }

    #think-content {
        height: 1fr;
        margin-bottom: 1;
        background: #1c1814;
        color: #e0cfa0;
        border: solid #4a4038;
    }

    #think-close-row {
        align: center middle;
        height: 3;
    }

    #close-btn {
        background: #4a4038;
        color: #e0cfa0;
        border: solid #c8a464;
        min-width: 14;
    }
    #close-btn:hover {
        background: #c8a464;
        color: #1c1814;
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
            yield Static("  ◌ Reasoning chain", id="think-title")
            yield TextArea(
                self._think_text,
                id="think-content",
                read_only=True,
            )
            with Vertical(id="think-close-row"):
                yield Button("Close  [Esc]", id="close-btn")

    def on_button_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
