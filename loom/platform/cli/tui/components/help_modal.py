"""
HelpModal — TUI overlay showing all slash commands and key bindings.

Triggered by /help.  Escape or Enter closes it.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


# Commands table: (command, description)
_COMMANDS = [
    ("/new",                    "Start a fresh session"),
    ("/sessions",               "Browse and switch sessions"),
    ("/model",                  "Show current model + registered providers"),
    ("/model <name>",           "Switch model  e.g. deepseek-v4-pro  claude-sonnet-4-6"),
    ("/personality [name]",     "Switch cognitive persona"),
    ("/personality off",        "Remove active persona"),
    ("/compact",                "Compress older context (frees tokens)"),
    ("/auto",                   "Toggle run_bash auto-approve (requires strict_sandbox)"),
    ("/pause",                  "Toggle HITL auto-pause after each tool batch"),
    ("/stop",                   "Immediately cancel the current running turn"),
    ("/scope",                  "List active scope grants (leases)"),
    ("/scope revoke <N>",       "Revoke a specific grant"),
    ("/scope clear",            "Revoke all non-system grants"),
    ("/help",                   "Show this help"),
]

_KEYS = [
    ("Enter",       "Send message"),
    ("Ctrl+J",      "Insert newline (reliable on macOS Terminal)"),
    ("Alt+Enter / Shift+Enter", "Insert newline (iTerm2, VS Code terminal)"),
    ("Tab",         "Complete slash command"),
    ("Up / Down",   "Recall previously sent messages (when input is empty)"),
    ("Click 📋 copy", "Copy a message to clipboard (per-bubble indicator)"),
    ("Escape",      "Interrupt current generation"),
    ("Ctrl+S",      "Browse & switch sessions"),
    ("Ctrl+L",      "Clear conversation view"),
    ("F1 / Ctrl+K", "Command Palette — theme, message search, system ops"),
    ("F2",          "Cycle workspace tab  Art → Exe → Bgt"),
    ("F4 / Ctrl+B", "Toggle right sidebar"),
    ("F5",          "Time-Travel — fork conversation at any past turn"),
    ("Ctrl+C",      "Quit Loom"),
]


def _section(title: str, rows: list[tuple[str, str]], cmd_width: int = 26) -> str:
    lines = [f"[bold #c8a464]{title}[/bold #c8a464]\n"]
    for cmd, desc in rows:
        pad = cmd_width - len(cmd)
        lines.append(
            f"  [#d4a853]{cmd}[/#d4a853]{'  ' + ' ' * max(0, pad)}"
            f"[dim]{desc}[/dim]"
        )
    return "\n".join(lines)


class HelpModal(ModalScreen[None]):
    """
    Slash-command and keybinding reference overlay.
    Parchment palette; Escape or Enter or the Close button dismisses it.
    """

    DEFAULT_CSS = """
    HelpModal {
        align: center middle;
    }

    #help-dialog {
        background: #242018;
        border: thick #4a4038;
        padding: 1 2;
        width: 78;
        height: auto;
        max-height: 44;
    }

    #help-title {
        color: #c8a464;
        text-style: bold;
        margin-bottom: 1;
        border-bottom: solid #4a4038;
        padding-bottom: 1;
    }

    #help-body {
        height: auto;
        margin-bottom: 1;
    }

    #help-close-row {
        align: center middle;
        height: 3;
        margin-top: 1;
        border-top: solid #4a4038;
        padding-top: 1;
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

    def compose(self) -> ComposeResult:
        body = (
            _section("Slash commands", _COMMANDS, cmd_width=28)
            + "\n\n"
            + _section("Keyboard shortcuts", _KEYS, cmd_width=18)
        )
        with Vertical(id="help-dialog"):
            yield Static("  Loom — command reference", id="help-title")
            yield Static(body, id="help-body")
            with Vertical(id="help-close-row"):
                yield Button("Close  [Esc]", id="close-btn")

    def on_button_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
