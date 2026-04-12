"""
SkillsView — browse skill genomes and their health.

Key design (mirrors SemanticView):
  - Uses ListView for virtualised rendering.
  - Data is small (≤16 genomes) — one-shot load is fine.
  - Confidence colouring, success rate badges, tag display.
  - _render() override returns Content immediately (no DOM ops in render path).
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
class SkillDisplay:
    name: str
    confidence: float
    usage_count: int
    success_rate: float
    deprecation_threshold: float
    tags: list[str]
    body: str
    updated_at: str


class SkillsView(Vertical):
    """
    Skill genome explorer.

    Shows all skill genomes sorted by confidence, with usage and
    success rate badges.  Small dataset — loaded in one shot.
    """

    DEFAULT_CSS = """
    SkillsView {
        height: 1fr;
        layout: vertical;
        overflow: hidden;
    }
    SkillsView ListView {
        height: 1fr;
        background: #0d0a1a;
    }
    """

    class Loaded(Message):
        pass

    def __init__(self, search: "PalaceSearch") -> None:
        super().__init__()
        self._search = search
        self._skills: list[SkillDisplay] = []

    def _render(self) -> "Content":
        from textual.content import Content
        return Content.from_markup("")

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold #d4a853]✧ Skill Genomes[/bold #d4a853]  "
            "[dim]— agent skills and their performance[/dim]",
            id="skills-header",
            classes="content-title",
        )
        yield Static("", id="skills-subtitle", classes="content-subtitle")
        yield ListView(id="skills-list")

    # ── Mount / load ─────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._load_async()

    async def _load_async(self) -> None:
        db = self._search._db
        cursor = await db.execute(
            """
            SELECT name, confidence, usage_count, success_rate,
                   deprecation_threshold, tags, body, updated_at
            FROM skill_genomes
            ORDER BY confidence DESC
            LIMIT 200
            """
        )
        rows = await cursor.fetchall()

        import json as _json
        self._skills = [
            SkillDisplay(
                name=r[0],
                confidence=float(r[1]),
                usage_count=int(r[2]),
                success_rate=float(r[3]),
                deprecation_threshold=float(r[4]) if r[4] else 0.5,
                tags=_json.loads(r[5]) if r[5] else [],
                body=r[6] or "",
                updated_at=r[7],
            )
            for r in rows
        ]
        self._render_skills()

    # ── Render ────────────────────────────────────────────────────────────────

    def _render_skills(self) -> None:
        lv = self.query_one("#skills-list", ListView)
        lv.clear()

        subtitle = self.query_one("#skills-subtitle", Static)
        subtitle.update(f"[dim]{len(self._skills)} genomes[/dim]")

        if not self._skills:
            lv.append(ListItem(Static("[dim]No skill genomes found.[/dim]")))
            self.post_message(self.Loaded())
            return

        for s in self._skills:
            lv.append(self._build_skill_card(s))

        self.post_message(self.Loaded())

    # ── Card builder ───────────────────────────────────────────────────────────

    def _build_skill_card(self, s: SkillDisplay) -> ListItem:
        conf_color = (
            "#86efac" if s.confidence > 0.7 else
            "#fbbf24" if s.confidence >= 0.6 else
            "#f87171"
        )
        sr_color = (
            "#86efac" if s.success_rate >= 0.8 else
            "#fbbf24" if s.success_rate >= 0.6 else
            "#f87171"
        )
        tags_str = "  ".join(f"[dim]{t}[/dim]" for t in s.tags[:6])
        body_preview = s.body[:100].replace("\n", " ") + ("…" if len(s.body) > 100 else "")

        label = (
            f"[bold #d4a853]✧[/]  [bold #e8deff]{s.name}[/bold #e8deff]\n"
            f"[dim]{body_preview}[/dim]\n"
            f"[{conf_color}]conf={s.confidence:.2f}[/{conf_color}]  "
            f"[dim]used: {s.usage_count}×[/dim]  "
            f"[{sr_color}]success: {s.success_rate:.0%}[/{sr_color}]\n"
            f"[dim]{tags_str}[/dim]"
        )
        return ListItem(Static(label), id=f"skill-{s.name[:40]}")

    def reload(self) -> None:
        self._load_async()