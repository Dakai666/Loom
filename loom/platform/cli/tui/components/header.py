"""
Header component — top bar with session info.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class Header(Widget):
    """
    Top-of-screen header showing Loom branding and session info.

    Shows:
    - Loom logo + version
    - Model name
    - Active personality
    - Memory DB path (abbreviated)
    """

    COMPONENT_CLASSES = {"header-logo", "header-info"}

    model: reactive[str] = reactive("")
    personality: reactive[str | None] = reactive(None)
    db_path: reactive[str] = reactive("")

    def __init__(self, model: str = "", db_path: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.model = model
        self.db_path = db_path

    def compose(self) -> ComposeResult:
        logo = Static(
            "[bold cyan]Loom[/bold cyan][dim] v0.3.0[/dim]",
            id="header-logo",
        )
        info = Static("", id="header-info")
        yield logo
        yield info

    def on_mount(self) -> None:
        self._update_info()

    def watch_model(self, model: str) -> None:
        self._update_info()

    def watch_personality(self, personality: str | None) -> None:
        self._update_info()

    def _update_info(self) -> None:
        from textual.css.query import NoMatches

        try:
            info = self.query_one("#header-info", Static)
        except NoMatches:
            return  # called before compose() — skip until mounted
        parts = []
        if self.model:
            parts.append(f"[green]{self.model}[/green]")
        if self.personality:
            parts.append(f"[dim]persona: {self.personality}[/dim]")
        if self.db_path:
            abbrev = self.db_path.split("/")[-1].split("\\")[-1]
            parts.append(f"[dim]memory: {abbrev}[/dim]")
        info.update("  |  ".join(parts) if parts else "")
