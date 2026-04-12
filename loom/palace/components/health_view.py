"""
HealthView — Memory Palace health dashboard.

2×2 grid showing key stats for each memory type, plus
a decay warning list at the bottom.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.containers import Vertical
from textual.widgets import Static
from textual.content import Content

if TYPE_CHECKING:
    from loom.palace.search import PalaceSearch


class HealthView(Vertical):
    """
    Memory palace health overview.

    Shows 2×2 stat grid + decay warning list.
    """

    DEFAULT_CSS = '''
    HealthView {
        height: 1fr;
        layout: vertical;
        overflow: hidden;
    }
    .health-title {
        margin-bottom: 1;
    }
    #health-grid {
        layout: grid;
        grid-size: 2 2;
        grid-gutter: 1 2;
        height: auto;
        margin-bottom: 2;
    }
    .health-card {
        background: #1e1238;
        border: solid #3d2a6b;
        padding: 1 2;
        height: auto;
    }
    .health-card-title {
        color: #d4a853;
        text-style: bold;
    }
    .health-card-value {
        color: #e8deff;
        text-style: bold;
        padding: 0 0;
    }
    .health-card-sub {
        color: #9b87b5;
    }
    .health-good    { color: #86efac; }
    .health-warning { color: #fbbf24; }
    .health-danger  { color: #f87171; }
    #decay-list {
        height: 1fr;
        overflow-y: auto;
    }
    .decay-card {
        background: #150d28;
        border-left: solid #7c3aed;
        padding: 0 1 0 2;
        margin-bottom: 1;
    }
    .decay-label { color: #9b87b5; }
    '''

    def __init__(self, search: "PalaceSearch") -> None:
        super().__init__()
        self._search = search
        self._stats: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold #d4a853]✦ Memory Palace Health[/bold #d4a853]  "
            "[dim]— overview of all memory systems[/dim]",
            classes="health-title",
        )
        yield Static("[dim]Loading health data...[/dim]")
        yield Horizontal(
            Static("", classes="health-card", id="card-semantic"),
            Static("", classes="health-card", id="card-relational"),
            Static("", classes="health-card", id="card-skills"),
            Static("", classes="health-card", id="card-sessions"),
            id="health-grid",
        )
        yield Static(
            "[bold #7c3aed]↘[/bold #7c3aed]  [dim]Decay warnings — effective confidence near threshold[/dim]",
            classes="decay-label",
            id="decay-header",
        )
        yield VerticalScroll(id="decay-list")

    def on_mount(self) -> None:
        self._load_async()

    async def _load_async(self) -> None:
        sem, rel, ski, ses = await self._fetch_all_stats()
        self._stats = {
            "semantic": sem,
            "relational": rel,
            "skills": ski,
            "sessions": ses,
    }
        self._render()

    async def _fetch_all_stats(self):
        sem_task = self._search.semantic_stats()
        rel_task = self._search.relational_stats()
        ski_task = self._search.skill_stats()
        ses_task = self._search.session_stats()

        import asyncio as _asyncio
        done = await _asyncio.gather(sem_task, rel_task, ski_task, ses_task,
                                      return_exceptions=True)
        return done[0], done[1], done[2], done[3]

    def _render(self) -> Content:
        sem = self._stats.get("semantic", {})
        rel = self._stats.get("relational", {})
        ski = self._stats.get("skills", {})
        ses = self._stats.get("sessions", {})

        # ── Semantic card ──────────────────────────────────────────────────
        total = sem.get("total", 0)
        high = sem.get("high", 0)
        mid  = sem.get("mid", 0)
        low  = sem.get("low", 0)
        today = sem.get("today", 0)
        trend_icon = "[#86efac]↑[/]" if today > 0 else "[dim]·[/]"
        trend_text = f"[#86efac]+{today}[/#86efac] today" if today > 0 else "[dim]none today[/]"
        self.query_one("#card-semantic", Static).update(
            f"[bold #d4a853]◈ Semantic[/bold #d4a853]\n"
            f"[bold #e8deff]{total:,}[bold]  [dim]facts[/dim]\n"
            f"[#a78bfa]●[/] {high} high  [dim]·[/] [#c084fc]●[/] {mid} mid  [dim]·[/] [#7c3aed]●[/] {low} low\n"
            f"[dim]{trend_icon} {trend_text}[/dim]"
        )

        # ── Relational card ─────────────────────────────────────────────────
        r_total = rel.get("total", 0)
        r_subj  = rel.get("subjects", 0)
        r_today = rel.get("today", 0)
        trend_icon = "[#86efac]↑[/]" if r_today > 0 else "[dim]·[/]"
        self.query_one("#card-relational", Static).update(
            f"[bold #d4a853]◉ Relational[/bold #d4a853]\n"
            f"[bold #e8deff]{r_total:,}[bold]  [dim]triples[/dim]\n"
            f"[dim]across {r_subj} subjects[/dim]\n"
            f"[dim]{trend_icon} {r_today} added today[/dim]"
        )

        # ── Skills card ─────────────────────────────────────────────────────
        s_total  = ski.get("total", 0)
        s_active = ski.get("active", 0)
        s_fail   = ski.get("failing", 0)
        fail_cls  = "health-danger" if s_fail > 0 else "health-good"
        fail_txt  = f"[#f87171]{s_fail} failing[/#f87171]" if s_fail > 0 else "[#86efac]none failing[/#86efac]"
        self.query_one("#card-skills", Static).update(
            f"[bold #d4a853]✧ Skills[/bold #d4a853]\n"
            f"[bold #e8deff]{s_total:,}[bold]  [dim]genomes[/dim]\n"
            f"[dim]{s_active} with usage[/dim]\n"
            f"{fail_txt}"
        )

        # ── Sessions card ───────────────────────────────────────────────────
        s_total2 = ses.get("total", 0)
        s_last   = ses.get("last_active") or "unknown"
        if s_last != "unknown":
            s_last = s_last[:16].replace("T", " ")
        self.query_one("#card-sessions", Static).update(
            f"[bold #d4a853]◌ Sessions[/bold #d4a853]\n"
            f"[bold #e8deff]{s_total2:,}[bold]  [dim]logged[/dim]\n"
            f"[dim]last active:[/dim]\n"
            f"[dim]{s_last}[/dim]"
        )

        # ── Decay warnings ──────────────────────────────────────────────────
        self._load_decays()
        return Content.from_markup("")

    async def _load_decays(self) -> None:
        """Show semantic entries with low effective confidence."""
        db = self._search._db
        cursor = await db.execute(
            """
            SELECT key, value, confidence, updated_at
            FROM semantic_entries
            WHERE confidence <= 0.35
            ORDER BY confidence ASC, updated_at ASC
            LIMIT 20
            """
        )
        rows = await cursor.fetchall()

        list_el = self.query_one("#decay-list", VerticalScroll)
        list_el.remove_children()

        if not rows:
            list_el.mount(Static("[dim]No decay warnings — all entries healthy.[/dim]"))
            return None  # will be replaced with Content by caller

        for r in rows:
            key, value, conf, updated = r
            value_short = value[:80] + ("…" if len(value) > 80 else "")
            card = Static(
                f"[#7c3aed]{key}[/#7c3aed]\n"
                f"[dim]{value_short}[/dim]\n"
                f"[#f87171]conf={conf:.2f}[/#f87171]  [dim]updated: {updated[:10]}[/dim]",
                classes="decay-card",
            )
            list_el.mount(card)

    def reload(self) -> None:
        self._load_async()
