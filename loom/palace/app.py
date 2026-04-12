"""
PalaceApp — main Textual application for the Memory Palace TUI.

Independent from the Chat TUI; uses a purple theme.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from .theme import CSS
from .search import PalaceSearch
from .components import (
    PalaceHeader,
    NavSidebar,
    PalaceStatusBar,
    SemanticView,
    HealthView,
    RelationalView,
    EpisodicView,
    SkillsView,
)
from .components.nav import PalaceSection


if TYPE_CHECKING:
    import aiosqlite


class PalaceApp(App):
    """
    Memory Palace — interactive TUI for exploring Loom's memory store.

    Layout:
        PalaceHeader        (dock top, 2 rows)
        Horizontal body
            NavSidebar       (38 cols)
            #content-area    (1fr) — contains the active view
        PalaceStatusBar     (dock bottom, 1 row)

    Bindings:
        1-5:    switch section
        Ctrl+F: focus search
        Esc:    reset / back to semantic
        Ctrl+R: refresh
        Ctrl+Q: quit
    """

    CSS = CSS

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True, priority=True),
        Binding("escape", "reset_view", "Back", show=True),
        Binding("ctrl+f", "focus_search", "Search", show=True),
        Binding("ctrl+r", "refresh", "Refresh", show=True),
        Binding("1", "section_1", "", show=False),
        Binding("2", "section_2", "", show=False),
        Binding("3", "section_3", "", show=False),
        Binding("4", "section_4", "", show=False),
        Binding("5", "section_5", "", show=False),
    ]

    def __init__(self, db_path: str = "~/.loom/memory.db", initial_view: str = "semantic") -> None:
        super().__init__()
        self._db_path = str(Path(db_path).expanduser().resolve())
        self._db: "aiosqlite.Connection | None" = None
        self._search: "PalaceSearch | None" = None
        # Resolve initial_view string to PalaceSection
        try:
            self._initial_section = PalaceSection(initial_view)
        except ValueError:
            self._initial_section = PalaceSection.SEMANTIC

    # ── App lifecycle ────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        """Build the full UI tree."""
        yield PalaceHeader()
        with Horizontal(id="body"):
            yield NavSidebar()
            # Content container — always visible, children are shown/hidden
            with Vertical(id="content-area"):
                pass  # views mounted in on_mount
        yield PalaceStatusBar()

    async def on_mount(self) -> None:
        """Connect DB and mount all views."""
        import aiosqlite

        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        self._search = PalaceSearch(self._db)

        # Mount all views into the content-area container
        content = self.query_one("#content-area", Vertical)
        views: list[object] = [
            SemanticView(self._search),
            HealthView(self._search),
            RelationalView(self._search),
            EpisodicView(self._search),
            SkillsView(self._search),
        ]
        view_map = {
            PalaceSection.SEMANTIC:    views[0],
            PalaceSection.HEALTH:      views[1],
            PalaceSection.RELATIONAL:  views[2],
            PalaceSection.EPISODIC:    views[3],
            PalaceSection.SKILLS:      views[4],
        }
        # Store by section name for lookup
        self._views = {s.value: v for s, v in view_map.items()}
        self._all_views = view_map  # full reference

        for v in views:
            content.mount(v)
            v.display = False  # all hidden until selected

        # Store nav and status references
        self._nav = self.query_one("NavSidebar", NavSidebar)
        self._status = self.query_one("PalaceStatusBar", PalaceStatusBar)

        # Wire nav → section switch via message subscription
        self._nav.on_section_selected = self._on_nav_section  # type: ignore[assignment]

        # Show semantic by default (nav's on_mount calls select(SEMANTIC)
        # which posts SectionSelected, triggering _on_nav_section)
        # We just call _show_section directly to be safe
        await self._show_section(self._initial_section)

        # Load status bar stats
        await self._update_status_bar()

    async def _on_nav_section(self, section: PalaceSection) -> None:
        """Handle NavSidebar.SectionSelected message."""
        await self._show_section(section)

    async def _show_section(self, section: PalaceSection) -> None:
        """Switch visible content view."""
        for s, v in self._all_views.items():
            v.display = (s == section)
        self._active_section = section
        self._nav.select(section)

    async def _update_status_bar(self) -> None:
        """Refresh the bottom status bar with live stats."""
        if self._search is None:
            return
        try:
            sem = await self._search.semantic_stats()
            rel = await self._search.relational_stats()
            ski = await self._search.skill_stats()
            ses = await self._search.session_stats()
            self._status.update(
                semantic=sem.get("total", 0),
                relational=rel.get("total", 0),
                skills=ski.get("total", 0),
                sessions=ses.get("total", 0),
                active_section=self._active_section.value,
            )
        except Exception:
            pass

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_section_1(self) -> None: self._nav.select(PalaceSection.SEMANTIC)
    def action_section_2(self) -> None: self._nav.select(PalaceSection.RELATIONAL)
    def action_section_3(self) -> None: self._nav.select(PalaceSection.EPISODIC)
    def action_section_4(self) -> None: self._nav.select(PalaceSection.SKILLS)
    def action_section_5(self) -> None: self._nav.select(PalaceSection.HEALTH)

    def action_focus_search(self) -> None:
        """Show search hint notification."""
        section_labels = {
            "semantic": "Type to filter semantic entries by key or value",
            "relational": "Search relational triples",
            "episodic": "Search session history",
            "skills": "Search skill genomes",
            "health": "Health view — no search",
        }
        msg = section_labels.get(self._active_section.value, "")
        if msg:
            self.notify(f"[dim]{msg}[/dim]", timeout=3)

    def action_reset_view(self) -> None:
        """Esc: go back to Semantic if not already there."""
        if self._active_section != PalaceSection.SEMANTIC:
            self._nav.select(PalaceSection.SEMANTIC)

    def action_refresh(self) -> None:
        """Ctrl+R: reload current view's data."""
        view = self._all_views.get(self._active_section)
        if view and hasattr(view, "refresh"):
            view.reload()  # type: ignore[union-attr]
        asyncio.create_task(self._update_status_bar())
        self.notify("[dim]Refreshed.[/dim]", timeout=1)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    async def on_unmount(self) -> None:
        if self._db:
            await self._db.close()
