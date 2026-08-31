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


class TestFencedCodeBlockIsolation:
    """PR #480 review P2: H2-looking lines *inside* a fenced code block must
    not be treated as section boundaries — otherwise a weave section that
    shows a markdown example would lose everything after the fake heading."""

    def test_h2_inside_backtick_fence_is_body(self, tmp_path):
        """The exact reviewer repro."""
        p = tmp_path / "weave.md"
        p.write_text(
            "## deep_weave\n"
            "- before\n"
            "\n"
            "```markdown\n"
            "## not_a_phase\n"
            "inside code\n"
            "```\n"
            "\n"
            "- after\n"
            "\n"
            "## evening_closure\n"
            "- close\n",
            encoding="utf-8",
        )
        plan = load_weave(p)
        assert set(plan.sections) == {"deep_weave", "evening_closure"}
        deep = plan.section_for("deep_weave")
        assert "- before" in deep
        assert "```markdown" in deep
        assert "## not_a_phase" in deep   # preserved as body, not promoted
        assert "inside code" in deep
        assert "- after" in deep
        assert plan.section_for("evening_closure") == "- close"

    def test_h2_inside_tilde_fence_is_body(self, tmp_path):
        p = tmp_path / "tilde.md"
        p.write_text(
            "## dawn\n"
            "~~~\n"
            "## not_a_phase\n"
            "~~~\n"
            "- after\n",
            encoding="utf-8",
        )
        plan = load_weave(p)
        assert set(plan.sections) == {"dawn"}
        assert "## not_a_phase" in plan.section_for("dawn")
        assert "- after" in plan.section_for("dawn")

    def test_fence_with_info_string(self, tmp_path):
        """Real-world fences carry an info string (` ```python `)."""
        p = tmp_path / "info.md"
        p.write_text(
            "## dawn\n"
            "```python\n"
            "## comment-looking-line\n"
            "x = 1\n"
            "```\n",
            encoding="utf-8",
        )
        plan = load_weave(p)
        assert set(plan.sections) == {"dawn"}
        assert "## comment-looking-line" in plan.section_for("dawn")

    def test_unclosed_fence_eats_to_eof(self, tmp_path):
        """An unclosed fence is a user error, but it must not crash and must
        not let a later `## phase` punch through into a new section — that
        line is, per markdown semantics, still inside the open fence."""
        p = tmp_path / "unclosed.md"
        p.write_text(
            "## dawn\n"
            "```\n"
            "unclosed\n"
            "## looks_like_phase\n"
            "still inside fence\n",
            encoding="utf-8",
        )
        plan = load_weave(p)
        assert set(plan.sections) == {"dawn"}
        assert "## looks_like_phase" in plan.section_for("dawn")


class TestGlobalSections:
    """Issue #565: a weave section whose name matches no anchor is not a
    mistake — it is *global* content (今日 Program / 長線事項 carry / …),
    written for the whole day rather than one phase.

    The original per-phase-only delivery model had nowhere to put those, so
    they were parsed and then silently dropped. ``global_sections`` names
    the leftover set; the dawn chime is where it gets delivered.
    """

    ANCHORS = ["dawn", "shared_learning", "pet", "curiosity", "evening_closure"]

    def test_unclaimed_sections_are_global(self, tmp_path):
        p = tmp_path / "weave.md"
        _write(p, "## 今日 Program\n- default\n\n## 長線事項 carry\n- taste\n")
        plan = load_weave(p)

        assert list(plan.global_sections(self.ANCHORS)) == [
            "今日 Program", "長線事項 carry",
        ]

    def test_anchor_named_sections_are_not_global(self, tmp_path):
        """A phase-scoped section belongs to its phase and must not also be
        broadcast at dawn, or the agent reads it twice in one day."""
        p = tmp_path / "weave.md"
        _write(p, "## dawn\n- 早安\n\n## 今日 Program\n- default\n")
        plan = load_weave(p)

        assert list(plan.global_sections(self.ANCHORS)) == ["今日 Program"]
        assert plan.section_for("dawn") == "- 早安"

    def test_file_order_is_preserved(self, tmp_path):
        """The agent wrote these in a deliberate order; keep it."""
        p = tmp_path / "weave.md"
        _write(p, "".join(f"## 區段{i}\n- x\n\n" for i in range(6)))

        assert list(load_weave(p).global_sections(self.ANCHORS)) == [
            f"區段{i}" for i in range(6)
        ]

    def test_empty_body_sections_are_dropped(self, tmp_path):
        """A heading with nothing under it is a placeholder, not content."""
        p = tmp_path / "weave.md"
        _write(p, "## 今日 Program\n\n## 長線事項\n- taste\n")

        assert list(load_weave(p).global_sections(self.ANCHORS)) == ["長線事項"]

    def test_all_anchors_leaves_nothing_global(self, tmp_path):
        p = tmp_path / "weave.md"
        _write(p, "## dawn\n- 早安\n\n## pet\n- 喵吉\n")

        assert load_weave(p).global_sections(self.ANCHORS) == {}

    def test_no_anchors_makes_everything_global(self, tmp_path):
        """No rhythm table → nothing is phase-scoped, so it is all global."""
        p = tmp_path / "weave.md"
        _write(p, "## 今日 Program\n- default\n")

        assert list(load_weave(p).global_sections([])) == ["今日 Program"]

    def test_empty_plan_has_no_globals(self, tmp_path):
        assert load_weave(tmp_path / "absent.md").global_sections(self.ANCHORS) == {}
