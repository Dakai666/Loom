"""
Header component — top bar with session info and agent activity status.

Layout (horizontal, 3 columns):
  [Loom v0.3]  |  ◌ Thinking... (centre)  |  model  persona  memory
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class Header(Widget):
    """
    Top-of-screen header showing Loom branding, live agent status, and session info.
    """

    DEFAULT_CSS = """
    Header {
        layout: horizontal;
        height: 3;
        background: #1c1814;
        border-bottom: solid #4a4038;
    }
    #header-logo {
        width: auto;
        padding: 1 2;
        content-align: left middle;
    }
    #header-status {
        width: 1fr;
        content-align: center middle;
    }
    #header-info {
        width: auto;
        padding: 1 2;
        content-align: right middle;
    }
    """

    model: reactive[str] = reactive("")
    personality: reactive[str | None] = reactive(None)
    db_path: reactive[str] = reactive("")
    agent_status: reactive[str] = reactive("")  # e.g. "◌ Thinking..." / "⟳ read_file"

    def __init__(self, model: str = "", db_path: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.model = model
        self.db_path = db_path

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold #c8a464]Loom[/bold #c8a464][dim] v0.3[/dim]",
            id="header-logo",
        )
        yield Static("", id="header-status")
        yield Static("", id="header-info")

    def on_mount(self) -> None:
        self._update_info()

    def watch_model(self, _: str) -> None:
        self._update_info()

    def watch_personality(self, _: str | None) -> None:
        self._update_info()

    def watch_agent_status(self, status: str) -> None:
        from textual.css.query import NoMatches
        try:
            self.query_one("#header-status", Static).update(status)
        except NoMatches:
            pass

    def set_thinking(self) -> None:
        """Show thinking indicator (call from app when TurnStart fires)."""
        self.agent_status = "[dim]◌ Thinking[/dim]"

    def set_running(self, tool_name: str) -> None:
        """Show tool-running indicator."""
        from rich.markup import escape as markup_escape
        self.agent_status = f"[#c8a464]⟳ {markup_escape(tool_name)}[/#c8a464]"

    def set_ready(self) -> None:
        """Clear status (idle)."""
        self.agent_status = ""

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
