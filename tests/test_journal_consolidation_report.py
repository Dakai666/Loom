"""
Tests for the convergent-dream report appender (#488 P1; #499 slice 2d).

The dream report is a SYSTEM-generated artifact written by code, not the agent.

#499 finding (real run, 2026-06-01): the report originally landed in the SAME
dated file as the agent's life journal (``autonomy/circadian/journal/``). At
real volume the report dwarfed the day's journal entries and drowned them out
(DK had to hand-restore the file). The two-phase-same-file decision (spec §8)
was falsified under load, so the report now writes to its OWN dated path,
``autonomy/circadian/dreams/YYYY-MM-DD.md``, parallel to journal/. It still must
NOT pollute the agent-facing ``journal_append`` tool's ``kind`` enum.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from loom.autonomy.circadian.journal import (
    DEFAULT_DREAMS_DIR,
    DEFAULT_JOURNAL_DIR,
    KIND_LABELS,
    append_consolidation_report,
    dream_path_for,
)


def _today(tz: str = "Asia/Taipei") -> str:
    return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")


class TestAppendConsolidationReport:
    def test_writes_dated_entry_with_label(self, tmp_path):
        path = append_consolidation_report(
            "### 摘要\n- nothing merged\n", timezone="Asia/Taipei", dreams_dir=tmp_path,
        )
        assert path == dream_path_for(_today(), tmp_path)
        text = path.read_text(encoding="utf-8")
        assert "· 夢境鞏固" in text
        assert "### 摘要" in text
        assert "nothing merged" in text

    def test_lazy_h1_header_on_first_write(self, tmp_path):
        path = append_consolidation_report("body", dreams_dir=tmp_path)
        text = path.read_text(encoding="utf-8")
        # dreams file carries its own header, not the life-journal "Journal" one
        assert text.startswith(f"# {_today()} Dream Consolidation")

    def test_appends_without_clobbering(self, tmp_path):
        append_consolidation_report("first pass body", dreams_dir=tmp_path)
        path = append_consolidation_report("second pass body", dreams_dir=tmp_path)
        text = path.read_text(encoding="utf-8")
        assert "first pass body" in text
        assert "second pass body" in text
        # H1 header written exactly once
        assert text.count("Dream Consolidation") == 1

    def test_does_not_pollute_agent_kind_enum(self):
        # The agent-facing journal_append tool must not gain a system kind.
        assert "dream_consolidation" not in KIND_LABELS
        assert set(KIND_LABELS) == {"moment", "finding", "keepsake", "tomorrow"}

    def test_returns_path(self, tmp_path):
        path = append_consolidation_report("x", dreams_dir=tmp_path)
        assert path.exists()


class TestSeparateFromLifeJournal:
    """2d: the dream report must not share a file with the life journal."""

    def test_default_dreams_dir_distinct_from_journal_dir(self):
        assert DEFAULT_DREAMS_DIR != DEFAULT_JOURNAL_DIR
        assert DEFAULT_DREAMS_DIR.name == "dreams"
        assert DEFAULT_JOURNAL_DIR.name == "journal"

    def test_dream_path_for_uses_dreams_dir_by_default(self):
        assert dream_path_for("2026-06-01") == DEFAULT_DREAMS_DIR / "2026-06-01.md"
