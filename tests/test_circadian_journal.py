"""
Tests for the journal_append tool (PR5, issue #463).

Per doc/56 §10: the weave journal is the dated artifact for life fragments
that shouldn't pollute semantic memory. True append at file level, lazy
creation, dated paths, kind enum guidance.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from loom.autonomy.circadian.journal import (
    DEFAULT_JOURNAL_DIR,
    KIND_LABELS,
    journal_path_for,
    make_journal_append_tool,
)
from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import ToolCapability, TrustLevel


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield


def _call(args: dict) -> ToolCall:
    return ToolCall(
        tool_name="journal_append",
        args=args,
        trust_level=TrustLevel.SAFE,
        session_id="test-session",
    )


# ===========================================================================
# Tool definition shape
# ===========================================================================


class TestToolDefinition:
    def test_safe_trust_level(self):
        tool = make_journal_append_tool()
        assert tool.trust_level is TrustLevel.SAFE

    def test_mutates_capability(self):
        tool = make_journal_append_tool()
        assert tool.capabilities & ToolCapability.MUTATES

    def test_kind_enum_in_schema(self):
        tool = make_journal_append_tool()
        kind_schema = tool.input_schema["properties"]["kind"]
        assert set(kind_schema["enum"]) == set(KIND_LABELS)

    def test_required_fields(self):
        tool = make_journal_append_tool()
        assert set(tool.input_schema["required"]) == {"kind", "body"}


# ===========================================================================
# Happy-path append behaviour
# ===========================================================================


class TestAppend:
    def test_creates_file_lazily_with_h1_header(self):
        tool = make_journal_append_tool(timezone="Asia/Taipei")
        result = asyncio.run(tool.executor(_call({"kind": "moment", "body": "喵吉早安"})))

        assert result.success
        today = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
        path = Path(f"autonomy/circadian/journal/{today}.md")
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert text.startswith(f"# {today} Journal\n\n")
        assert "## " in text  # an H2 entry exists
        assert "生活片段" in text  # Chinese display label
        assert "喵吉早安" in text

    def test_second_call_appends_no_duplicate_header(self):
        tool = make_journal_append_tool(timezone="Asia/Taipei")
        asyncio.run(tool.executor(_call({"kind": "moment", "body": "一"})))
        asyncio.run(tool.executor(_call({"kind": "finding", "body": "二"})))

        today = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
        text = Path(f"autonomy/circadian/journal/{today}.md").read_text("utf-8")
        # Exactly one H1 header
        assert text.count(f"# {today} Journal") == 1
        # Two H2 entries (with one matching each kind)
        assert "生活片段" in text
        assert "有趣發現" in text
        assert text.index("一") < text.index("二")  # order preserved

    def test_each_entry_has_timestamp_and_label(self):
        tool = make_journal_append_tool(timezone="Asia/Taipei")
        asyncio.run(tool.executor(_call({"kind": "keepsake", "body": "x"})))

        today = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
        text = Path(f"autonomy/circadian/journal/{today}.md").read_text("utf-8")
        # ## HH:MM · 留念
        assert re.search(r"^## \d{2}:\d{2} · 留念$", text, re.MULTILINE)

    def test_markdown_body_preserved_verbatim(self):
        tool = make_journal_append_tool(timezone="Asia/Taipei")
        body = "- bullet one\n- bullet two\n\n> blockquote\n```\nfence\n```"
        asyncio.run(tool.executor(_call({"kind": "finding", "body": body})))

        today = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
        text = Path(f"autonomy/circadian/journal/{today}.md").read_text("utf-8")
        assert body in text

    def test_dated_path_uses_engine_timezone(self, tmp_path):
        # Confirm path uses the configured timezone, not wall-clock UTC. Pick
        # an extreme TZ so today-in-Pacific/Kiritimati differs from UTC by 14h.
        tool = make_journal_append_tool(timezone="Pacific/Kiritimati")
        asyncio.run(tool.executor(_call({"kind": "moment", "body": "x"})))
        expected_date = datetime.now(ZoneInfo("Pacific/Kiritimati")).strftime("%Y-%m-%d")
        assert Path(f"autonomy/circadian/journal/{expected_date}.md").exists()

    def test_custom_journal_dir(self, tmp_path):
        custom = tmp_path / "alt/journal"
        tool = make_journal_append_tool(timezone="Asia/Taipei", journal_dir=custom)
        asyncio.run(tool.executor(_call({"kind": "moment", "body": "x"})))
        today = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
        assert (custom / f"{today}.md").exists()


# ===========================================================================
# Validation
# ===========================================================================


class TestValidation:
    def test_unknown_kind_rejected(self):
        tool = make_journal_append_tool()
        result = asyncio.run(tool.executor(_call({"kind": "rant", "body": "x"})))
        assert not result.success
        assert "kind" in result.error
        # File should NOT be created on validation failure
        today = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
        assert not Path(f"autonomy/circadian/journal/{today}.md").exists()

    def test_empty_body_rejected(self):
        tool = make_journal_append_tool()
        result = asyncio.run(tool.executor(_call({"kind": "moment", "body": "   "})))
        assert not result.success
        assert "body" in result.error
        today = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
        assert not Path(f"autonomy/circadian/journal/{today}.md").exists()

    def test_missing_kind_rejected(self):
        tool = make_journal_append_tool()
        result = asyncio.run(tool.executor(_call({"body": "x"})))
        assert not result.success
        assert "kind" in result.error

    def test_missing_body_rejected(self):
        tool = make_journal_append_tool()
        result = asyncio.run(tool.executor(_call({"kind": "moment"})))
        assert not result.success
        assert "body" in result.error


# ===========================================================================
# Path helpers
# ===========================================================================


class TestJournalPath:
    def test_default_base(self):
        assert journal_path_for("2026-05-28") == DEFAULT_JOURNAL_DIR / "2026-05-28.md"

    def test_custom_base(self, tmp_path):
        p = journal_path_for("2026-05-28", tmp_path / "j")
        assert p == tmp_path / "j" / "2026-05-28.md"


# ===========================================================================
# Acceptance criteria from issue #463
# ===========================================================================


class TestAcceptanceCriteria:
    """The three boxes in issue #463 / doc/57 §7 PR5."""

    def test_ac1_full_day_yields_non_empty_journal(self):
        """跑完一天有一份非空 journal — simulated by a sequence of appends
        spread across the four kinds."""
        tool = make_journal_append_tool(timezone="Asia/Taipei")
        for kind, body in [
            ("moment", "10:00 餵了喵吉"),
            ("finding", "讀到一篇談 timezone 的有趣文章"),
            ("keepsake", "DK 今天笑了"),
            ("tomorrow", "想試試早起一小時"),
        ]:
            r = asyncio.run(tool.executor(_call({"kind": kind, "body": body})))
            assert r.success

        today = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
        text = Path(f"autonomy/circadian/journal/{today}.md").read_text("utf-8")
        assert text.strip()
        for label in KIND_LABELS.values():
            assert label in text

    def test_ac2_journal_writes_never_touch_memory(self):
        """semantic memory 沒被流水帳污染. Structural assertion: the journal
        module imports nothing from loom.core.memory, so it cannot write
        to MemoryGovernor / SemanticMemory by construction."""
        import ast
        import loom.autonomy.circadian.journal as journal_mod
        tree = ast.parse(Path(journal_mod.__file__).read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("loom.core.memory"), (
                    f"journal.py must not import from memory layer; "
                    f"found `from {node.module}`"
                )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("loom.core.memory"), (
                        f"journal.py must not import memory layer; "
                        f"found `import {alias.name}`"
                    )

    def test_ac3_dated_file_not_rolling(self):
        """journal 檔案是 dated（每天一份），不是 rolling. Two appends with
        different ``timezone``-resolved dates land in different files."""
        # Two tools, two timezones 24h apart → guaranteed different dates.
        tool_a = make_journal_append_tool(timezone="Pacific/Kiritimati")
        tool_b = make_journal_append_tool(timezone="Pacific/Pago_Pago")
        asyncio.run(tool_a.executor(_call({"kind": "moment", "body": "a"})))
        asyncio.run(tool_b.executor(_call({"kind": "moment", "body": "b"})))

        files = sorted(Path("autonomy/circadian/journal").glob("*.md"))
        assert len(files) == 2  # two distinct dated files, no rolling
