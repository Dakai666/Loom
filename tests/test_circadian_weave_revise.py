"""
Tests for the weave_revise tool + proposal artifact (PR4, issue #462).

DK's design (2026-05-28): no confirm round-trip. The tool atomically
revises tomorrow's daily_weave.md and writes a TOML audit artifact under
``proposals/applied/``; mtime conflict guard parks proposals under
``proposals/conflicts/`` instead so DK's hand-edits always win.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from loom.autonomy.circadian.proposal import (
    APPLIED_SUBDIR,
    CONFLICTS_SUBDIR,
    Change,
    PROPOSALS_DIR,
    WeaveProposal,
    apply_changes,
    load_proposal,
    make_weave_revise_tool,
    proposal_path,
    render_weave_markdown,
)
from loom.autonomy.circadian.weave import load_weave_for_revision
from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield


def _seed_weave(body: str) -> Path:
    p = Path("autonomy/circadian/daily_weave.md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _call(args: dict) -> ToolCall:
    return ToolCall(
        tool_name="weave_revise",
        args=args,
        trust_level=TrustLevel.SAFE,
        session_id="test-session",
    )


# ===========================================================================
# Pure transformations
# ===========================================================================


class TestApplyChanges:
    def test_add_appends_at_end(self):
        sections = {"dawn": "- a", "evening_closure": "- z"}
        new, err = apply_changes(sections, [
            Change(section="errand", action="add", new_body="- go to store"),
        ])
        assert err is None
        assert list(new) == ["dawn", "evening_closure", "errand"]
        assert new["errand"] == "- go to store"

    def test_remove_drops_section(self):
        new, err = apply_changes(
            {"dawn": "a", "pet": "b", "evening_closure": "c"},
            [Change(section="pet", action="remove")],
        )
        assert err is None
        assert list(new) == ["dawn", "evening_closure"]

    def test_replace_preserves_position(self):
        new, err = apply_changes(
            {"dawn": "a", "pet": "b", "evening_closure": "c"},
            [Change(section="pet", action="replace", new_body="NEW")],
        )
        assert err is None
        assert list(new) == ["dawn", "pet", "evening_closure"]
        assert new["pet"] == "NEW"

    def test_rename_preserves_position_and_body(self):
        new, err = apply_changes(
            {"dawn": "a", "shared_learning": "hn", "evening_closure": "c"},
            [Change(section="shared_learning", action="rename", to="reading_block")],
        )
        assert err is None
        assert list(new) == ["dawn", "reading_block", "evening_closure"]
        assert new["reading_block"] == "hn"  # body preserved when new_body omitted

    def test_rename_with_new_body_overrides(self):
        new, err = apply_changes(
            {"x": "old"},
            [Change(section="x", action="rename", to="y", new_body="fresh")],
        )
        assert err is None
        assert new == {"y": "fresh"}

    def test_unknown_section_errors_all_or_nothing(self):
        before = {"dawn": "a"}
        new, err = apply_changes(before, [
            Change(section="dawn", action="replace", new_body="ok"),
            Change(section="missing", action="remove"),
        ])
        assert err is not None and "missing" in err
        assert new == before  # original unchanged

    def test_add_collision_errors(self):
        new, err = apply_changes(
            {"dawn": "x"},
            [Change(section="dawn", action="add", new_body="y")],
        )
        assert err is not None and "already exists" in err

    def test_rename_target_collision_errors(self):
        new, err = apply_changes(
            {"a": "1", "b": "2"},
            [Change(section="a", action="rename", to="b")],
        )
        assert err is not None and "already exists" in err

    def test_invalid_action_errors(self):
        new, err = apply_changes(
            {"a": "1"},
            [Change(section="a", action="frobnicate")],
        )
        assert err is not None and "invalid action" in err


class TestRenderWeaveMarkdown:
    def test_preserves_prelude(self):
        text = render_weave_markdown(
            "# 今日織程\n\ndate: 2026-05-29\n",
            {"dawn": "- recall", "evening_closure": "- close"},
        )
        assert text.startswith("# 今日織程")
        assert "date: 2026-05-29" in text
        assert "## dawn\n- recall\n" in text
        assert text.endswith("\n")

    def test_empty_prelude(self):
        text = render_weave_markdown("", {"dawn": "- x"})
        assert text.startswith("## dawn")

    def test_section_order_preserved(self):
        text = render_weave_markdown("", {"c": "1", "a": "2", "b": "3"})
        assert text.index("## c") < text.index("## a") < text.index("## b")


# ===========================================================================
# Proposal artifact round-trip
# ===========================================================================


class TestProposalArtifact:
    def test_round_trip_through_toml(self, tmp_path):
        p = WeaveProposal(
            date="2026-05-28",
            phase="evening_closure",
            based_on_mtime=1234567890123456,  # st_mtime_ns is an int
            rationale="絲絲 想多睡半小時，dawn 從 08:00 改成 08:30 — 但這條走 rhythm 不走 weave；這份只調 deep_weave 內容。",
            changes=[
                Change(section="deep_weave", action="replace", new_body="- 讀 DDIA Ch.7"),
                Change(section="errand", action="add", new_body="- 買貓砂"),
                Change(section="curiosity", action="remove"),
            ],
        )
        target = tmp_path / "p.toml"
        from loom.autonomy.circadian.proposal import _save_proposal_toml
        _save_proposal_toml(p, target)
        back = load_proposal(target)
        assert back is not None
        assert back.date == p.date
        assert back.rationale == p.rationale
        assert len(back.changes) == 3
        assert back.changes[0].new_body == "- 讀 DDIA Ch.7"
        assert back.changes[2].action == "remove"

    def test_load_missing_returns_none(self, tmp_path):
        assert load_proposal(tmp_path / "absent.toml") is None

    def test_summary_lines_human_readable(self):
        p = WeaveProposal(
            date="2026-05-28", phase="evening_closure", based_on_mtime=0,
            rationale="", changes=[
                Change(section="x", action="add", new_body=""),
                Change(section="y", action="remove"),
                Change(section="a", action="rename", to="b"),
                Change(section="c", action="replace", new_body=""),
            ],
        )
        lines = p.summary_lines()
        assert any("新增" in l for l in lines)
        assert any("刪除" in l for l in lines)
        assert any("改名" in l for l in lines)
        assert any("內容換" in l for l in lines)


# ===========================================================================
# Tool — happy path
# ===========================================================================


class TestToolHappyPath:
    async def test_revise_writes_proposal_and_applies(self):
        wp = _seed_weave(
            "# 今日織程\n\ndate: 2026-05-28\n\n"
            "## dawn\n- recall\n\n"
            "## shared_learning\n- HN\n\n"
            "## evening_closure\n- close\n"
        )
        tool = make_weave_revise_tool()
        result = await tool.executor(_call({
            "rationale": "HN 沒看 — 改 reading_block",
            "changes": [
                {"section": "shared_learning", "action": "rename",
                 "to": "reading_block", "new_body": "- DDIA Ch.7"},
            ],
        }))
        assert result.success, result.error
        new_text = wp.read_text(encoding="utf-8")
        assert "## reading_block" in new_text
        assert "DDIA Ch.7" in new_text
        assert "## shared_learning" not in new_text
        # Prelude preserved
        assert "# 今日織程" in new_text
        assert "date: 2026-05-28" in new_text
        # Proposal archived to applied/
        applied = PROPOSALS_DIR / APPLIED_SUBDIR
        archived = list(applied.glob("*-evening.toml"))
        assert len(archived) == 1
        p = load_proposal(archived[0])
        assert p.rationale == "HN 沒看 — 改 reading_block"

    async def test_revise_missing_rationale_fails(self):
        _seed_weave("## dawn\n- x\n")
        tool = make_weave_revise_tool()
        result = await tool.executor(_call({
            "rationale": "  ",
            "changes": [{"section": "dawn", "action": "replace", "new_body": "y"}],
        }))
        assert not result.success
        assert "rationale" in result.error

    async def test_revise_empty_changes_fails(self):
        _seed_weave("## dawn\n- x\n")
        tool = make_weave_revise_tool()
        result = await tool.executor(_call({
            "rationale": "why",
            "changes": [],
        }))
        assert not result.success
        assert "changes" in result.error

    async def test_revise_no_weave_file_fails(self):
        tool = make_weave_revise_tool()
        result = await tool.executor(_call({
            "rationale": "why",
            "changes": [{"section": "dawn", "action": "add", "new_body": "x"}],
        }))
        assert not result.success
        # Unified "could not take a stable snapshot" covers both missing file
        # and mid-snapshot hand-edit (PR #481 P1 fix).
        assert "stable snapshot" in result.error

    async def test_invalid_change_does_not_touch_weave(self):
        wp = _seed_weave("## dawn\n- original\n")
        before = wp.read_text(encoding="utf-8")
        tool = make_weave_revise_tool()
        result = await tool.executor(_call({
            "rationale": "try to add a colliding section",
            "changes": [{"section": "dawn", "action": "add", "new_body": "boom"}],
        }))
        assert not result.success
        # daily_weave.md untouched on rejection
        assert wp.read_text(encoding="utf-8") == before


# ===========================================================================
# Mtime conflict guard — the protection DK explicitly required
# ===========================================================================


class TestMtimeConflict:
    async def test_concurrent_handedit_after_snapshot_parks_proposal(self, monkeypatch):
        """Post-snapshot race: DK edits between snapshot completion and the
        tool's pre-write stat. The existing guard catches this — proposal
        goes to conflicts/, file untouched."""
        wp = _seed_weave("## dawn\n- original\n")
        tool = make_weave_revise_tool()

        original_stat = Path.stat

        class _FakeStat:
            """Shim stat result that bumps st_mtime_ns to simulate a hand-edit."""
            def __init__(self, real, bump_ns):
                for attr in ("st_mode", "st_ino", "st_dev", "st_nlink",
                             "st_uid", "st_gid", "st_size", "st_atime",
                             "st_mtime", "st_ctime", "st_atime_ns",
                             "st_ctime_ns"):
                    setattr(self, attr, getattr(real, attr, 0))
                self.st_mtime_ns = real.st_mtime_ns + bump_ns

        def _fake_stat(self, *a, **kw):
            real = original_stat(self, *a, **kw)
            # Snapshot calls stat twice (before+after read). Only bump on the
            # *third* stat (the tool's pre-write guard) so the snapshot itself
            # is stable; the conflict surfaces at the post-snapshot check.
            if self.name == "daily_weave.md":
                _fake_stat._n += 1
                if _fake_stat._n >= 3:
                    return _FakeStat(real, 100_000_000)  # +100 ms in ns
            return real

        _fake_stat._n = 0
        monkeypatch.setattr(Path, "stat", _fake_stat)

        before = wp.read_text(encoding="utf-8")
        result = await tool.executor(_call({
            "rationale": "should be blocked",
            "changes": [{"section": "dawn", "action": "replace", "new_body": "new"}],
        }))
        assert not result.success
        assert "changed under us" in result.error
        # daily_weave.md untouched
        assert wp.read_text(encoding="utf-8") == before
        # Proposal landed in conflicts/, not applied/
        conflicts = PROPOSALS_DIR / CONFLICTS_SUBDIR
        applied = PROPOSALS_DIR / APPLIED_SUBDIR
        assert list(conflicts.glob("*-evening.toml"))
        assert not (applied.exists() and list(applied.glob("*-evening.toml")))

    async def test_handedit_during_snapshot_read_blocks_revise(self, monkeypatch):
        """PR #481 review P1: DK's edit lands *during* the snapshot read —
        between ``read_text()`` returning the old buffer and the post-read
        stat. The pre-fix code recorded the new mtime alongside stale
        content, and the post-snapshot guard then passed against the
        already-bumped mtime — silently overwriting DK's edit. The
        stat-read-stat snapshot must refuse the torn read.
        """
        wp = _seed_weave("## dawn\n- ORIGINAL\n")
        tool = make_weave_revise_tool()

        original_read = Path.read_text

        def _racy_read(self, *a, **kw):
            text = original_read(self, *a, **kw)
            if self.name == "daily_weave.md" and not getattr(_racy_read, "_done", False):
                _racy_read._done = True
                # Simulate DK hand-edit landing between the snapshot's
                # initial stat and the post-read stat.
                time.sleep(0.005)  # ensure st_mtime_ns ticks
                self.write_text("## dawn\n- DK HAND EDIT\n", encoding="utf-8")
            return text

        monkeypatch.setattr(Path, "read_text", _racy_read)

        result = await tool.executor(_call({
            "rationale": "should be blocked by snapshot race",
            "changes": [{"section": "dawn", "action": "replace",
                         "new_body": "- AGENT WRITE"}],
        }))
        assert not result.success
        assert "stable snapshot" in result.error
        # DK's hand-edit preserved on disk — the agent write never happened
        assert wp.read_text(encoding="utf-8") == "## dawn\n- DK HAND EDIT\n"
        # No applied artifact was created — the tool refused before write
        applied = PROPOSALS_DIR / APPLIED_SUBDIR
        assert not (applied.exists() and list(applied.glob("*-evening.toml")))


# ===========================================================================
# Prelude preservation (PR4 design: DK header survives)
# ===========================================================================


class TestPreludePreservation:
    async def test_user_authored_header_survives(self):
        wp = _seed_weave(
            "# 今日織程\n\n"
            "date: 2026-05-28\n"
            "mood: rainy ☔\n\n"
            "DK's note: 今天要記得多喝水\n\n"
            "## dawn\n- recall\n"
        )
        tool = make_weave_revise_tool()
        result = await tool.executor(_call({
            "rationale": "add an errand",
            "changes": [{"section": "errand", "action": "add", "new_body": "- 買貓砂"}],
        }))
        assert result.success
        text = wp.read_text(encoding="utf-8")
        assert "mood: rainy ☔" in text
        assert "DK's note: 今天要記得多喝水" in text
        assert "## dawn" in text
        assert "## errand" in text

    async def test_fence_in_prelude_with_h2_inside_does_not_split(self):
        """PR3 lesson surfaces here too — H2 inside a fence in the prelude
        must not trick the splitter into ending the prelude early."""
        wp = _seed_weave(
            "# 今日織程\n\n"
            "```markdown\n"
            "## not a real section\n"
            "```\n\n"
            "## dawn\n- recall\n"
        )
        tool = make_weave_revise_tool()
        result = await tool.executor(_call({
            "rationale": "noop replace to test prelude",
            "changes": [{"section": "dawn", "action": "replace", "new_body": "- still ok"}],
        }))
        assert result.success
        text = wp.read_text(encoding="utf-8")
        assert "```markdown" in text
        assert "## not a real section" in text
        assert "## dawn" in text
        assert "- still ok" in text


# ===========================================================================
# load_weave_for_revision sanity
# ===========================================================================


class TestLoadWeaveForRevision:
    def test_returns_prelude_sections_mtime(self):
        wp = _seed_weave("# header\n\n## a\nbody\n")
        snap = load_weave_for_revision(wp)
        assert snap is not None
        prelude, sections, mtime = snap
        assert "# header" in prelude
        assert sections == {"a": "body"}
        assert mtime > 0

    def test_missing_returns_none(self, tmp_path):
        assert load_weave_for_revision(tmp_path / "absent.md") is None
