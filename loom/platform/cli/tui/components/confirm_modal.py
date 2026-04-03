"""
ConfirmModal — TUI dialog for GUARDED/CRITICAL tool confirmation.

Replaces the raw-terminal prompt+suspend approach with a proper
Textual ModalScreen so the user never leaves the TUI.
"""

from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConfirmModal(ModalScreen[bool]):
    """
    Modal confirmation dialog for tool calls that require user approval.

    Returns True (Allow) or False (Deny) via dismiss().

    Layout:
        ┌─ Tool requires confirmation ──────────────────┐
        │                                               │
        │  write_file  [yellow]GUARDED[/yellow]         │
        │  path="/some/path.py"  content="..."          │
        │                                               │
        │        [ Allow ]        [ Deny ]              │
        └───────────────────────────────────────────────┘
    """

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }

    #confirm-dialog {
        background: $surface;
        border: thick $warning;
        padding: 1 2;
        width: 64;
        height: auto;
    }

    #confirm-title {
        color: $warning;
        text-style: bold;
        margin-bottom: 1;
    }

    #confirm-tool {
        margin-bottom: 0;
    }

    #confirm-args {
        margin-bottom: 1;
    }

    #confirm-buttons {
        align: center middle;
        height: 3;
        margin-top: 1;
    }

    Button {
        margin: 0 2;
        min-width: 12;
    }
    """

    BINDINGS = [
        ("y", "allow", "Allow"),
        ("n", "deny", "Deny"),
        ("escape", "deny", "Deny"),
    ]

    def __init__(self, tool_name: str, trust_label: str, args_preview: str) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._trust_label = trust_label
        self._args_preview = args_preview

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(
                "  Tool requires confirmation",
                id="confirm-title",
            )
            yield Static(
                f"[bold]{escape(self._tool_name)}[/bold]  {self._trust_label}",
                id="confirm-tool",
            )
            yield Static(
                f"[dim]{escape(self._args_preview)}[/dim]",
                id="confirm-args",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Allow  [Y]", id="allow-btn", variant="success")
                yield Button("Deny   [N]", id="deny-btn", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "allow-btn")

    def action_allow(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)
