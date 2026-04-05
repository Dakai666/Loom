from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static, Label
from textual.widgets.option_list import Option
from textual.containers import Vertical
from rich.text import Text

class MiniMapModal(ModalScreen[int | None]):
    DEFAULT_CSS = """
    MiniMapModal {
        align: center middle;
        background: #1c1814 60%;
    }
    #minimap-dialog {
        width: 80%;
        height: 80%;
        background: #242018;
        border: solid #c8a464;
        padding: 1 2;
    }
    #minimap-title {
        color: #c8a464;
        text-style: bold;
        margin-bottom: 1;
        content-align: center middle;
    }
    OptionList {
        border: solid #4a4038;
        background: #1c1814;
        height: 1fr;
    }
    """

    def __init__(self, turns_data: list[tuple[int, str]]) -> None:
        super().__init__()
        self.turns_data = turns_data

    def compose(self) -> ComposeResult:
        with Vertical(id="minimap-dialog"):
            yield Label("⏳ Time-Travel Conversation Map", id="minimap-title")
            yield Label("[dim]Select a point in history to branch off a new timeline.[/dim]", id="minimap-subtitle")
            
            options = []
            for t_idx, summary in self.turns_data:
                options.append(Option(summary, id=f"turn_{t_idx}"))
                
            yield OptionList(*options, id="minimap-list")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        turn_id = event.option.id
        if turn_id and turn_id.startswith("turn_"):
            self.dismiss(int(turn_id.split("_")[1]))

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()
        
    def key_escape(self) -> None:
        self.dismiss(None)
