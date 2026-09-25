"""System message = PromptStack + session sections (Issue #597).

Contract
--------
``messages[0]`` is always rendered as the PromptStack's composed prompt
followed by the session's own sections (workspace note, memory index,
memory health alert, MCP server instructions), in the order they were first
added, joined by blank lines.  It is never edited as a string in place, so:

* ``switch_personality()`` swaps only the PromptStack part — every section
  survives the switch (previously the whole message was overwritten);
* ``_refresh_memory_index()`` replaces the memory-index section wholesale —
  no sentinel parsing, so the tail of an old index block (skills catalog,
  self-portrait) cannot be left behind and duplicated.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from loom.core import session as session_module
from loom.core.session import LoomSession


class _FakeStack:
    def __init__(self, base: str = "SOUL") -> None:
        self.base = base
        self.personality: str | None = None

    @property
    def composed_prompt(self) -> str:
        return "\n\n---\n\n".join(p for p in (self.base, self.personality) if p)

    def switch_personality(self, name: str) -> bool:
        if name == "ghost":
            return False
        self.personality = f"PERSONA:{name}"
        return True

    def clear_personality(self) -> None:
        self.personality = None


def _session(base: str = "SOUL") -> LoomSession:
    s = LoomSession.__new__(LoomSession)
    s._stack = _FakeStack(base)
    s.messages = []
    s._system_sections = {}
    s.budget = MagicMock()
    s._memory = SimpleNamespace(semantic=None, procedural=None, episodic=None)
    return s


def _with_sections(s: LoomSession) -> LoomSession:
    s._set_system_section("workspace", "## Workspace\nYour working directory is: `/w`")
    s._set_system_section("memory_index", "Memory Index\n───\n<available_skills>a</available_skills>")
    s._set_system_section("memory_health", "HEALTH ALERT")
    s._set_system_section("mcp", "## MCP server: substrate\nuse get_context")
    s.messages.append({"role": "user", "content": "hi"})
    return s


def _system(s: LoomSession) -> str:
    assert s.messages[0]["role"] == "system"
    return s.messages[0]["content"]


class TestRender:
    def test_stack_then_sections_in_insertion_order(self) -> None:
        s = _with_sections(_session())
        assert _system(s) == (
            "SOUL"
            "\n\n## Workspace\nYour working directory is: `/w`"
            "\n\nMemory Index\n───\n<available_skills>a</available_skills>"
            "\n\nHEALTH ALERT"
            "\n\n## MCP server: substrate\nuse get_context"
        )
        assert s.messages[1] == {"role": "user", "content": "hi"}

    def test_empty_stack_renders_sections_only(self) -> None:
        s = _session(base="")
        s._set_system_section("workspace", "## Workspace")
        assert _system(s) == "## Workspace"

    def test_replacing_a_section_keeps_its_position(self) -> None:
        s = _with_sections(_session())
        s._set_system_section("memory_index", "Memory Index v2")
        text = _system(s)
        assert "<available_skills>" not in text
        assert text.index("Memory Index v2") < text.index("HEALTH ALERT")

    def test_blank_section_is_dropped(self) -> None:
        s = _with_sections(_session())
        s._set_system_section("memory_health", "")
        assert "HEALTH ALERT" not in _system(s)

    def test_nothing_to_render_removes_system_message(self) -> None:
        s = _session(base="")
        s._set_system_section("workspace", "## Workspace")
        s.messages.append({"role": "user", "content": "hi"})
        s._set_system_section("workspace", "")
        assert s.messages == [{"role": "user", "content": "hi"}]


class TestSwitchPersonality:
    def test_sections_survive_switch(self) -> None:
        s = _with_sections(_session())
        before = _system(s)

        assert s.switch_personality("adversarial") is True

        text = _system(s)
        assert text.startswith("SOUL\n\n---\n\nPERSONA:adversarial\n\n## Workspace")
        assert text.endswith(before[len("SOUL"):])
        assert s.messages[1] == {"role": "user", "content": "hi"}

    def test_off_restores_original_message(self) -> None:
        s = _with_sections(_session())
        before = _system(s)
        s.switch_personality("adversarial")
        s.switch_personality("off")
        assert _system(s) == before

    def test_unknown_personality_changes_nothing(self) -> None:
        s = _with_sections(_session())
        before = list(s.messages)
        assert s.switch_personality("ghost") is False
        assert s.messages == before


class _FakeIndex:
    def __init__(self, n: int) -> None:
        self.n = n
        self.is_empty = False

    def render(self) -> str:
        # Mirrors MemoryIndex.render(): rule-bounded block, hint lines, then
        # a tail (skills catalog / self-portrait) *after* the hints — the part
        # the old sentinel-based splice failed to remove.
        bar = "─" * 45
        return (
            f"Memory Index\n{bar}\nSemantic  : {self.n} facts\n{bar}\n"
            "Use recall(query) to retrieve relevant entries.\n\n"
            f"<available_skills>{self.n}</available_skills>"
        )


class TestRefreshMemoryIndex:
    async def test_refresh_replaces_block_without_leftovers(self, monkeypatch) -> None:
        counter = iter(range(1, 100))

        class _Indexer:
            def __init__(self, *_a, **_kw) -> None:
                pass

            async def build(self):
                return _FakeIndex(next(counter))

        monkeypatch.setattr(session_module, "MemoryIndexer", _Indexer)
        s = _session()
        s._set_system_section("workspace", "## Workspace")
        s._set_system_section("memory_index", _FakeIndex(0).render())
        s._set_system_section("mcp", "## MCP server: substrate")

        await s._refresh_memory_index()
        await s._refresh_memory_index()

        text = _system(s)
        assert text.count("Memory Index") == 1
        assert text.count("<available_skills>") == 1
        assert "<available_skills>2</available_skills>" in text
        assert text.index("Memory Index") < text.index("## MCP server: substrate")
        s.budget.record_messages.assert_called_with(s.messages)

    async def test_refresh_survives_a_personality_switch(self, monkeypatch) -> None:
        class _Indexer:
            def __init__(self, *_a, **_kw) -> None:
                pass

            async def build(self):
                return _FakeIndex(7)

        monkeypatch.setattr(session_module, "MemoryIndexer", _Indexer)
        s = _with_sections(_session())
        s.switch_personality("adversarial")
        await s._refresh_memory_index()

        text = _system(s)
        assert "PERSONA:adversarial" in text
        assert "Semantic  : 7 facts" in text
        assert "## MCP server: substrate" in text
