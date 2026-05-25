"""
Tests for relational triple storage — bridge helpers on the semantic store.

Since #451 phase B the ``RelationalMemory`` class is retired; triples
live in ``semantic_entries`` keyed ``rel:{subject}::{predicate}`` and the
read/write API is the ``relational_bridge`` helpers
(:func:`upsert_triple` / :func:`get_triple` / :func:`query_triples` /
:func:`delete_triple`). This file validates the same upsert/get/query/
delete behaviours the old class used to provide.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from loom.core.memory.relational_bridge import (
    RelationalEntry,
    delete_triple,
    get_triple,
    query_triples,
    upsert_triple,
)
from loom.core.memory.semantic import SemanticMemory
from loom.core.memory.store import SQLiteStore


# ---------------------------------------------------------------------------
# DB Fixtures
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRelationalTriples:
    def _entry(self, subject="user", predicate="prefers", obj="concise responses", **kw):
        return RelationalEntry(subject=subject, predicate=predicate, object=obj, **kw)

    async def test_upsert_inserts_new_entry(self, semantic):
        await upsert_triple(semantic, self._entry())
        got = await get_triple(semantic, "user", "prefers")
        assert got is not None
        assert got.object == "concise responses"

    async def test_get_returns_none_for_missing(self, semantic):
        assert await get_triple(semantic, "user", "nonexistent") is None

    async def test_upsert_updates_existing(self, semantic):
        await upsert_triple(semantic, self._entry(obj="verbose"))
        await upsert_triple(semantic, self._entry(obj="concise"))
        got = await get_triple(semantic, "user", "prefers")
        assert got.object == "concise"

    async def test_query_by_subject(self, semantic):
        await upsert_triple(semantic, self._entry(predicate="prefers", obj="A"))
        await upsert_triple(semantic, self._entry(predicate="avoids", obj="B"))
        results = await query_triples(semantic, subject="user")
        assert len(results) == 2
        predicates = {r.predicate for r in results}
        assert predicates == {"prefers", "avoids"}

    async def test_query_by_predicate(self, semantic):
        await upsert_triple(semantic, RelationalEntry(subject="user", predicate="uses", object="sqlite"))
        await upsert_triple(semantic, RelationalEntry(subject="project", predicate="uses", object="python"))
        results = await query_triples(semantic, predicate="uses")
        assert len(results) == 2

    async def test_query_by_both(self, semantic):
        await upsert_triple(semantic, self._entry())
        results = await query_triples(semantic, subject="user", predicate="prefers")
        assert len(results) == 1
        assert results[0].object == "concise responses"

    async def test_query_all_no_filter(self, semantic):
        await upsert_triple(semantic, RelationalEntry(subject="a", predicate="p1", object="x"))
        await upsert_triple(semantic, RelationalEntry(subject="b", predicate="p2", object="y"))
        results = await query_triples(semantic)
        assert len(results) == 2

    async def test_query_empty_returns_empty_list(self, semantic):
        assert await query_triples(semantic, subject="nobody") == []

    async def test_delete_removes_entry(self, semantic):
        await upsert_triple(semantic, self._entry())
        deleted = await delete_triple(semantic, "user", "prefers")
        assert deleted is True
        assert await get_triple(semantic, "user", "prefers") is None

    async def test_delete_returns_false_when_not_found(self, semantic):
        assert await delete_triple(semantic, "nobody", "nothing") is False

    async def test_source_and_confidence_preserved(self, semantic):
        entry = self._entry(confidence=0.75)
        entry.source = "manual"
        await upsert_triple(semantic, entry)
        got = await get_triple(semantic, "user", "prefers")
        assert got.confidence == pytest.approx(0.75)
        assert got.source == "manual"

    async def test_subject_with_colons_round_trips(self, semantic):
        """Subjects already containing colons (``openai:musk-trial:2026-05-15``,
        ``minimax:tts:speech-2.6-hd``, etc.) must survive the ``rel:{S}::{P}``
        key encoding."""
        await upsert_triple(semantic, RelationalEntry(
            subject="minimax:tts:speech-2.6-hd",
            predicate="fails_with",
            object="token plan not supported (error 2061)",
        ))
        got = await get_triple(
            semantic, "minimax:tts:speech-2.6-hd", "fails_with",
        )
        assert got is not None
        assert got.subject == "minimax:tts:speech-2.6-hd"
        assert got.predicate == "fails_with"
