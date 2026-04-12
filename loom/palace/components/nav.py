"""
NavSidebar — left navigation panel for Memory Palace.
"""

from __future__ import annotations

from enum import Enum

from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static


class PalaceSection(Enum):
    SEMANTIC   = "semantic"
    RELATIONAL = "relational"
    EPISODIC   = "episodic"
    SKILLS     = "skills"
    HEALTH     = "health"


class NavSidebar(Widget):
    """
    Left sidebar with five navigation items.
    Click or press 1-5 to switch sections.
    """

    DEFAULT_CSS = '''
    NavSidebar {
        width: 38;
        background: #150d28;
        border-right: solid #3d2a6b;
    }
    #nav-list {
        height: 1fr;
        padding: 1 0;
    }
    .nav-item {
        height: 3;
        padding: 0 1;
        content-align: left middle;
    }
    .nav-item:hover {
        background: #1e1238;
    }
    .nav-active {
        background: #2a1e50;
    }
    #nav-sep {
        height: 1;
        border-top: solid #3d2a6b;
        margin: 1 0;
    }
    '''

    class SectionSelected(Message, bubble=True):
        def __init__(self, section: PalaceSection) -> None:
            super().__init__()
            self.section = section

    NAV_ITEMS = [
        (PalaceSection.SEMANTIC,   "◈", "Semantic",   "Facts & knowledge"),
        (PalaceSection.RELATIONAL, "◉", "Relational",  "Subject → predicate → object"),
        (PalaceSection.EPISODIC,   "◌", "Episodic",   "Session history & turns"),
        (PalaceSection.SKILLS,     "✧", "Skills",     "Skill genomes & health"),
        (PalaceSection.HEALTH,     "✦", "Health",     "Memory palace overview"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._active: PalaceSection = PalaceSection.SEMANTIC
        self._nav_widgets: dict[PalaceSection, Static] = {}

    def compose(self) -> ComposeResult:
        for section, icon, label, hint in self.NAV_ITEMS:
            num = list(PalaceSection).index(section) + 1
            widget_id = f"nav-{section.value}"
            # Default to inactive style; select() will activate SEMANTIC on startup
            yield Static(
                f"[dim]{num}[/dim]  [#9b87b5]{icon}[/#9b87b5]  "
                f"[#9b87b5]{label}[/#9b87b5]  [dim]{hint}[/dim]",
                id=widget_id,
                classes="nav-item",
            )
        yield Static("", id="nav-sep")

    def on_mount(self) -> None:
        # Populate _nav_widgets from the DOM now that compose() has run
        self._nav_widgets = {}
        for section, *_ in self.NAV_ITEMS:
            widget_id = f"nav-{section.value}"
            try:
                self._nav_widgets[section] = self.query_one(f"#{widget_id}", Static)
            except Exception:
                pass

        # Activate SEMANTIC by default
        self.select(PalaceSection.SEMANTIC)

    def select(self, section: PalaceSection) -> None:
        """Programmatically switch to a section."""
        if section not in self._nav_widgets:
            return

        # Deactivate current
        old = self._nav_widgets.get(self._active)
        if old:
            old.classes = "nav-item"
            for s, icon, label, hint in self.NAV_ITEMS:
                if s == self._active:
                    num = list(PalaceSection).index(s) + 1
                    old.update(
                        f"[dim]{num}[/dim]  [#9b87b5]{icon}[/#9b87b5]  "
                        f"[#9b87b5]{label}[/#9b87b5]  [dim]{hint}[/dim]"
                    )
                    break

        # Activate new
        self._active = section
        new = self._nav_widgets[section]
        new.classes = "nav-item nav-active"
        for s, icon, label, hint in self.NAV_ITEMS:
            if s == section:
                num = list(PalaceSection).index(s) + 1
                new.update(
                    f"[dim]{num}[/dim]  [#a78bfa]{icon}[/#a78bfa]  "
                    f"[#e8deff]{label}[/#e8deff]  [dim]{hint}[/dim]"
                )
                break

        self.post_message(self.SectionSelected(section))

    def on_click(self, event) -> None:
        """Map click y-position to section."""
        index = event.y // 3
        if 0 <= index < len(self.NAV_ITEMS):
            section = self.NAV_ITEMS[index][0]
            self.select(section)

    def key_1(self) -> None: self.select(PalaceSection.SEMANTIC)
    def key_2(self) -> None: self.select(PalaceSection.RELATIONAL)
    def key_3(self) -> None: self.select(PalaceSection.EPISODIC)
    def key_4(self) -> None: self.select(PalaceSection.SKILLS)
    def key_5(self) -> None: self.select(PalaceSection.HEALTH)
