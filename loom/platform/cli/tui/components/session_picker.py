"""
SessionPickerModal — in-TUI session browser.

Shows up to 20 recent sessions with title, model, date, and turn count.
User selects one with Up/Down + Enter (or clicks); modal returns the
session_id string (or None if cancelled).
"""

from __future__ import annotations

from typing import Any

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static


class SessionPickerModal(ModalScreen[str | None]):
    """
    Modal session picker.

    Returns the selected session_id, or None if the user presses Escape.

    Usage:
        result = await app.push_screen_wait(SessionPickerModal(rows))
        if result:
            # restart with session result
    """

    DEFAULT_CSS = """
    SessionPickerModal {
        align: center middle;
    }

    #picker-dialog {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 80;
        height: 24;
    }

    #picker-title {
        color: $primary;
        text-style: bold;
        margin-bottom: 1;
    }

    #picker-hint {
        color: $text-muted;
        margin-top: 1;
    }

    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "select", "Open"),
    ]

    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        super().__init__()
        self._sessions = sessions  # list of dicts from SessionLog.list_sessions()

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Static("  Sessions", id="picker-title")
            yield DataTable(id="session-table", cursor_type="row", zebra_stripes=True)
            yield Static(
                "[dim]Enter[/dim] open  [dim]Escape[/dim] cancel",
                id="picker-hint",
            )

    def on_mount(self) -> None:
        table: DataTable = self.query_one("#session-table", DataTable)
        table.add_columns("ID", "Title", "Model", "Turns", "Last active")
        for row in self._sessions:
            sid = row.get("session_id", "")
            title = (row.get("title") or "(untitled)")[:30]
            model = (row.get("model") or "")[:18]
            turns = str(row.get("turn_count", 0))
            last = (row.get("last_active") or "")[:16].replace("T", " ")
            table.add_row(
                escape(sid),
                escape(title),
                escape(model),
                turns,
                last,
                key=sid,
            )
        if self._sessions:
            table.move_cursor(row=0)
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.dismiss(str(event.row_key.value))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_select(self) -> None:
        table: DataTable = self.query_one("#session-table", DataTable)
        if table.cursor_row is not None:
            key = table.get_row_at(table.cursor_row)[0]  # ID column
            self.dismiss(str(key))
        else:
            self.dismiss(None)
