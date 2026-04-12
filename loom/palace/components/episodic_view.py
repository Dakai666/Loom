"""
EpisodicView — browse session history (episodic memory + session logs).

Key design (mirrors SemanticView):
  - Uses ListView for virtualised rendering.
  - Two modes: session-list mode and turn-detail mode (controlled by
    self._selected_session).
  - Click a session to expand into turn timeline.
  - Batched append via call_next for session list load.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import ListView, ListItem, Static

if TYPE_CHECKING:
    from loom.palace.search import PalaceSearch


@dataclass
class SessionDisplay:
    session_id: str
    title: str
    model: str
    started_at: str
    last_active: str
    turn_count: int


@dataclass
class TurnDisplay:
    turn_index: int
    role: str
    content: str
    created_at: str


class EpisodicView(Vertical):
    """
    Session history explorer.

    Two modes:
      - Session list: shows all sessions, click to expand.
      - Turn timeline: shows turns for a selected session.
    Escape returns to session list.
    """

    DEFAULT_CSS = """
    EpisodicView {
        height: 1fr;
        layout: vertical;
        overflow: hidden;
    }
    EpisodicView ListView {
        height: 1fr;
        background: #0d0a1a;
    }
    """

    class Loaded(Message):
        pass

    def __init__(self, search: "PalaceSearch") -> None:
        super().__init__()
        self._search = search
        self._sessions: list[SessionDisplay] = []
        self._selected_session: str | None = None
        self._turns: list[TurnDisplay] = []
        self._batch_index = 0

    def _render(self) -> "Content":
        from textual.content import Content
        return Content.from_markup("")

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold #d4a853]◌ Episodic Memory[/bold #d4a853]  "
            "[dim]— session history and turns[/dim]",
            id="epi-header",
            classes="content-title",
        )
        yield Static("", id="epi-subtitle", classes="content-subtitle")
        yield ListView(id="epi-list")

    # ── Mount / load ─────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._load_async()

    async def _load_async(self) -> None:
        db = self._search._db
        cursor = await db.execute(
            """
            SELECT session_id, title, model, started_at, last_active, turn_count
            FROM sessions
            ORDER BY last_active DESC
            LIMIT 100
            """
        )
        rows = await cursor.fetchall()
        self._sessions = [
            SessionDisplay(
                session_id=r[0],
                title=r[1] or "(no title)",
                model=r[2],
                started_at=r[3],
                last_active=r[4],
                turn_count=r[5],
            )
            for r in rows
        ]
        self._start_batched_session_load()

    # ── Batched session list population ───────────────────────────────────────

    def _start_batched_session_load(self) -> None:
        lv = self.query_one("#epi-list", ListView)
        lv.clear()
        self._selected_session = None
        self._batch_index = 0

        subtitle = self.query_one("#epi-subtitle", Static)
        subtitle.update(f"[dim]{len(self._sessions)} sessions[/dim]")

        first = min(50, len(self._sessions))
        self._append_session_batch(lv, self._sessions[:first])
        self._batch_index = first

        if self._batch_index < len(self._sessions):
            self._schedule_next_session_batch(lv)
        else:
            self.post_message(self.Loaded())

    def _schedule_next_session_batch(self, lv: ListView) -> None:
        async def _tick() -> None:
            batch_size = 50
            start = self._batch_index
            end   = min(start + batch_size, len(self._sessions))
            self._append_session_batch(lv, self._sessions[start:end])
            self._batch_index = end
            if self._batch_index < len(self._sessions):
                self._schedule_next_session_batch(lv)
            else:
                self.post_message(self.Loaded())

        self.call_next(_tick)

    def _append_session_batch(self, lv: ListView, sessions: list[SessionDisplay]) -> None:
        for s in sessions:
            la = s.last_active[:16].replace("T", " ") if s.last_active else "?"
            label = (
                f"[#60a5fa]{s.session_id[:8]}…[/]  "
                f"[bold #e8deff]{s.title}[/bold #e8deff]\n"
                f"[dim]model: {s.model}  ·  {s.turn_count} turns  ·  last: {la}[/dim]"
            )
            lv.append(ListItem(Static(label), id=f"session-{s.session_id[:40]}"))

    # ── Session detail mode ───────────────────────────────────────────────────

    async def _load_session_detail(self, session_id: str) -> None:
        db = self._search._db
        cursor = await db.execute(
            """
            SELECT role, content, created_at
            FROM session_log
            WHERE session_id = ?
            ORDER BY id ASC
            LIMIT 200
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()

        self._selected_session = session_id
        self._turns = []
        turn_index = -1
        for role, content, created in rows:
            content = str(content or "")
            if not content.strip():
                continue
            if role == "user":
                turn_index += 1
            self._turns.append(TurnDisplay(
                turn_index=turn_index if role == "user" else self._turns[-1].turn_index if self._turns else 0,
                role=role,
                content=content,
                created_at=created or "",
            ))

        lv = self.query_one("#epi-list", ListView)
        lv.clear()

        subtitle = self.query_one("#epi-subtitle", Static)
        subtitle.update(f"[bold #60a5fa]{session_id[:8]}…[/]  [dim]{len(rows)} entries[/dim]  [dim]← back[/dim]")

        lv.append(ListItem(Static("[dim]← back to sessions[/dim]", id="epi-back"), id="epi-back-item"))

        for t in self._turns:
            lv.append(self._build_turn_item(t))
        self.post_message(self.Loaded())

    def _build_turn_item(self, t: TurnDisplay) -> ListItem:
        ts = t.created_at[:16].replace("T", " ") if t.created_at else ""
        if t.role == "user":
            label = (
                f"[dim]── Turn {t.turn_index}  {ts} ──[/dim]\n"
                f"[#fbbf24]you>[/]  [dim]{t.content[:90]}[/dim]"
            )
        elif t.role == "assistant":
            label = f"[#a78bfa]loom>[/]  [dim]{t.content[:110]}[/dim]"
        elif t.role == "tool":
            success = "✓" if "success" in t.content.lower() else "✗"
            color = "#86efac" if success == "✓" else "#f87171"
            label = f"   [dim]⟳[/]  [dim]{t.content[:90]}[/dim]  [{color}]{success}[/{color}]"
        else:
            label = f"[dim]{t.content[:100]}[/dim]"
        return ListItem(Static(label))

    # ── Interaction ───────────────────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        try:
            lv = self.query_one("#epi-list", ListView)
            node = lv.children[lv.index]
            item_id = node.id or ""
        except Exception:
            return

        if self._selected_session is None:
            if item_id.startswith("session-"):
                session_id = item_id[len("session-"):]
                import asyncio as _asyncio
                _asyncio.create_task(self._load_session_detail(session_id))
        else:
            if item_id == "epi-back-item":
                self._start_batched_session_load()

    def back_to_sessions(self) -> None:
        if self._selected_session is not None:
            self._start_batched_session_load()

    def reload(self) -> None:
        self._load_async()