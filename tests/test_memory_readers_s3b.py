"""#650 S3 PR-B contracts; synthetic sources only, no legacy backfill."""
import asyncio
import inspect
import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from click.testing import CliRunner

from loom.core.cognition.reflection import ReflectionAPI
from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.core.memory.episodic import EpisodicEntry, EpisodicMemory
from loom.core.memory.facade import MemoryFacade
from loom.core.memory.index import MemoryIndexer
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.search import MemorySearch
from loom.core.memory.semantic import SemanticMemory
from loom.core.memory.session_log import SessionLog
from loom.core.memory.store import SQLiteStore
from loom.platform.cli.main import _gather_memory_health, cli
from loom.platform.cli.tools import make_recall_period_tool


@pytest_asyncio.fixture
async def memory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    async with store.connect() as db:
        sem, proc = SemanticMemory(db), ProceduralMemory(db)
        yield MemoryFacade(semantic=sem, procedural=proc, episodic=EpisodicMemory(db),
                           session_log=SessionLog(db), search=MemorySearch(sem, proc))


async def event(memory, sid="s", name="fetch", success="absent", content="source", role="tool"):
    metadata = {"tool_name": name, "tool_call_id": content}
    if success != "absent":
        metadata["success"] = success
    if success is False:
        metadata["failure_type"] = "execution_error"
    await memory.session_log.log_message(sid, 1, role, content, metadata)


async def legacy(memory, sid="s", success=True):
    await memory.episodic.write(EpisodicEntry(
        session_id=sid, event_type="tool_result", content="legacy-only",
        metadata={"tool_name": "fetch", "success": success}))


async def test_reflection_uses_log_and_reports_unknown_without_double_count(memory):
    await legacy(memory, success=True)
    await event(memory, role="user")
    for outcome in (True, False, "absent", "false"):
        await event(memory, success=outcome)
    api = ReflectionAPI(memory)
    summary = await api.session_summary("s")
    assert "session_log" in summary and "4 tool call" in summary
    assert "1 ok, 1 failed, 2 unknown" in summary and "1 user message" in summary
    assert await api.failure_summary("s") == {
        "source": "session_log", "counts": {"execution_error": 1}, "unknown_outcomes": 2}
    assert await api.tool_success_rate("s") == {
        "source": "session_log", "rates": {"fetch": 0.5}, "unknown_outcomes": {"fetch": 2}}
    recent = await api.recent_tool_calls("s")
    assert len(recent) == 4 and all(row["source"] == "session_log" for row in recent)
    assert [r["metadata"]["success"] for r in recent] == [None, None, False, True]


async def test_recent_tools_filter_scope_before_limit_and_keep_last_rows(memory):
    for i in range(125):
        await event(memory, content=f"step-{i}", success=True)
    for i in range(125):
        await event(memory, content="later-user", role="user")
        await event(memory, sid="sibling", content="other", success=False)
    recent = await ReflectionAPI(memory).recent_tool_calls("s", n=2)
    assert [r["content"] for r in recent] == ["step-124", "step-123"]
    assert await ReflectionAPI(memory).recent_tool_calls("s", n=0) == []


async def test_legacy_is_explicit_labeled_and_never_combined_with_known_log(memory):
    await legacy(memory, sid="old", success=False)
    await event(memory, sid="old")  # pre-outcome-metadata log
    api = ReflectionAPI(memory)
    assert "unknown" in await api.session_summary("old")
    assert (await api.tool_success_rate("old"))["rates"] == {"fetch": None}
    assert "legacy episodic fallback" in await api.session_summary("old", legacy_episodic=True)
    assert (await api.failure_summary("old", legacy_episodic=True))["counts"] == {"unclassified": 1}
    # An episode lacking success is unknown even in the explicit fallback.
    await memory.episodic.write(EpisodicEntry(session_id="child-only", event_type="tool_result", content="old"))
    assert "No activity" in await api.session_summary("child-only")
    assert "1 unknown" in await api.session_summary("child-only", legacy_episodic=True)
    await event(memory, sid="old", success=True, content="new-known")
    rates = await api.tool_success_rate("old", legacy_episodic=True)
    assert rates["source"] == "session_log" and rates["rates"] == {"fetch": 1.0}
    assert rates["unknown_outcomes"] == {"fetch": 1}


async def test_reflection_masks_legacy_and_log_content_and_metadata(memory):
    secret = "synthetic-secret-123456"
    await event(memory, content=f"password={secret}", success=True)
    await memory.episodic.write(EpisodicEntry(session_id="old", event_type="tool_result",
        content=f"password={secret}", metadata={"success": True, "detail": f"password={secret}"}))
    api = ReflectionAPI(memory)
    assert secret not in json.dumps(await api.recent_tool_calls("s"))
    assert secret not in json.dumps(await api.recent_tool_calls("old", legacy_episodic=True))


async def test_period_adapter_returns_log_once_and_requires_explicit_legacy(memory):
    await legacy(memory)
    await event(memory, success=True, content="modern")
    since = datetime.now(UTC) - timedelta(days=1)
    groups = await memory.recall_period(since=since, session_id="s", include_sessions=True)
    assert not groups["episodic"]
    assert [r["content"] for r in groups["messages"]] == ["modern"]
    disabled = await memory.recall_period(since=since, include_episodic=False)
    assert not disabled["messages"] and not disabled["episodic"]
    await legacy(memory, sid="old")
    groups = await memory.recall_period(since=since, session_id="old")
    assert not groups["episodic"] and not groups["messages"]
    groups = await memory.recall_period(since=since, session_id="old", legacy_episodic=True)
    assert len(groups["episodic"]) == 1 and not groups["messages"]
    tool = make_recall_period_tool(memory)
    assert "include_episodic" in tool.input_schema["properties"]
    result = await tool.executor(ToolCall(id="r", tool_name="recall_period", session_id="s",
        trust_level=TrustLevel.SAFE, args={"since": since.isoformat(), "session_id": "old", "legacy_episodic": True}))
    assert "legacy episodic fallback" in result.output.lower()


async def test_period_time_and_session_scope_apply_before_limit(memory):
    await event(memory, sid="other", success=True)
    await event(memory, success=True, content="inside")
    await event(memory, success=True, content="boundary")
    db = memory.semantic._db
    base = datetime(2026, 1, 1, tzinfo=UTC)
    await db.execute("UPDATE session_log SET created_at=? WHERE content='inside'", (base.isoformat(),))
    await db.execute("UPDATE session_log SET created_at=? WHERE content='boundary'", ((base+timedelta(days=1)).isoformat(),))
    await db.commit()
    groups = await memory.recall_period(since=base, until=base+timedelta(days=1), session_id="s", limit=1)
    assert [r["content"] for r in groups["messages"]] == ["inside"]


async def test_health_uses_log_coverage_not_episodic_backlog(memory):
    for i in range(12):
        await legacy(memory, sid=f"old-{i}")
    await event(memory, success=True)
    await event(memory)  # unknown
    db = memory.semantic._db
    before = db.total_changes
    health = await _gather_memory_health(db)
    assert db.total_changes == before
    assert "episodic_uncompressed_sessions" not in health
    assert health["session_log_rows"] == 2
    assert health["session_log_unknown_outcomes"] == 1
    assert health["session_log_tool_rows"] == 2
    assert health["session_log_sessions"] == 1


async def test_cli_reflect_wires_log_and_prints_unknown(memory, tmp_path):
    await event(memory)
    db = str(tmp_path / "memory.db")
    result = await asyncio.to_thread(CliRunner().invoke, cli, ["reflect", "--db", db, "--session", "s"])
    assert result.exit_code == 0, result.output
    assert "session_log" in result.output and "unknown" in result.output.lower()
    assert "0%" not in result.output


def test_indexer_drops_only_unused_episodic_parameter():
    assert "episodic" not in inspect.signature(MemoryIndexer).parameters


async def test_period_tool_labels_unknown_and_masks_explicit_legacy(memory):
    secret = "synthetic-secret-123456"
    await event(memory, content="old log preview")
    await memory.episodic.write(EpisodicEntry(session_id="old", event_type="tool_result",
        content=f"password={secret}", metadata={}))
    tool = make_recall_period_tool(memory)
    since = (datetime.now(UTC)-timedelta(days=1)).isoformat()
    async def invoke(sid, use_legacy=False):
        return await tool.executor(ToolCall(id="r", tool_name="recall_period", session_id="s",
            trust_level=TrustLevel.SAFE, args={"since": since, "session_id": sid, "legacy_episodic": use_legacy}))
    result = await invoke("s")
    assert "unknown outcome" in result.output.lower()
    assert "log:s:1" in result.output
    result = await invoke("old", True)
    assert "legacy episodic fallback" in result.output.lower()
    assert "unknown outcome" in result.output.lower()
    assert secret not in result.output


async def test_legacy_selection_checks_known_log_before_recent_limit(memory):
    await event(memory, success=True, content="old-known")
    await event(memory, content="new-unknown")
    await legacy(memory, success=False)
    recent = await ReflectionAPI(memory).recent_tool_calls("s", n=1, legacy_episodic=True)
    assert len(recent) == 1 and recent[0]["content"] == "new-unknown"
    assert recent[0]["source"] == "session_log" and recent[0]["metadata"]["success"] is None


async def test_period_source_unavailable_is_not_silent_empty_history(memory):
    memory.session_log = None
    result = await make_recall_period_tool(memory).executor(ToolCall(
        id="r", tool_name="recall_period", session_id="s", trust_level=TrustLevel.SAFE,
        args={"since": "2020-01-01"}))
    assert "session_log unavailable" in result.output


async def test_legacy_reads_do_not_backfill_or_mutate_either_source(memory):
    await legacy(memory, sid="old")
    db = memory.semantic._db
    before = db.total_changes
    api = ReflectionAPI(memory)
    await api.session_summary("old", legacy_episodic=True)
    await api.tool_success_rate("old", legacy_episodic=True)
    await memory.recall_period(since=datetime(2000, 1, 1, tzinfo=UTC), session_id="old", legacy_episodic=True)
    assert db.total_changes == before
    assert await memory.session_log.read_trace("old") == []
