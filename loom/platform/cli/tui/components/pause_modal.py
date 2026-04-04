"""
PauseModal — HITL pause dialog for TUI.

Shown when stream_turn() hits a pause check point.
Three outcomes:
  - Resume     → session.resume()
  - Cancel     → session.cancel()
  - Redirect   → session.resume_with(message)
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


class PauseModal(ModalScreen[str | None]):
    """
    HITL pause modal.

    Returns:
        None        → Resume (continue as-is)
        "__cancel__" → Cancel (abandon rest of turn)
        str         → Redirect message (injected then resume)
    """

    DEFAULT_CSS = """
    PauseModal {
        align: center middle;
    }
    #pause-dialog {
        background: #242018;
        border: thick #c8a464;
        padding: 1 2;
        width: 60;
        height: auto;
    }
    #pause-title {
        color: #c8a464;
        text-style: bold;
        margin-bottom: 1;
    }
    #pause-body {
        color: #8a7a5e;
        margin-bottom: 1;
    }
    #pause-input {
        background: #1c1814;
        color: #e0cfa0;
        border: solid #4a4038;
        margin-bottom: 1;
    }
    #pause-input:focus {
        border: solid #c8a464;
    }
    #pause-hint {
        color: #4a4038;
        margin-bottom: 1;
    }
    #pause-buttons {
        height: 3;
    }
    #btn-resume {
        background: #3a4a38;
        color: #7a9e78;
        border: solid #4a4038;
        margin-right: 1;
    }
    #btn-resume:hover {
        background: #4a5a48;
    }
    #btn-cancel {
        background: #4a2a28;
        color: #b87060;
        border: solid #4a4038;
    }
    #btn-cancel:hover {
        background: #5a3a38;
    }
    """

    BINDINGS = [
        ("escape", "do_resume", "Resume"),
    ]

    def __init__(self, tool_count: int = 0) -> None:
        super().__init__()
        self._tool_count = tool_count

    def compose(self) -> ComposeResult:
        with Vertical(id="pause-dialog"):
            yield Static("⏸  Agent Paused", id="pause-title")
            yield Static(
                f"Completed {self._tool_count} tool call(s). "
                "The agent is waiting for your input before continuing.",
                id="pause-body",
            )
            yield Input(placeholder="Redirect message (or leave empty to resume)…", id="pause-input")
            yield Static(
                "Enter: resume / send redirect  ·  Escape: resume  ·  Cancel button: stop",
                id="pause-hint",
            )
            with Horizontal(id="pause-buttons"):
                yield Button("▶ Resume", id="btn-resume")
                yield Button("✕ Cancel", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#pause-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-resume":
            self._do_resume()
        elif event.button.id == "btn-cancel":
            self.dismiss("__cancel__")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._do_resume()

    def action_do_resume(self) -> None:
        self._do_resume()

    def _do_resume(self) -> None:
        msg = self.query_one("#pause-input", Input).value.strip()
        self.dismiss(msg if msg else None)
