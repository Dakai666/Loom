"""
Tests for the convergent-dream ToolDefinition adapter (#488, P1).

Mirrors make_dream_cycle_tool: wraps the read-only cognition cycle, writes the
夢境鞏固 report to the journal, and returns a summary. Must stay read-only on
the DB and SAFE on trust.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.middleware import ToolCall


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def db_conn(store):
    async with store.connect() as conn:
        yield conn


@pytest_asyncio.fixture
async def semantic(db_conn):
    return SemanticMemory(db_conn)


def _make_call(args: dict) -> ToolCall:
    return ToolCall(id="t1", tool_name="convergent_dream", args=args,
                    trust_level=TrustLevel.SAFE, session_id="s1")


async def _combined_llm(messages):
    sys = messages[0]["content"]
    if "difference inventory" in sys:
        return '{"unique_by_key":{},"mergeable":false,"rationale":"distinct"}'
    return "[]"


async def _snapshot(db_conn):
    cursor = await db_conn.execute(
        "SELECT key, value, source, updated_at FROM semantic_entries ORDER BY key")
    return list(await cursor.fetchall())


class TestConvergentDreamTool:
    def test_tool_is_safe(self):
        from loom.core.memory.maintenance import make_convergent_dream_tool
        tool = make_convergent_dream_tool(object(), _combined_llm)
        assert tool.trust_level == TrustLevel.SAFE
        assert tool.name == "convergent_dream"

    async def test_runs_and_writes_report(self, semantic, tmp_path):
        from loom.core.memory.maintenance import make_convergent_dream_tool
        await semantic.upsert(SemanticEntry(
            key="user:pref:tone:a", value="answer with formal polished prose", source="manual"))
        await semantic.upsert(SemanticEntry(
            key="user:pref:tone:b", value="reply short blunt fragments only", source="manual"))

        tool = make_convergent_dream_tool(
            semantic, _combined_llm, timezone="Asia/Taipei", journal_dir=tmp_path)
        result = await tool.executor(_make_call({}))

        assert result.success is True
        assert "scanned" in result.output.lower() or "掃描" in result.output
        # report file written to the journal dir
        md_files = list(tmp_path.glob("*.md"))
        assert len(md_files) == 1
        assert "夢境鞏固" in md_files[0].read_text(encoding="utf-8")

    async def test_is_read_only(self, semantic, db_conn, tmp_path):
        from loom.core.memory.maintenance import make_convergent_dream_tool
        await semantic.upsert(SemanticEntry(
            key="a:b:c:x", value="zebra ocean mountain", source="manual"))
        await semantic.upsert(SemanticEntry(
            key="a:b:c:y", value="violin guitar trumpet", source="manual"))

        before = await _snapshot(db_conn)
        tool = make_convergent_dream_tool(
            semantic, _combined_llm, journal_dir=tmp_path)
        await tool.executor(_make_call({}))
        after = await _snapshot(db_conn)
        assert before == after
