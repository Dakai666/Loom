"""
Tests for Phase 4A — PromptStack (three-layer prompt composition).

Coverage
--------
- Empty stack → empty composed_prompt
- Single-layer (soul only)
- Two-layer (soul + agent)
- Three-layer (soul + agent + personality)
- Layer ordering and LAYER_SEPARATOR
- switch_personality — success and failure
- switch_personality updates composed_prompt correctly
- switch_personality replaces existing personality layer
- clear_personality removes layer, keeps others
- available_personalities discovery
- current_personality property
- layer_names property
- from_config — full config
- from_config — no [identity] section
- from_config — empty personality string
- from_config — relative path resolution
- Missing files are silently skipped
"""

from pathlib import Path
import pytest
from loom.core.cognition.prompt_stack import PromptStack, PromptLayer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_soul(tmp_path: Path) -> Path:
    p = tmp_path / "SOUL.md"
    p.write_text("I am SOUL.", encoding="utf-8")
    return p


@pytest.fixture
def tmp_agent(tmp_path: Path) -> Path:
    p = tmp_path / "Agent.md"
    p.write_text("I am Agent.", encoding="utf-8")
    return p


@pytest.fixture
def tmp_personalities(tmp_path: Path) -> Path:
    pd = tmp_path / "personalities"
    pd.mkdir()
    (pd / "adversarial.md").write_text("I challenge assumptions.", encoding="utf-8")
    (pd / "minimalist.md").write_text("Delete before adding.", encoding="utf-8")
    return pd


# ---------------------------------------------------------------------------
# Basic composition
# ---------------------------------------------------------------------------

class TestComposition:
    def test_empty_stack_returns_empty_string(self):
        stack = PromptStack()
        assert stack.load() == ""
        assert stack.composed_prompt == ""

    def test_soul_only(self, tmp_soul):
        stack = PromptStack(soul_path=tmp_soul)
        result = stack.load()
        assert result == "I am SOUL."
        assert stack.layer_names == ["soul"]

    def test_soul_and_agent(self, tmp_soul, tmp_agent):
        stack = PromptStack(soul_path=tmp_soul, agent_path=tmp_agent)
        result = stack.load()
        sep = PromptStack.LAYER_SEPARATOR
        assert result == f"I am SOUL.{sep}I am Agent."
        assert stack.layer_names == ["soul", "agent"]

    def test_three_layers(self, tmp_soul, tmp_agent, tmp_personalities):
        p_path = tmp_personalities / "adversarial.md"
        stack = PromptStack(
            soul_path=tmp_soul,
            agent_path=tmp_agent,
            personality_path=p_path,
            personalities_dir=tmp_personalities,
        )
        result = stack.load()
        sep = PromptStack.LAYER_SEPARATOR
        assert result == f"I am SOUL.{sep}I am Agent.{sep}I challenge assumptions."
        assert stack.layer_names == ["soul", "agent", "personality"]

    def test_missing_files_are_skipped(self, tmp_path):
        stack = PromptStack(
            soul_path=tmp_path / "nonexistent_soul.md",
            agent_path=tmp_path / "nonexistent_agent.md",
        )
        assert stack.load() == ""
        assert stack.layer_names == []

    def test_partial_missing_skips_gracefully(self, tmp_soul, tmp_path):
        stack = PromptStack(
            soul_path=tmp_soul,
            agent_path=tmp_path / "ghost.md",
        )
        assert stack.load() == "I am SOUL."
        assert stack.layer_names == ["soul"]

    def test_separator_is_present_between_layers(self, tmp_soul, tmp_agent):
        stack = PromptStack(soul_path=tmp_soul, agent_path=tmp_agent)
        stack.load()
        assert PromptStack.LAYER_SEPARATOR in stack.composed_prompt

    def test_load_returns_same_as_composed_prompt(self, tmp_soul, tmp_agent):
        stack = PromptStack(soul_path=tmp_soul, agent_path=tmp_agent)
        returned = stack.load()
        assert returned == stack.composed_prompt


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class TestProperties:
    def test_layer_names_empty_before_load(self):
        stack = PromptStack()
        assert stack.layer_names == []

    def test_current_personality_none_when_absent(self, tmp_soul):
        stack = PromptStack(soul_path=tmp_soul)
        stack.load()
        assert stack.current_personality is None

    def test_current_personality_returns_stem(self, tmp_personalities):
        p_path = tmp_personalities / "adversarial.md"
        stack = PromptStack(personality_path=p_path, personalities_dir=tmp_personalities)
        stack.load()
        assert stack.current_personality == "adversarial"

    def test_layer_names_reflects_loaded_layers(self, tmp_soul, tmp_agent):
        stack = PromptStack(soul_path=tmp_soul, agent_path=tmp_agent)
        stack.load()
        assert stack.layer_names == ["soul", "agent"]


# ---------------------------------------------------------------------------
# switch_personality
# ---------------------------------------------------------------------------

class TestSwitchPersonality:
    def test_switch_success(self, tmp_soul, tmp_personalities):
        stack = PromptStack(soul_path=tmp_soul, personalities_dir=tmp_personalities)
        stack.load()
        ok = stack.switch_personality("adversarial")
        assert ok is True
        assert "adversarial" in stack.layer_names or stack.current_personality == "adversarial"

    def test_switch_failure_unknown_name(self, tmp_soul, tmp_personalities):
        stack = PromptStack(soul_path=tmp_soul, personalities_dir=tmp_personalities)
        stack.load()
        ok = stack.switch_personality("nonexistent")
        assert ok is False

    def test_switch_updates_composed_prompt(self, tmp_soul, tmp_personalities):
        stack = PromptStack(soul_path=tmp_soul, personalities_dir=tmp_personalities)
        stack.load()
        stack.switch_personality("adversarial")
        assert "I challenge assumptions." in stack.composed_prompt

    def test_switch_replaces_existing_personality(self, tmp_soul, tmp_personalities):
        p_path = tmp_personalities / "adversarial.md"
        stack = PromptStack(
            soul_path=tmp_soul,
            personality_path=p_path,
            personalities_dir=tmp_personalities,
        )
        stack.load()
        assert "I challenge assumptions." in stack.composed_prompt

        stack.switch_personality("minimalist")
        assert "Delete before adding." in stack.composed_prompt
        assert "I challenge assumptions." not in stack.composed_prompt
        # Only one personality layer
        assert stack.layer_names.count("personality") == 1

    def test_switch_sets_current_personality(self, tmp_soul, tmp_personalities):
        stack = PromptStack(soul_path=tmp_soul, personalities_dir=tmp_personalities)
        stack.load()
        stack.switch_personality("minimalist")
        assert stack.current_personality == "minimalist"

    def test_switch_appends_layer_when_none_active(self, tmp_soul, tmp_personalities):
        stack = PromptStack(soul_path=tmp_soul, personalities_dir=tmp_personalities)
        stack.load()
        assert "personality" not in stack.layer_names
        stack.switch_personality("adversarial")
        assert "personality" in stack.layer_names


# ---------------------------------------------------------------------------
# clear_personality
# ---------------------------------------------------------------------------

class TestClearPersonality:
    def test_clear_removes_personality_layer(self, tmp_soul, tmp_personalities):
        p_path = tmp_personalities / "adversarial.md"
        stack = PromptStack(
            soul_path=tmp_soul,
            personality_path=p_path,
            personalities_dir=tmp_personalities,
        )
        stack.load()
        assert "personality" in stack.layer_names

        stack.clear_personality()
        assert "personality" not in stack.layer_names

    def test_clear_keeps_other_layers(self, tmp_soul, tmp_agent, tmp_personalities):
        p_path = tmp_personalities / "adversarial.md"
        stack = PromptStack(
            soul_path=tmp_soul,
            agent_path=tmp_agent,
            personality_path=p_path,
            personalities_dir=tmp_personalities,
        )
        stack.load()
        stack.clear_personality()
        assert stack.layer_names == ["soul", "agent"]

    def test_clear_resets_current_personality(self, tmp_personalities):
        p_path = tmp_personalities / "adversarial.md"
        stack = PromptStack(personality_path=p_path, personalities_dir=tmp_personalities)
        stack.load()
        assert stack.current_personality == "adversarial"

        stack.clear_personality()
        assert stack.current_personality is None

    def test_clear_on_empty_stack_is_noop(self):
        stack = PromptStack()
        stack.load()
        stack.clear_personality()   # must not raise
        assert stack.layer_names == []


# ---------------------------------------------------------------------------
# available_personalities
# ---------------------------------------------------------------------------

class TestAvailablePersonalities:
    def test_lists_personality_stems(self, tmp_personalities):
        stack = PromptStack(personalities_dir=tmp_personalities)
        avail = stack.available_personalities()
        assert sorted(avail) == ["adversarial", "minimalist"]

    def test_empty_when_dir_missing(self, tmp_path):
        stack = PromptStack(personalities_dir=tmp_path / "ghost_dir")
        assert stack.available_personalities() == []

    def test_sorted_alphabetically(self, tmp_personalities):
        avail = PromptStack(personalities_dir=tmp_personalities).available_personalities()
        assert avail == sorted(avail)


# ---------------------------------------------------------------------------
# from_config
# ---------------------------------------------------------------------------

class TestFromConfig:
    def test_full_config(self, tmp_path):
        pd = tmp_path / "personalities"
        pd.mkdir()
        (tmp_path / "SOUL.md").write_text("soul", encoding="utf-8")
        (tmp_path / "Agent.md").write_text("agent", encoding="utf-8")
        (pd / "adversarial.md").write_text("adv", encoding="utf-8")

        config = {
            "identity": {
                "soul": "SOUL.md",
                "agent": "Agent.md",
                "personality": "personalities/adversarial.md",
                "personalities_dir": "personalities",
            }
        }
        stack = PromptStack.from_config(config, base_dir=tmp_path)
        result = stack.load()
        sep = PromptStack.LAYER_SEPARATOR
        assert result == f"soul{sep}agent{sep}adv"

    def test_no_identity_section(self, tmp_path):
        (tmp_path / "SOUL.md").write_text("soul", encoding="utf-8")
        stack = PromptStack.from_config({}, base_dir=tmp_path)
        # Default soul path is SOUL.md relative to base_dir
        result = stack.load()
        assert result == "soul"

    def test_empty_personality_string_treated_as_none(self, tmp_path):
        (tmp_path / "SOUL.md").write_text("soul", encoding="utf-8")
        config = {"identity": {"soul": "SOUL.md", "personality": ""}}
        stack = PromptStack.from_config(config, base_dir=tmp_path)
        stack.load()
        assert "personality" not in stack.layer_names
        assert stack.current_personality is None

    def test_relative_paths_resolved_to_base_dir(self, tmp_path):
        (tmp_path / "SOUL.md").write_text("soul content", encoding="utf-8")
        config = {"identity": {"soul": "SOUL.md"}}
        stack = PromptStack.from_config(config, base_dir=tmp_path)
        assert stack.load() == "soul content"

    def test_missing_optional_fields_produce_no_layers(self, tmp_path):
        # No SOUL.md exists in tmp_path
        config = {"identity": {}}
        stack = PromptStack.from_config(config, base_dir=tmp_path)
        assert stack.load() == ""
