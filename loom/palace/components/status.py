"""
PalaceStatusBar — bottom status line for Memory Palace.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static


class PalaceStatusBar(Widget):
    """
    Bottom dock showing live memory stats summary.
    Updated whenever a section is selected or data changes.
    """

    DEFAULT_CSS = '''
    PalaceStatusBar {
        dock: bottom;
        height: 1;
        background: #150d28;
        border-top: solid #3d2a6b;
        padding: 0 1;
    }
    #status-text {
        color: #9b87b5;
    }
    '''

    class StatusUpdated(Message, bubble=True):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def compose(self) -> ComposeResult:
        yield Static("[dim]Loading memory palace...[/dim]", id="status-text")

    def update(
        self,
        semantic: int = 0,
        relational: int = 0,
        skills: int = 0,
        sessions: int = 0,
        active_section: str = "semantic",
    ) -> None:
        parts = [
            "[#a78bfa]Semantic[/]  [dim]·[/dim]",
            f"[#e8deff]{semantic:,}[/#e8deff] facts",
            "[dim]·[/dim]",
            "[#c084fc]Relational[/]  [dim]·[/dim]",
            f"[#e8deff]{relational:,}[/#e8deff] triples",
            "[dim]·[/dim]",
            "[#d4a853]Skills[/]  [dim]·[/dim]",
            f"[#e8deff]{skills:,}[/#e8deff] genomes",
            "[dim]·[/dim]",
            "[#60a5fa]Sessions[/]  [dim]·[/dim]",
            f"[#e8deff]{sessions:,}[/#e8deff] logged",
            "[dim]  ·  [/dim]",
            f"[dim]View: {active_section}[/dim]",
        ]
        text = "  ".join(parts)
        try:
            self.query_one("#status-text", Static).update(text)
        except Exception:
            pass
