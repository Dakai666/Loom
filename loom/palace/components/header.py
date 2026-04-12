"""
PalaceHeader — top bar for the Memory Palace TUI.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static


class PalaceHeader(Widget):
    """
    Top-of-screen header for the Memory Palace.

    Shows ❖ Memory Palace branding with gold accent and a hint line.
    """

    DEFAULT_CSS = """
    PalaceHeader {
        dock: top;
        height: 2;
        background: #150d28;
        border-bottom: solid #3d2a6b;
    }
    #header-title {
        width: auto;
        content-align: left middle;
        padding: 0 1;
    }
    #header-hints {
        width: 1fr;
        content-align: right middle;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold #d4a853]❖[/bold #d4a853]  [bold #e8deff]Memory Palace[/bold #e8deff]  [dim]v0.4[/dim]",
            id="header-title",
        )
        yield Static(
            "[dim]↑↓[/dim] Navigate  [dim]·[/dim] [dim]Enter[/dim] Expand  [dim]·[/dim] [dim]Esc[/dim] Back  [dim]·[/dim] [dim]Ctrl+F[/dim] Search  [dim]·[/dim] [dim]Ctrl+Q[/dim] Quit",
            id="header-hints",
        )
