"""
Tests for SemanticMemory.consolidate() + resolve_redirect() (#489, P2).

The merge primitive: fuse N near-duplicate facts into one refined survivor,
preserving every sacrificed fact's FULL original text (絲絲's hard
requirement — merge must not flatten the wording that carries the insight),
and leave a redirect stub at each sacrificed key so recall still resolves.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.memory.semantic import SemanticEntry, SemanticMemory


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


async def _seed_pair(semantic):
    a = SemanticEntry(key="user:tea", value="the user drinks tea every morning without fail",
                      source="manual", confidence=0.7)
    b = SemanticEntry(key="user:tea_dup", value="user has tea each morning",
                      source="session:x:fact:0", confidence=0.9)
    await semantic.upsert(a)
    await semantic.upsert(b)
    return await semantic.get("user:tea"), await semantic.get("user:tea_dup")


class TestConsolidate:
    async def test_survivor_value_becomes_refined(self, semantic):
        a, b = await _seed_pair(semantic)
        await semantic.consolidate(
            [a, b], refined_value="user drinks tea every morning",
            survivor_key="user:tea",
            sacrificed_content=b.value, merge_rationale="b is subsumed by a")
        survivor = await semantic.get("user:tea")
        assert survivor.value == "user drinks tea every morning"

    async def test_sacrificed_full_text_preserved_untruncated(self, semantic):
        a, b = await _seed_pair(semantic)
        await semantic.consolidate(
            [a, b], refined_value="refined", survivor_key="user:tea",
            sacrificed_content=b.value, merge_rationale="r")
        survivor = await semantic.get("user:tea")
        ci = survivor.metadata["consolidated_into"]
        # the sacrificed entry's FULL original value is stored verbatim
        sac = ci["sacrificed_entries"]
        assert any(s["key"] == "user:tea_dup" and s["value"] == b.value for s in sac)
        assert ci["rationale"] == "r"

    async def test_sacrificed_becomes_redirect_stub(self, semantic):
        a, b = await _seed_pair(semantic)
        await semantic.consolidate(
            [a, b], refined_value="refined", survivor_key="user:tea",
            sacrificed_content=b.value, merge_rationale="r")
        stub = await semantic.get("user:tea_dup")
        assert stub is not None                       # key preserved
        assert stub.metadata.get("redirected_to") == "user:tea"

    async def test_resolve_redirect_follows_to_survivor(self, semantic):
        a, b = await _seed_pair(semantic)
        await semantic.consolidate(
            [a, b], refined_value="refined", survivor_key="user:tea",
            sacrificed_content=b.value, merge_rationale="r")
        resolved = await semantic.resolve_redirect("user:tea_dup")
        assert resolved is not None
        assert resolved.key == "user:tea"
        assert resolved.value == "refined"

    async def test_resolve_redirect_passthrough_for_normal_key(self, semantic):
        await semantic.upsert(SemanticEntry(key="plain", value="v", source="manual"))
        resolved = await semantic.resolve_redirect("plain")
        assert resolved.key == "plain"

    async def test_confidence_takes_max_of_members(self, semantic):
        a, b = await _seed_pair(semantic)   # 0.7 and 0.9
        await semantic.consolidate(
            [a, b], refined_value="refined", survivor_key="user:tea",
            sacrificed_content=b.value, merge_rationale="r")
        survivor = await semantic.get("user:tea")
        assert survivor.confidence == pytest.approx(0.9)

    async def test_stub_has_no_embedding(self, db_conn):
        # Stub must not surface in semantic search → embedding nulled.
        from unittest.mock import AsyncMock
        provider = AsyncMock()
        provider.embed.return_value = [[1.0, 0.0, 0.0]]
        sem = SemanticMemory(db_conn, embedding_provider=provider)
        await sem.upsert(SemanticEntry(key="s1", value="alpha", source="manual"))
        await sem.upsert(SemanticEntry(key="s2", value="alpha too", source="manual"))
        a, b = await sem.get("s1"), await sem.get("s2")
        await sem.consolidate([a, b], refined_value="r", survivor_key="s1",
                              sacrificed_content=b.value, merge_rationale="x")
        pairs = {e.key: vec for e, vec in await sem.list_with_embeddings(10)}
        assert pairs["s2"] is None      # stub embedding nulled
        assert pairs["s1"] is not None  # survivor re-embedded

    # ── validation (fail-loud on programmer error) ──────────────────────
    async def test_survivor_not_in_entries_raises(self, semantic):
        a, b = await _seed_pair(semantic)
        with pytest.raises(ValueError):
            await semantic.consolidate([a, b], refined_value="r", survivor_key="nope",
                                       sacrificed_content="x", merge_rationale="r")

    async def test_fewer_than_two_entries_raises(self, semantic):
        a, _ = await _seed_pair(semantic)
        with pytest.raises(ValueError):
            await semantic.consolidate([a], refined_value="r", survivor_key="user:tea",
                                       sacrificed_content="x", merge_rationale="r")

    async def test_empty_required_fields_raise(self, semantic):
        a, b = await _seed_pair(semantic)
        with pytest.raises(ValueError):
            await semantic.consolidate([a, b], refined_value="", survivor_key="user:tea",
                                       sacrificed_content="x", merge_rationale="r")
        with pytest.raises(ValueError):
            await semantic.consolidate([a, b], refined_value="r", survivor_key="user:tea",
                                       sacrificed_content="x", merge_rationale="")
