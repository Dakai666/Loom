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
    diagnose_join,
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


class TestDiagnoseJoin:
    """Issue #565: the weave file's H2 headings are the join key onto
    ``rhythm.toml`` anchor names. On 2026-07-16 the file's layout was
    reworked to program-shaped headings (今日 Program / 今日重點 / …) and
    the join silently went dead for six weeks — every phase chime quietly
    fell through to ``meaning`` alone, and the agent, seeing only the
    absence, diagnosed a *write* failure that never happened.

    A full miss on a non-empty file is the one unambiguous signature of
    that break, so that is exactly what we report — nothing else, to keep
    the warning free of false positives.
    """

    ANCHORS = ["dawn", "shared_learning", "pet", "curiosity", "evening_closure"]

    def test_full_miss_on_non_empty_file_is_reported(self, tmp_path):
        p = tmp_path / "weave.md"
        _write(p, "## 今日 Program\n- default\n\n## 今日重點\n- 喵吉\n")
        plan = load_weave(p)

        warning = diagnose_join(plan, self.ANCHORS)

        assert warning is not None
        # Names both sides of the broken join so the reader can act.
        assert "今日 Program" in warning
        assert "dawn" in warning

    def test_partial_match_is_healthy(self, tmp_path):
        """A phase with no section is normal and must not warn — only a
        *total* miss indicates the join key itself is wrong."""
        p = tmp_path / "weave.md"
        _write(p, "## dawn\n- 早安\n\n## 今日重點\n- 喵吉\n")
        plan = load_weave(p)

        assert diagnose_join(plan, self.ANCHORS) is None

    def test_full_match_is_healthy(self, tmp_path):
        p = tmp_path / "weave.md"
        _write(p, "## dawn\n- 早安\n\n## pet\n- 喵吉\n")
        plan = load_weave(p)

        assert diagnose_join(plan, self.ANCHORS) is None

    def test_empty_plan_does_not_warn(self, tmp_path):
        """Missing file / no H2s is a supported state (fresh install) and
        load_weave already logs it — warning again would be noise."""
        assert diagnose_join(load_weave(tmp_path / "absent.md"), self.ANCHORS) is None

    def test_no_anchors_does_not_warn(self, tmp_path):
        """No rhythm table means there is no join to be broken."""
        p = tmp_path / "weave.md"
        _write(p, "## 今日 Program\n- default\n")
        assert diagnose_join(load_weave(p), []) is None

    def test_warning_is_bounded(self, tmp_path):
        """The weave file can hold a lot of sections; the warning must stay
        a warning and not paste the whole table of contents."""
        p = tmp_path / "weave.md"
        _write(p, "".join(f"## 區段{i}\n- x\n\n" for i in range(40)))

        warning = diagnose_join(load_weave(p), self.ANCHORS)

        assert warning is not None
        assert len(warning) < 700
