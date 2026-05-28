"""
Tests for the daily-weave plan reader (issue #461).

The reader is the data feed for today's chime *content* — paired with
``rhythm.py`` from PR2 which provides the stable per-phase scaffolding.
Tolerant by contract: a missing or broken weave file never blocks phase
chimes; they just fall through to the rhythm meaning alone.
"""

from __future__ import annotations

from pathlib import Path

from loom.autonomy.circadian.weave import (
    DEFAULT_WEAVE_PATH,
    WeavePlan,
    load_weave,
)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TestLoadWeave:
    def test_missing_file_returns_empty(self, tmp_path):
        plan = load_weave(tmp_path / "absent.md")
        assert plan.sections == {}
        assert plan.section_for("dawn") is None

    def test_parses_h2_sections(self, tmp_path):
        p = tmp_path / "weave.md"
        _write(p, """
# 今日織程
date: 2026-05-28

## dawn
- recall 近期記憶
- 跟 DK 說早安

## shared_learning
- 讀 HN top 10
- 挑 1-2 件想分享的

## evening_closure
- 道晚安
- 收織
""")
        plan = load_weave(p)
        assert set(plan.sections) == {"dawn", "shared_learning", "evening_closure"}
        assert "recall" in plan.section_for("dawn")
        assert "HN top 10" in plan.section_for("shared_learning")

    def test_preserves_markdown_inner_content(self, tmp_path):
        p = tmp_path / "weave.md"
        _write(p, """
## deep_weave
- bullet A
  - nested
- bullet B

> blockquote line

```python
def x(): pass
```
""")
        body = load_weave(p).section_for("deep_weave")
        assert "nested" in body
        assert "blockquote line" in body
        assert "```python" in body
        assert "def x(): pass" in body

    def test_h2_with_no_body(self, tmp_path):
        p = tmp_path / "weave.md"
        _write(p, "## empty_phase\n\n## next_phase\n- something\n")
        plan = load_weave(p)
        # Empty body → section_for returns None (treat-as-missing semantics).
        assert plan.section_for("empty_phase") is None
        assert plan.section_for("next_phase") == "- something"

    def test_trailing_h2_with_no_body(self, tmp_path):
        p = tmp_path / "weave.md"
        _write(p, "## dawn\n- recall\n\n## evening_closure\n")
        plan = load_weave(p)
        assert plan.section_for("dawn") == "- recall"
        assert plan.section_for("evening_closure") is None

    def test_no_h2_returns_empty(self, tmp_path):
        p = tmp_path / "no_h2.md"
        _write(p, "# Title only\n\nSome prose with no level-2 headings.\n")
        plan = load_weave(p)
        assert plan.sections == {}

    def test_duplicate_h2_last_wins(self, tmp_path):
        p = tmp_path / "dupes.md"
        _write(p, "## dawn\n- first version\n\n## dawn\n- second version\n")
        plan = load_weave(p)
        assert plan.section_for("dawn") == "- second version"

    def test_h2_with_trailing_whitespace_in_heading(self, tmp_path):
        p = tmp_path / "ws.md"
        _write(p, "##   dawn   \n- recall\n")
        plan = load_weave(p)
        assert plan.section_for("dawn") == "- recall"

    def test_h3_not_treated_as_h2(self, tmp_path):
        """An H3 inside a section is part of the H2 body, not a new section."""
        p = tmp_path / "nested.md"
        _write(p, "## dawn\n### subsection of dawn\n- detail\n")
        plan = load_weave(p)
        assert "dawn" in plan.sections
        assert "### subsection of dawn" in plan.section_for("dawn")

    def test_default_path_is_workspace_relative(self):
        assert DEFAULT_WEAVE_PATH == Path("autonomy/circadian/daily_weave.md")

    def test_empty_plan_lookup_returns_none(self):
        assert WeavePlan().section_for("anything") is None
