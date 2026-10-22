"""#634: period recall scopes all layers and accounts for actual reads."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.core.memory.episodic import EpisodicEntry, EpisodicMemory
from loom.core.memory.facade import MemoryFacade
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.search import MemorySearch
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.session_log import SessionLog
from loom.core.memory.store import SQLiteStore
from loom.platform.cli.tools import make_recall_period_tool


SINCE = datetime(2026, 10, 21, tzinfo=UTC)
UNTIL = SINCE + timedelta(days=1)
OLD_ACCESS = SINCE - timedelta(days=30)
SESSION_IDS = ("s1", "s10", "s'_%")


@pytest_asyncio.fixture
async def period_memory(tmp_path):
    store = SQLiteStore(str(tmp_path / "period.db"))
    await store.initialize()
    async with store.connect() as db:
        semantic = SemanticMemory(db)
        procedural = ProceduralMemory(db)
        episodic = EpisodicMemory(db)
        sessions = SessionLog(db)
        memory = MemoryFacade(
            semantic=semantic, procedural=procedural, episodic=episodic,
            search=MemorySearch(semantic, procedural), session_log=sessions,
        )
        for index, sid in enumerate(SESSION_IDS, start=1):
            at = SINCE + timedelta(hours=index)
            await sessions.create_session(sid, "model", title=sid)
            await sessions.update_session(sid, 1, at.isoformat(), None)
            await episodic.write(EpisodicEntry(
                session_id=sid, event_type="message", content=f"event {sid}",
                created_at=at,
            ))
            await sessions.log_message(sid, 0, "user", f"message {sid}")
            await db.execute(
                "UPDATE session_log SET created_at = ? WHERE session_id = ?",
                (at.isoformat(), sid),
            )
            for suffix, fact_time in (("start", SINCE), ("later", at)):
                await semantic.upsert(SemanticEntry(
                    key=f"{sid}:{suffix}", value=f"issues discussed in {sid}",
                    source=f"session:{sid}", updated_at=fact_time,
                    last_accessed_at=OLD_ACCESS,
                ))
                await db.execute(
                    "UPDATE semantic_entries SET updated_at = ? WHERE key = ?",
                    (fact_time.isoformat(), f"{sid}:{suffix}"),
                )
        for key, at, source in (
            ("unknown", UNTIL - timedelta(hours=1), "memorize"),
            ("before", SINCE - timedelta(seconds=1), "session:s1"),
            ("until", UNTIL, "session:s1"),
        ):
            await semantic.upsert(SemanticEntry(
                key=key, value=key, source=source, updated_at=at,
                last_accessed_at=None if key == "unknown" else OLD_ACCESS,
            ))
            await db.execute(
                "UPDATE semantic_entries SET updated_at = ? WHERE key = ?",
                (at.isoformat(), key),
            )
        await db.commit()
        yield memory


@pytest.mark.parametrize("session_id", [None, *SESSION_IDS, "missing"])
@pytest.mark.parametrize("limit", [1, 20])
async def test_period_scopes_all_layers_before_limit(period_memory, session_id, limit):
    """Newer unrelated rows must not crowd out a requested session."""
    result = await period_memory.recall_period(
        since=SINCE, until=UNTIL, session_id=session_id, limit=limit,
        include_sessions=True,
    )
    expected_sessions = [sid for sid in reversed(SESSION_IDS)
                         if session_id is None or sid == session_id][:limit]
    expected_events = [sid for sid in SESSION_IDS
                       if session_id is None or sid == session_id][:limit]
    assert [row["session_id"] for row in result["sessions"]] == expected_sessions
    assert [row["session_id"] for row in result["messages"]] == expected_events
    assert [row.session_id for row in result["episodic"]] == expected_events

    facts = result["semantic"]
    if session_id is None:
        assert facts[0].key == "unknown"
        assert len(facts) == min(limit, 7)
    elif session_id in SESSION_IDS:
        assert [row.key for row in facts] == [
            f"{session_id}:later", f"{session_id}:start",
        ][:limit]
        assert all(row.source == f"session:{session_id}" for row in facts)
    else:
        assert facts == []
    assert all(SINCE <= row.updated_at < UNTIL for row in facts)


@pytest.mark.parametrize("session_id", [None, "s1"])
async def test_period_marks_only_returned_facts_without_changing_update_time(
    period_memory, session_id,
):
    db = period_memory.semantic._db
    before = await (await db.execute(
        "SELECT key, updated_at, last_accessed_at FROM semantic_entries",
    )).fetchall()
    started = datetime.now(UTC)
    result = await period_memory.recall_period(
        since=SINCE, until=UNTIL, session_id=session_id, limit=1,
        include_episodic=False,
    )
    returned = {row.key for row in result["semantic"]}
    assert len(returned) == 1
    for key, updated, accessed in before:
        stored = await period_memory.semantic.get(key)
        assert stored.updated_at.isoformat() == updated
        if key in returned:
            assert stored.last_accessed_at >= started
        else:
            assert stored.last_accessed_at == (
                datetime.fromisoformat(accessed) if accessed else None
            )


async def test_empty_period_does_not_mark_other_facts(period_memory, monkeypatch):
    mark = AsyncMock(wraps=period_memory.semantic.mark_accessed)
    monkeypatch.setattr(period_memory.semantic, "mark_accessed", mark)
    result = await period_memory.recall_period(
        since=UNTIL + timedelta(days=1), include_episodic=False,
    )
    assert result["semantic"] == []
    mark.assert_not_awaited()


async def test_maintenance_period_listing_does_not_mark_accessed(period_memory):
    facts = await period_memory.semantic.list_between(SINCE, UNTIL)
    assert facts
    for fact in facts:
        stored = await period_memory.semantic.get(fact.key)
        assert stored.last_accessed_at == (None if fact.key == "unknown" else OLD_ACCESS)


async def test_period_tool_retrieves_session_issues_and_marks_them(period_memory):
    """Exercise the real tool/facade/SQLite path, without an LLM or live DB."""
    tool = make_recall_period_tool(period_memory)
    result = await tool.executor(ToolCall(
        id="period-issues", tool_name="recall_period",
        trust_level=TrustLevel.SAFE, session_id="current", args={
            "since": SINCE.isoformat(), "until": UNTIL.isoformat(),
            "session_id": "s1", "include_sessions": True,
        },
    ))
    assert result.success
    assert "issues discussed in s1" in result.output
    assert "issues discussed in s10" not in result.output
    assert "message s10" not in result.output
    assert "message s1" in result.output
    assert "unknown" not in result.output
    assert (await period_memory.semantic.get("s1:later")).last_accessed_at > OLD_ACCESS
