"""
Tests for Issue #56: Skill on-demand loading and progressive disclosure.

Covers:
- Frontmatter parsing
- SkillCatalogEntry and MemoryIndex rendering
- load_skill tool structured output
- SkillOutcomeTracker assessment parsing
- Auto-import from skills/ directory
"""

import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from loom.core.memory.index import MemoryIndex, SkillCatalogEntry


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


class TestParseSkillFrontmatter:
    """Test YAML frontmatter extraction from SKILL.md files."""

    def test_valid_frontmatter(self):
        from loom.platform.cli.main import _parse_skill_frontmatter

        raw = """---
name: loom-engineer
description: Full implementation cycle from issue to PR.
---

# Loom Engineer

Some body content here.
"""
        name, desc, tags = _parse_skill_frontmatter(raw)
        assert name == "loom-engineer"
        assert "implementation" in desc.lower() or "PR" in desc
        assert isinstance(tags, list)

    def test_frontmatter_with_tags(self):
        from loom.platform.cli.main import _parse_skill_frontmatter

        raw = """---
name: test-skill
description: A test skill.
tags:
  - coding
  - testing
---
Body.
"""
        name, desc, tags = _parse_skill_frontmatter(raw)
        assert name == "test-skill"
        assert tags == ["coding", "testing"]

    def test_no_frontmatter(self):
        from loom.platform.cli.main import _parse_skill_frontmatter

        name, desc, tags = _parse_skill_frontmatter("# Just a markdown file")
        assert name == ""
        assert desc == ""

    def test_malformed_yaml(self):
        from loom.platform.cli.main import _parse_skill_frontmatter

        raw = """---
name: broken
description: Use this when: the user asks
---
Body.
"""
        # Should handle colons in description gracefully or fail gracefully
        name, desc, tags = _parse_skill_frontmatter(raw)
        # Either parses or returns empty — both are acceptable
        assert isinstance(name, str)
        assert isinstance(desc, str)

    def test_missing_description(self):
        from loom.platform.cli.main import _parse_skill_frontmatter

        raw = """---
name: no-desc
---
Body.
"""
        name, desc, tags = _parse_skill_frontmatter(raw)
        assert name == "no-desc"
        assert desc == ""


# ---------------------------------------------------------------------------
# MemoryIndex rendering with skill catalog
# ---------------------------------------------------------------------------


class TestMemoryIndexSkillCatalog:
    """Test that MemoryIndex renders <available_skills> XML correctly."""

    def test_render_with_catalog(self):
        index = MemoryIndex(
            semantic_count=10,
            semantic_topics=["python", "loom"],
            skill_count=2,
            skill_catalog=[
                SkillCatalogEntry(
                    name="loom-engineer",
                    description="Full implementation cycle.",
                ),
                SkillCatalogEntry(
                    name="code-analyst",
                    description="Deep code analysis.",
                ),
            ],
        )
        rendered = index.render()

        assert "<available_skills>" in rendered
        assert "</available_skills>" in rendered
        assert "<name>loom-engineer</name>" in rendered
        assert "<name>code-analyst</name>" in rendered
        assert "<description>Full implementation cycle.</description>" in rendered
        assert "load_skill(name)" in rendered

    def test_render_without_catalog(self):
        index = MemoryIndex(
            semantic_count=5,
            skill_count=0,
        )
        rendered = index.render()

        assert "<available_skills>" not in rendered

    def test_render_fallback_tags(self):
        """When skills exist in DB but no catalog, show tags as fallback."""
        index = MemoryIndex(
            semantic_count=5,
            skill_count=3,
            skill_tags=["coding", "bash"],
            skill_catalog=[],  # empty catalog
        )
        rendered = index.render()

        assert "<available_skills>" not in rendered
        assert "coding" in rendered

    def test_catalog_entry_fields(self):
        entry = SkillCatalogEntry(
            name="test-skill",
            description="Test description",
            location="/path/to/SKILL.md",
        )
        assert entry.name == "test-skill"
        assert entry.description == "Test description"
        assert entry.location == "/path/to/SKILL.md"


# ---------------------------------------------------------------------------
# load_skill tool helpers
# ---------------------------------------------------------------------------


class TestStripFrontmatter:
    """Test frontmatter stripping from skill body."""

    def test_strip_removes_yaml(self):
        from loom.platform.cli.tools import _strip_frontmatter

        body = """---
name: test
description: A test.
---

# Test Skill

Instructions here.
"""
        result = _strip_frontmatter(body)
        assert "# Test Skill" in result
        assert "name: test" not in result

    def test_no_frontmatter_passthrough(self):
        from loom.platform.cli.tools import _strip_frontmatter

        body = "# Just markdown\n\nNo frontmatter."
        assert _strip_frontmatter(body) == body


class TestFindSkillResources:
    """Test skill directory resource discovery."""

    def test_finds_directory(self, tmp_path):
        from loom.platform.cli.tools import _find_skill_resources

        # Create a skill directory with resources
        skill_dir = tmp_path / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Test")
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "helper.py").write_text("# helper")

        dir_path, resources = _find_skill_resources(
            "test-skill", [tmp_path / "skills"]
        )
        assert dir_path is not None
        assert "scripts\\helper.py" in resources or "scripts/helper.py" in resources

    def test_not_found(self, tmp_path):
        from loom.platform.cli.tools import _find_skill_resources

        dir_path, resources = _find_skill_resources(
            "nonexistent", [tmp_path]
        )
        assert dir_path is None
        assert resources == []

    def test_underscore_hyphen_variant(self, tmp_path):
        from loom.platform.cli.tools import _find_skill_resources

        # Skill stored with underscore, queried with hyphen
        skill_dir = tmp_path / "skills" / "loom_engineer"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Test")

        dir_path, resources = _find_skill_resources(
            "loom-engineer", [tmp_path / "skills"]
        )
        assert dir_path is not None


# ---------------------------------------------------------------------------
# SkillOutcomeTracker
# ---------------------------------------------------------------------------


class TestSkillOutcomeTracker:
    """Test the outcome tracker's assessment parsing."""

    def test_parse_valid_json(self):
        from loom.core.memory.skill_outcome import _parse_assessment

        score, summary = _parse_assessment(
            '{"score": 4, "summary": "Good execution with minor issues."}'
        )
        assert score == 4
        assert "Good execution" in summary

    def test_parse_markdown_wrapped(self):
        from loom.core.memory.skill_outcome import _parse_assessment

        score, summary = _parse_assessment(
            '```json\n{"score": 5, "summary": "Excellent."}\n```'
        )
        assert score == 5

    def test_parse_invalid_score(self):
        from loom.core.memory.skill_outcome import _parse_assessment

        score, summary = _parse_assessment('{"score": 7, "summary": "Too high."}')
        assert score is None

    def test_parse_garbage(self):
        from loom.core.memory.skill_outcome import _parse_assessment

        score, summary = _parse_assessment("I did a great job!")
        assert score is None

    def test_record_activation(self):
        from loom.core.memory.skill_outcome import SkillOutcomeTracker

        tracker = SkillOutcomeTracker(
            procedural=MagicMock(),
            semantic=MagicMock(),
            session_id="test-session",
        )
        tracker.record_activation("test-skill", 1)
        assert "test-skill" in tracker.activated_skills
        assert tracker.has_active_skills()

    def test_record_tool_usage(self):
        from loom.core.memory.skill_outcome import SkillOutcomeTracker

        tracker = SkillOutcomeTracker(
            procedural=MagicMock(),
            semantic=MagicMock(),
            session_id="test-session",
        )
        tracker.record_tool_usage()
        tracker.record_tool_usage()
        assert tracker._turn_tool_count == 2


# ---------------------------------------------------------------------------
# SkillEvolutionHook
# ---------------------------------------------------------------------------


class TestSkillEvolutionHook:
    """Test evolution hook trigger conditions."""

    def test_should_evolve_low_confidence(self):
        from loom.core.cognition.counter_factual import SkillEvolutionHook

        hook = SkillEvolutionHook(
            router=MagicMock(),
            model="test",
            procedural=MagicMock(),
            semantic=MagicMock(),
        )

        # Mock skill with low confidence
        skill = MagicMock()
        skill.confidence = 0.4
        skill.usage_count = 5

        assert hook._should_evolve(skill) is True

    def test_should_not_evolve_high_confidence(self):
        from loom.core.cognition.counter_factual import SkillEvolutionHook

        hook = SkillEvolutionHook(
            router=MagicMock(),
            model="test",
            procedural=MagicMock(),
            semantic=MagicMock(),
        )

        skill = MagicMock()
        skill.confidence = 0.8
        skill.usage_count = 5

        assert hook._should_evolve(skill) is False

    def test_should_not_evolve_low_usage(self):
        from loom.core.cognition.counter_factual import SkillEvolutionHook

        hook = SkillEvolutionHook(
            router=MagicMock(),
            model="test",
            procedural=MagicMock(),
            semantic=MagicMock(),
        )

        skill = MagicMock()
        skill.confidence = 0.3
        skill.usage_count = 1  # too few uses

        assert hook._should_evolve(skill) is False
