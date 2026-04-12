"""
RelationalView — browse relational (subject, predicate, object) triples.

Key design (mirrors SemanticView):
  - Uses ListView for virtualised rendering.
  - Subjects listed first; clicking expands into predicate rows.
  - Expansion is a mode switch — stored in self._expanded_subject.
  - Batched loading via call_next for smooth UX with larger datasets.
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
class TripleDisplay:
    subject: str
    predicate: str
    object: str
    confidence: float


class RelationalView(Vertical):
    """
    Relational memory explorer.

    Shows subjects (with triple count) in a list; click a subject to
    expand into its predicates.  Back via pressing Escape or clicking
    the back row.
    """

    DEFAULT_CSS = """
    RelationalView {
        height: 1fr;
        layout: vertical;
        overflow: hidden;
    }
    RelationalView ListView {
        height: 1fr;
        background: #0d0a1a;
    }
    """

    class Loaded(Message):
        pass

    def __init__(self, search: "PalaceSearch") -> None:
        super().__init__()
        self._search = search
        self._subjects: list[tuple[str, int]] = []
        self._expanded_subject: str | None = None
        self._expanded_triples: list[TripleDisplay] = []
        self._batch_index = 0

    def _render(self) -> "Content":
        from textual.content import Content
        return Content.from_markup("")

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold #d4a853]◉ Relational Memory[/bold #d4a853]  "
            "[dim]— subject → predicate → object[/dim]",
            id="rel-header",
            classes="content-title",
        )
        yield Static("", id="rel-subtitle", classes="content-subtitle")
        yield ListView(id="rel-list")

    # ── Mount / load ─────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._load_async()

    async def _load_async(self) -> None:
        self._subjects = await self._search.list_relational_subjects()
        self._render_subject_mode()

    # ── Subject-mode rendering ────────────────────────────────────────────────

    def _render_subject_mode(self) -> None:
        """Fill the list with subject rows."""
        lv = self.query_one("#rel-list", ListView)
        lv.clear()
        self._expanded_subject = None

        subtitle = self.query_one("#rel-subtitle", Static)
        subtitle.update(f"[dim]{len(self._subjects)} subjects[/dim]")

        for subj, count in self._subjects:
            lv.append(self._build_subject_item(subj, count))

        self.post_message(self.Loaded())

    def _build_subject_item(self, subj: str, count: int) -> ListItem:
        label = (
            f"[#a78bfa]◉[/]  [bold #a78bfa]{subj}[/bold #a78bfa]  "
            f"[dim]·[/]  [dim]{count} triple{'s' if count != 1 else ''}[/dim]"
        )
        return ListItem(Static(label), id=f"subj-{subj[:40]}")

    # ── Expand subject → predicate mode ─────────────────────────────────────

    async def _expand_subject(self, subject: str) -> None:
        self._expanded_subject = subject
        triples_raw = await self._search.list_relational_by_subject(subject)
        self._expanded_triples = [
            TripleDisplay(
                subject=t.key,
                predicate=t.extra.get("predicate", "?"),
                object=t.extra.get("object", "?"),
                confidence=t.extra.get("confidence", 1.0),
            )
            for t in triples_raw
        ]

        lv = self.query_one("#rel-list", ListView)
        lv.clear()

        lv.append(ListItem(
            Static("[dim]← back to subjects[/dim]", id="rel-back"),
            id="rel-back-item",
        ))

        subtitle = self.query_one("#rel-subtitle", Static)
        subtitle.update(f"[bold #a78bfa]{subject}[/bold #a78bfa]  [dim]{len(self._expanded_triples)} triples[/dim]")

        for t in self._expanded_triples:
            lv.append(self._build_predicate_item(t))
        self.post_message(self.Loaded())

    # ── Predicate item builder ───────────────────────────────────────────────

    def _build_predicate_item(self, t: TripleDisplay) -> ListItem:
        label = (
            f"[dim]──[/dim]  [bold #c084fc]{t.predicate}[/bold #c084fc]  "
            f"[dim]──▶[/dim]  [#e8deff]{t.object}[/#e8deff]  "
            f"[dim]conf={t.confidence:.2f}[/dim]"
        )
        return ListItem(Static(label))

    # ── Interaction ───────────────────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        try:
            lv = self.query_one("#rel-list", ListView)
            node = lv.children[lv.index]
            item_id = node.id or ""
        except Exception:
            return

        if self._expanded_subject is None:
            if item_id.startswith("subj-"):
                subject = item_id[len("subj-"):]
                import asyncio as _asyncio
                _asyncio.create_task(self._expand_subject(subject))
        else:
            if item_id == "rel-back-item":
                self._render_subject_mode()

    def back_to_subjects(self) -> None:
        if self._expanded_subject is not None:
            self._render_subject_mode()

    def reload(self) -> None:
        self._load_async()