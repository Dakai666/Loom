"""R2-1: trusted base aliases work; links below that base remain hostile."""
from datetime import datetime, timezone

import pytest

from loom.autonomy.circadian.journal import make_journal_append_tool, append_consolidation_report
from loom.core.harness.middleware import ToolCall


@pytest.mark.parametrize("writer", ["journal", "system_report"])
async def test_trusted_base_ancestor_alias_can_append(tmp_path, writer):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    leaf = "journal" if writer == "journal" else "dreams"
    target = alias / "autonomy/circadian" / leaf
    if writer == "journal":
        tool = make_journal_append_tool(journal_dir=target, timezone="UTC")
        result = await tool.executor(ToolCall(tool_name=tool.name, args={"kind": "moment", "body": "safe"},
                                             session_id="r2", trust_level=tool.trust_level))
        assert result.success, result.error
    else:
        append_consolidation_report("safe", dreams_dir=target, timezone="UTC")
    assert "safe" in (real / "autonomy/circadian" / leaf /
                      (datetime.now(timezone.utc).date().isoformat() + ".md")).read_text()


@pytest.mark.parametrize("part", ["autonomy", "circadian", "journal", "leaf"])
async def test_workspace_internal_links_remain_refused(tmp_path, part):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "autonomy/circadian/journal"
    outside = tmp_path / "outside"
    outside.mkdir()
    link = target / (datetime.now(timezone.utc).date().isoformat() + ".md") if part == "leaf" else (
        ws / "autonomy" if part == "autonomy" else ws / "autonomy/circadian" if part == "circadian" else target)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside / "private.md" if part == "leaf" else outside, target_is_directory=part != "leaf")
    tool = make_journal_append_tool(journal_dir=target, timezone="UTC")
    result = await tool.executor(ToolCall(tool_name=tool.name, args={"kind": "moment", "body": "secret body"},
                                         session_id="r2", trust_level=tool.trust_level))
    assert not result.success
    assert not list(outside.iterdir())
