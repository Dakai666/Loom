"""
SemanticView — browse and search semantic memory facts.

Key performance design:
  - Uses Textual ListView (virtualised — only renders visible items)
  - Batched append: first 100 shown immediately, then 50 items per
    10ms tick via call_next so the UI stays responsive during load.
  - Search filters are applied in-memory (data is already loaded).
  - _render() override prevents None-visual errors when hidden.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import ListView, ListItem, Static

if TYPE_CHECKING:
    from loom.palace.search import PalaceSearch


SortKey = Literal["updated", "confidence", "key"]


@dataclass
class SemanticEntryDisplay:
    key: str
    value: str
    confidence: float
    source: str
    updated_at: str
    history_count: int = 0


class SemanticView(Vertical):
    """
    Main semantic memory explorer with virtualised list rendering.

    Layout (top to bottom):
        Title line
        Subtitle / stats line (live-updated as batches load)
        ListView — virtualised, infinite scroll, click to select
    """

    DEFAULT_CSS = """
    SemanticView {
        height: 1fr;
        layout: vertical;
        overflow: hidden;
    }
    SemanticView ListView {
        height: 1fr;
        background: #0d0a1a;
    }
    """

    # ── Messages ─────────────────────────────────────────────────────────────

    class Loaded(Message):
        """Fired when all entries have been loaded into the list."""
        pass

    class BatchLoaded(Message):
        """Fired after each batch is appended.  `total` is current count."""
        def __init__(self, total: int) -> None:
            super().__init__()
            self.total = total

    # ── Init ────────────────────────────────────────────────────────────────

    def __init__(self, search: "PalaceSearch") -> None:
        super().__init__()
        self._search = search
        self._mode: str = "browse"    # browse | search | health
        self._sort_key: SortKey = "updated"
        self._all_entries: list[SemanticEntryDisplay] = []
        self._filtered: list[SemanticEntryDisplay] = []
        self._loading = False
        self._batch_entries: list[SemanticEntryDisplay] = []
        self._batch_index = 0

    def _render(self) -> "Content":
        """Return empty Content to prevent None-visual when hidden."""
        from textual.content import Content
        return Content.from_markup("")

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold #d4a853]◈ Semantic Memory[/bold #d4a853]  "
            "[dim]— facts distilled from experience[/dim]",
            id="semantic-title",
        )
        yield Static("[dim]Loading…[/dim]", id="semantic-subtitle", classes="content-subtitle")
        yield ListView(id="semantic-list")

    # ── Mount → load ─────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._load_async()

    async def _load_async(self) -> None:
        if self._loading:
            return
        self._loading = True

        try:
            rows = await self._fetch_rows()
            self._all_entries = self._rows_to_entries(rows)
            self._apply_filter_and_load()
        finally:
            self._loading = False

    async def _fetch_rows(self) -> list[tuple]:
        """Fetch all semantic rows from DB (up to 2000, enough for browse)."""
        db = self._search._db
        cursor = await db.execute(
            """
            SELECT key, value, confidence, source, metadata, updated_at
            FROM semantic_entries
            ORDER BY updated_at DESC
            LIMIT 2000
            """
        )
        return await cursor.fetchall()

    @staticmethod
    def _rows_to_entries(rows: list[tuple]) -> list[SemanticEntryDisplay]:
        import json
        entries = []
        for r in rows:
            meta = json.loads(r[4]) if r[4] else {}
            history = meta.get("history", []) if isinstance(meta, dict) else []
            entries.append(SemanticEntryDisplay(
                key=r[0],
                value=r[1],
                confidence=float(r[2]),
                source=r[3] or "",
                updated_at=r[5],
                history_count=len(history) if isinstance(history, list) else 0,
            ))
        return entries

    # ── Batched list population ───────────────────────────────────────────────

    def _apply_filter_and_load(self) -> None:
        """Sort + filter entries then start batch-loading the ListView."""
        entries = self._all_entries

        if self._mode == "health":
            entries = [e for e in entries if e.confidence < 0.3]

        if self._sort_key == "confidence":
            entries = sorted(entries, key=lambda e: e.confidence, reverse=True)
        elif self._sort_key == "key":
            entries = sorted(entries, key=lambda e: e.key)
        else:  # updated
            entries = sorted(entries, key=lambda e: e.updated_at, reverse=True)

        self._filtered = entries
        self._start_batched_load(entries)

    def _start_batched_load(self, entries: list[SemanticEntryDisplay]) -> None:
        """Load entries in batches: first 100 immediately, then 50 every ~10ms."""
        lv = self.query_one("#semantic-list", ListView)
        lv.clear()
        self._batch_index = 0
        self._batch_entries = entries

        # First batch immediately
        first = min(100, len(entries))
        self._append_batch(lv, entries[:first])
        self._batch_index = first
        self._update_subtitle(len(entries), loaded=first)

        # Remaining batches on a timer
        if self._batch_index < len(entries):
            self._schedule_next_batch(lv)

    def _schedule_next_batch(self, lv: ListView) -> None:
        """Fire next batch via call_next, then chain until done."""
        async def _tick() -> None:
            batch_size = 50
            start = self._batch_index
            end   = min(start + batch_size, len(self._batch_entries))
            self._append_batch(lv, self._batch_entries[start:end])
            self._batch_index = end
            self._update_subtitle(len(self._batch_entries), loaded=end)

            if self._batch_index < len(self._batch_entries):
                self._schedule_next_batch(lv)
            else:
                self.post_message(self.Loaded())

        self.call_next(_tick)

    def _append_batch(self, lv: ListView, entries: list[SemanticEntryDisplay]) -> None:
        """Build ListItem cards and append to ListView in one call."""
        for e in entries:
            card = self._build_card(e)
            lv.append(card)

    def _update_subtitle(self, total: int, loaded: int) -> None:
        try:
            sub = self.query_one("#semantic-subtitle", Static)
            mode_tag = {"browse": "", "search": "  [dim]filtered[/dim]", "health": "  [dim]⚠ low confidence[/dim]"}[self._mode]
            if loaded < total:
                sub.update(f"[dim]{loaded}/{total} loaded  ·  sort: {self._sort_key}{mode_tag}[/dim]")
            else:
                sub.update(f"[dim]{total} facts  ·  sort: {self._sort_key}{mode_tag}[/dim]")
        except Exception:
            pass

    # ── Card builder ─────────────────────────────────────────────────────────

    def _build_card(self, e: SemanticEntryDisplay) -> ListItem:
        conf_color = (
            "#a78bfa" if e.confidence > 0.7 else
            "#c084fc" if e.confidence >= 0.4 else
            "#7c3aed"
        )
        src = e.source or "unknown"
        if src.startswith("session:"):
            src = "session:*"
        elif len(src) > 15:
            src = src[:12] + "…"

        value_short = e.value[:110] + ("…" if len(e.value) > 110 else "")
        hist = f"  [dim]↺ {e.history_count}[/dim]" if e.history_count > 0 else ""

        label = (
            f"[#a78bfa]{e.key}[/#a78bfa]  "
            f"[dim]{e.updated_at[:16].replace('T',' ')}[/dim]  "
            f"[{conf_color}]{e.confidence:.2f}[/{conf_color}]  "
            f"[dim]src: {src}[/dim][dim]{hist}[/dim]\n"
            f"[#e8deff]{value_short}[/#e8deff]"
        )
        return ListItem(Static(label), id=f"fact-{e.key[:40]}")

    # ── Filter / sort control ───────────────────────────────────────────────

    def set_mode(self, mode: str) -> None:
        if mode not in {"browse", "search", "health"}:
            return
        self._mode = mode
        self._apply_filter_and_load()

    def set_sort(self, key: SortKey) -> None:
        self._sort_key = key
        self._apply_filter_and_load()

    def filter_by_query(self, query: str) -> None:
        """In-memory filter by substring match on key or value."""
        if not query.strip():
            self._apply_filter_and_load()
            return
        q = query.lower()
        filtered = [
            e for e in self._all_entries
            if q in e.key.lower() or q in e.value.lower()
        ]
        self._filtered = filtered
        lv = self.query_one("#semantic-list", ListView)
        lv.clear()
        batch = filtered[:100]
        for e in batch:
            lv.append(self._build_card(e))
        self._update_subtitle(len(filtered), loaded=len(batch))
        if len(filtered) > 100:
            self._batch_index = 100
            self._batch_entries = filtered
            self._schedule_next_batch(lv)
        else:
            self.post_message(self.Loaded())

    def reload(self) -> None:
        """Cancel any in-flight load and restart."""
        self._loading = False
        self._all_entries = []
        self._filtered = []
        self._batch_entries = []
        self._load_async()