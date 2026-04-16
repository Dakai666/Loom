"""
SessionPickerModal — in-TUI session browser.

Shows up to 20 recent sessions with title, model, date, and turn count.
User selects one with Up/Down + Enter (or clicks); modal returns the
session_id string (or None if cancelled).

Edit mode (L2, Issue #126):
  - Press 'e' on a selected row to rename that session.
  - The DataTable is replaced with a TextArea pre-filled with the current title.
  - Press Enter to save (calls update_title_fn), Escape to cancel.
  - On save the modal dismisses with the session_id so the caller can refresh.
"""

from __future__ import annotations

from typing import Any, Callable, Awaitable

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static, TextArea


class SessionPickerModal(ModalScreen[str | None]):
    """
    Modal session picker.

    Returns the selected session_id, or None if the user presses Escape.
    When the user edits a title with 'e', the modal dismisses with the
    session_id after persisting the change so the caller can refresh.

    Parameters
    ----------
    sessions:
        List of dicts from ``SessionLog.list_sessions()``.
    update_title_fn:
        Async callback ``fn(session_id, new_title)`` called when the user
        saves an edited title.  If omitted, title edits are visual-only.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "select", "Open"),
        Binding("e", "edit_title", "Rename"),
    ]

    def __init__(
        self,
        sessions: list[dict[str, Any]],
        update_title_fn: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self._sessions = sessions
        self._update_title_fn = update_title_fn
        self._edit_mode = False
        self._edit_text_area: TextArea | None = None
        self._editing_session_id: str | None = None

    # ------------------------------------------------------------------
    # Mount / compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Static("  Sessions", id="picker-title")
            yield DataTable(id="session-table", cursor_type="row", zebra_stripes=True)
            yield Static(
                "[dim]Enter[/dim] open  "
                "[dim]e[/dim] rename  "
                "[dim]Escape[/dim] cancel",
                id="picker-hint",
            )

    def on_mount(self) -> None:
        self._populate_table(self.query_one("#session-table", DataTable))

    def _populate_table(self, table: DataTable) -> None:
        table.add_columns("ID", "Title", "Model", "Turns", "Last active")
        for row in self._sessions:
            sid = row.get("session_id", "")
            title = (row.get("title") or "(untitled)")[:30]
            model = (row.get("model") or "")[:18]
            turns = str(row.get("turn_count", 0))
            last = (row.get("last_active") or "")[:16].replace("T", " ")
            table.add_row(
                escape(sid), escape(title), escape(model), turns, last, key=sid,
            )
        if self._sessions:
            table.move_cursor(row=0)
        table.focus()

    # ------------------------------------------------------------------
    # Edit mode (L2) — triggered by 'e' binding
    # ------------------------------------------------------------------

    def action_edit_title(self) -> None:
        """Replace the DataTable with a TextArea for title editing."""
        if self._edit_mode:
            return
        table: DataTable | None = self.query_one("#session-table", DataTable, None)
        if table is None or table.cursor_row is None:
            return

        row_data = table.get_row_at(table.cursor_row)
        if not row_data:
            return

        sid = row_data[0]   # session_id column
        old_title = row_data[1] if len(row_data) > 1 else ""

        self._edit_mode = True
        self._editing_session_id = sid

        # Swap table → TextArea
        if table.parent:
            table.remove()
        self._edit_text_area = TextArea(
            text=old_title,
            id="title-editor",
            multiline=False,
        )
        hint: Static = self.query_one("#picker-hint", Static)
        hint.update(
            "[dim]Enter[/dim] save  "
            "[dim]Escape[/dim] cancel edit"
        )
        dialog: Vertical = self.query_one("#picker-dialog", Vertical)
        dialog.mount(self._edit_text_area)
        self._edit_text_area.focus()

    async def _submit_edit(self) -> None:
        """Save the edited title, update the in-memory row, dismiss."""
        sid = self._editing_session_id or ""
        new_title = (
            self._edit_text_area.text.strip()
            if self._edit_text_area
            else ""
        )

        self._teardown_edit_mode()

        # Persist via callback
        if new_title and sid and self._update_title_fn:
            try:
                await self._update_title_fn(sid, new_title)
            except Exception:
                pass  # Best-effort — session can still be selected

        # Update in-memory title so caller sees fresh data on next open
        for row in self._sessions:
            if row.get("session_id") == sid:
                row["title"] = new_title
                break

        self.dismiss(str(sid))

    def _cancel_edit(self) -> None:
        """Remove the TextArea and restore browse mode."""
        self._teardown_edit_mode()
        # Re-mount DataTable
        table = DataTable(id="session-table", cursor_type="row", zebra_stripes=True)
        dialog: Vertical = self.query_one("#picker-dialog", Vertical)
        dialog.mount(table)
        self._populate_table(table)

    def _teardown_edit_mode(self) -> None:
        """Remove TextArea and reset edit state."""
        if self._edit_text_area is not None:
            self._edit_text_area.remove()
            self._edit_text_area = None
        self._edit_mode = False
        self._editing_session_id = None
        hint: Static = self.query_one("#picker-hint", Static)
        hint.update(
            "[dim]Enter[/dim] open  "
            "[dim]e[/dim] rename  "
            "[dim]Escape[/dim] cancel"
        )

    # ------------------------------------------------------------------
    # TextArea key handling (edit mode)
    # ------------------------------------------------------------------

    def on_text_area_submitted(self, event: TextArea.Submitted) -> None:
        """Enter pressed in TextArea → save title and dismiss."""
        if not self._edit_mode:
            return
        event.stop()

        # Capture values synchronously before DOM changes
        sid = self._editing_session_id or ""
        new_title = (
            self._edit_text_area.text.strip()
            if self._edit_text_area
            else ""
        )
        callback = self._update_title_fn

        # Reset edit state immediately so _submit_edit cannot race with itself
        self._edit_mode = False
        self._editing_session_id = None
        if self._edit_text_area is not None:
            self._edit_text_area.remove()
            self._edit_text_area = None
        hint: Static = self.query_one("#picker-hint", Static)
        hint.update(
            "[dim]Enter[/dim] open  "
            "[dim]e[/dim] rename  "
            "[dim]Escape[/dim] cancel"
        )

        # Schedule the async DB write + dismiss in the next event-loop tick.
        # This avoids any race with TextArea widget teardown.
        async def _deferred() -> None:
            try:
                if new_title and sid and callback:
                    await callback(sid, new_title)
                    # Update in-memory title so next open shows fresh data
                    for row in self._sessions:
                        if row.get("session_id") == sid:
                            row["title"] = new_title
                            break
                    self.app.notify(f"Title updated \u2192 {new_title}", timeout=4)  # type: ignore[attr-defined]
            except Exception:
                pass
            finally:
                self.dismiss(str(sid))

        self.app.call_later(_deferred)  # type: ignore[attr-defined]

    def on_key(self, event: Key) -> None:
        """Route Escape in edit mode to cancel."""
        if (
            self._edit_mode
            and self._edit_text_area is not None
            and event.key == "escape"
        ):
            event.stop()
            self._cancel_edit()

    # ------------------------------------------------------------------
    # Normal mode actions
    # ------------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.dismiss(str(event.row_key.value))

    def action_cancel(self) -> None:
        if self._edit_mode:
            self._cancel_edit()
        else:
            self.dismiss(None)

    def action_select(self) -> None:
        if self._edit_mode:
            return
        table: DataTable | None = self.query_one("#session-table", DataTable, None)
        if table is not None and table.cursor_row is not None:
            key = table.get_row_at(table.cursor_row)[0]
            self.dismiss(str(key))
        else:
            self.dismiss(None)
