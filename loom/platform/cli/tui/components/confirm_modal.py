"""
ConfirmModal — TUI dialog for GUARDED/CRITICAL tool confirmation.

Replaces the raw-terminal prompt with a proper Textual ModalScreen.
"""

from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConfirmModal(ModalScreen[bool]):
    """
    Modal confirmation dialog for tool calls requiring user approval.
    Returns True (Allow) or False (Deny) via dismiss().
    """

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }

    /* Border / title colour is overridden at runtime via inline styles when CRITICAL */
    #confirm-dialog {
        background: #242018;
        border: thick #c8924a;
        padding: 1 2;
        width: 64;
        height: auto;
    }

    #confirm-title {
        color: #c8924a;
        text-style: bold;
        margin-bottom: 1;
    }

    #confirm-dialog.critical {
        border: thick #b87060;
    }

    #confirm-dialog.critical #confirm-title {
        color: #b87060;
    }

    #confirm-tool {
        color: #e0cfa0;
        margin-bottom: 0;
    }

    #confirm-args {
        color: #8a7a5e;
        margin-bottom: 1;
    }

    #confirm-buttons {
        align: center middle;
        height: 3;
        margin-top: 1;
    }

    #allow-btn {
        background: #7a9e78;
        color: #1c1814;
        border: solid #4a4038;
        min-width: 14;
    }
    #allow-btn:hover {
        background: #9abf98;
    }

    #deny-btn {
        background: #b87060;
        color: #1c1814;
        border: solid #4a4038;
        min-width: 14;
    }
    #deny-btn:hover {
        background: #d09080;
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
        self._trust_label = trust_label   # plain text: "SAFE" / "GUARDED" / "CRITICAL"
        self._args_preview = args_preview

    def _trust_colour(self) -> str:
        return {
            "SAFE":     "#7a9e78",   # sage green
            "GUARDED":  "#c8924a",   # ochre
            "CRITICAL": "#b87060",   # terracotta
        }.get(self._trust_label, "#c8924a")

    def compose(self) -> ComposeResult:
        colour = self._trust_colour()
        css_class = "critical" if self._trust_label == "CRITICAL" else ""
        with Vertical(id="confirm-dialog", classes=css_class):
            yield Static(
                "  Tool requires confirmation",
                id="confirm-title",
            )
            yield Static(
                f"[bold]{escape(self._tool_name)}[/bold]  "
                f"[bold {colour}]{self._trust_label}[/bold {colour}]",
                id="confirm-tool",
            )
            yield Static(
                f"[dim]{escape(self._args_preview)}[/dim]",
                id="confirm-args",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Allow  [Y]", id="allow-btn")
                yield Button("Deny   [N]", id="deny-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "allow-btn")

    def action_allow(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)
