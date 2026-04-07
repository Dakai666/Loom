"""
Header component — top bar with session info.

Layout (horizontal, 2 columns):
  [Loom v0.3]  |  model  persona  memory
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class Header(Widget):
    """
    Top-of-screen header showing Loom branding and session info.
    Agent activity is shown inline in the message stream and ToolBlock.
    """

    DEFAULT_CSS = """
    Header {
        layout: horizontal;
        height: 1;
        background: #1c1814;
        border-bottom: solid #4a4038;
        padding: 0 1;
    }
    #header-logo {
        width: auto;
        content-align: left middle;
    }
    #header-hints {
        width: 1fr;
        padding: 0 4;
        content-align: center middle;
    }
    #header-info {
        width: auto;
        content-align: right middle;
    }
    """

    model: reactive[str] = reactive("")
    personality: reactive[str | None] = reactive(None)
    db_path: reactive[str] = reactive("")

    def __init__(self, model: str = "", db_path: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.model = model
        self.db_path = db_path

    def compose(self) -> ComposeResult:
        logo = "[bold #c8a464]❖ LOOM[/bold #c8a464] [dim]v0.4[/dim]"
        yield Static(logo, id="header-logo")
        yield Static(
            "[dim]F1: [white]Cmds[/white]  ·  F2: [white]Tabs[/white]  ·  F4: [white]Sidebar[/white]  ·  F5: [bold #d4a853]Time-Travel[/bold #d4a853][/dim]",
            id="header-hints"
        )
        yield Static("", id="header-info")

    def on_mount(self) -> None:
        self._update_info()

    def watch_model(self, _: str) -> None:
        self._update_info()

    def watch_personality(self, _: str | None) -> None:
        self._update_info()

    def _update_info(self) -> None:
        from textual.css.query import NoMatches

        try:
            info = self.query_one("#header-info", Static)
        except NoMatches:
            return

        parts = []
        if self.model:
            parts.append(f"[#a0b898]{self.model}[/#a0b898]")
        if self.personality:
            parts.append(f"[dim]persona: {self.personality}[/dim]")
        if self.db_path:
            abbrev = self.db_path.split("/")[-1].split("\\")[-1]
            parts.append(f"[dim]{abbrev}[/dim]")
        info.update("  [dim]·[/dim]  ".join(parts) if parts else "")
