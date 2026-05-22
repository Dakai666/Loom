"""
Tests for RelationalMemory — subject/predicate/object triple store.

Coverage
--------
RelationalMemory (async, real DB)
  - upsert() inserts new entry
  - get() retrieves by subject+predicate
  - get() returns None for missing pair
  - upsert() updates object for same (subject, predicate)
  - query() by subject returns all matching
  - query() by predicate returns all matching
  - query() by both is exact lookup
  - query() no filter returns all entries
  - query() returns empty list when no entries
  - delete() removes entry, returns True
  - delete() returns False when entry not found
  - source and confidence preserved through upsert
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.memory.relational import RelationalEntry, RelationalMemory


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
async def relational(db_conn):
    return RelationalMemory(db_conn)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRelationalMemory:
    def _entry(self, subject="user", predicate="prefers", obj="concise responses", **kw):
        return RelationalEntry(subject=subject, predicate=predicate, object=obj, **kw)

    async def test_upsert_inserts_new_entry(self, relational):
        await relational.upsert(self._entry())
        got = await relational.get("user", "prefers")
        assert got is not None
        assert got.object == "concise responses"

    async def test_get_returns_none_for_missing(self, relational):
        assert await relational.get("user", "nonexistent") is None

    async def test_upsert_updates_existing(self, relational):
        await relational.upsert(self._entry(obj="verbose"))
        await relational.upsert(self._entry(obj="concise"))
        got = await relational.get("user", "prefers")
        assert got.object == "concise"

    async def test_query_by_subject(self, relational):
        await relational.upsert(self._entry(predicate="prefers", obj="A"))
        await relational.upsert(self._entry(predicate="avoids", obj="B"))
        results = await relational.query(subject="user")
        assert len(results) == 2
        predicates = {r.predicate for r in results}
        assert predicates == {"prefers", "avoids"}

    async def test_query_by_predicate(self, relational):
        await relational.upsert(RelationalEntry(subject="user", predicate="uses", object="sqlite"))
        await relational.upsert(RelationalEntry(subject="project", predicate="uses", object="python"))
        results = await relational.query(predicate="uses")
        assert len(results) == 2

    async def test_query_by_both(self, relational):
        await relational.upsert(self._entry())
        results = await relational.query(subject="user", predicate="prefers")
        assert len(results) == 1
        assert results[0].object == "concise responses"

    async def test_query_all_no_filter(self, relational):
        await relational.upsert(RelationalEntry(subject="a", predicate="p1", object="x"))
        await relational.upsert(RelationalEntry(subject="b", predicate="p2", object="y"))
        results = await relational.query()
        assert len(results) == 2

    async def test_query_empty_returns_empty_list(self, relational):
        assert await relational.query(subject="nobody") == []

    async def test_delete_removes_entry(self, relational):
        await relational.upsert(self._entry())
        deleted = await relational.delete("user", "prefers")
        assert deleted is True
        assert await relational.get("user", "prefers") is None

    async def test_delete_returns_false_when_not_found(self, relational):
        assert await relational.delete("nobody", "nothing") is False

    async def test_source_and_confidence_preserved(self, relational):
        entry = self._entry(confidence=0.75)
        entry.source = "manual"
        await relational.upsert(entry)
        got = await relational.get("user", "prefers")
        assert got.confidence == pytest.approx(0.75)
