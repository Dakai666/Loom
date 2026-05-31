"""
Tests for the convergent-dream report appender (#488, P1, spec §8).

The dream report is a SYSTEM-generated artifact written by code, not the agent.
It lands in the same dated circadian journal so the file becomes "絲絲's
two-phase dream record" (grep-able), but it must NOT pollute the agent-facing
``journal_append`` tool's ``kind`` enum (which is for daily life-texture only).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from loom.autonomy.circadian.journal import (
    KIND_LABELS,
    append_consolidation_report,
    journal_path_for,
)


def _today(tz: str = "Asia/Taipei") -> str:
    return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")


class TestAppendConsolidationReport:
    def test_writes_dated_entry_with_label(self, tmp_path):
        path = append_consolidation_report(
            "### 摘要\n- nothing merged\n", timezone="Asia/Taipei", journal_dir=tmp_path,
        )
        assert path == journal_path_for(_today(), tmp_path)
        text = path.read_text(encoding="utf-8")
        assert "· 夢境鞏固" in text
        assert "### 摘要" in text
        assert "nothing merged" in text

    def test_lazy_h1_header_on_first_write(self, tmp_path):
        path = append_consolidation_report("body", journal_dir=tmp_path)
        text = path.read_text(encoding="utf-8")
        assert text.startswith(f"# {_today()} Journal")

    def test_appends_without_clobbering(self, tmp_path):
        append_consolidation_report("first pass body", journal_dir=tmp_path)
        path = append_consolidation_report("second pass body", journal_dir=tmp_path)
        text = path.read_text(encoding="utf-8")
        assert "first pass body" in text
        assert "second pass body" in text
        # H1 header written exactly once
        assert text.count("Journal") == 1

    def test_does_not_pollute_agent_kind_enum(self):
        # The agent-facing journal_append tool must not gain a system kind.
        assert "dream_consolidation" not in KIND_LABELS
        assert set(KIND_LABELS) == {"moment", "finding", "keepsake", "tomorrow"}

    def test_returns_path(self, tmp_path):
        path = append_consolidation_report("x", journal_dir=tmp_path)
        assert path.exists()
