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

from unittest.mock import MagicMock

from loom.core.memory.store import SQLiteStore
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.search import MemorySearch
from loom.core.memory.facade import MemoryFacade


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

    async def test_atomic_rollback_on_stub_failure(self, semantic, db_conn):
        # Codex review #494 — consolidate must be all-or-nothing. If a stub
        # update fails, the survivor must NOT be left half-written.
        a, b = await _seed_pair(semantic)
        await db_conn.execute(
            "CREATE TRIGGER block_stub BEFORE UPDATE ON semantic_entries "
            "WHEN NEW.key = 'user:tea_dup' "
            "BEGIN SELECT RAISE(ABORT, 'blocked'); END;"
        )
        await db_conn.commit()
        try:
            with pytest.raises(Exception):
                await semantic.consolidate(
                    [a, b], refined_value="REFINED SURVIVOR", survivor_key="user:tea",
                    sacrificed_content=b.value, merge_rationale="r")
            # survivor rolled back to its original value — no half-write
            assert (await semantic.get("user:tea")).value == a.value
            assert (await semantic.get("user:tea_dup")).value == b.value
        finally:
            await db_conn.execute("DROP TRIGGER block_stub")
            await db_conn.commit()


class TestRedirectChain:
    """Multi-hop redirect resolution + loop guard + chain metadata (#490 P3)."""

    async def _chain_abc(self, semantic):
        for k, v in [("a", "fact a"), ("b", "fact b"), ("c", "fact c")]:
            await semantic.upsert(SemanticEntry(key=k, value=v, source="manual"))
        a, b = await semantic.get("a"), await semantic.get("b")
        await semantic.consolidate([a, b], refined_value="B refined", survivor_key="b",
                                   sacrificed_content=a.value, merge_rationale="r")  # a→b
        b, c = await semantic.get("b"), await semantic.get("c")
        await semantic.consolidate([b, c], refined_value="C refined", survivor_key="c",
                                   sacrificed_content=b.value, merge_rationale="r")  # b→c

    async def test_multi_hop_follows_to_terminal(self, semantic):
        await self._chain_abc(semantic)
        resolved = await semantic.resolve_redirect("a")   # a→b→c
        assert resolved is not None
        assert resolved.key == "c"
        assert resolved.value == "C refined"

    async def test_redirect_chain_metadata_recorded(self, semantic):
        await self._chain_abc(semantic)
        stub_a = await semantic.get("a")
        assert "redirect_chain" in stub_a.metadata

    async def test_cycle_returns_none(self, semantic, db_conn):
        # Craft a cycle directly: a→b, b→a.
        await semantic.upsert(SemanticEntry(key="a", value="x", source="manual"))
        await semantic.upsert(SemanticEntry(key="b", value="y", source="manual"))
        import json
        for k, tgt in [("a", "b"), ("b", "a")]:
            await db_conn.execute(
                "UPDATE semantic_entries SET metadata=? WHERE key=?",
                (json.dumps({"redirected_to": tgt}), k))
        await db_conn.commit()
        assert await semantic.resolve_redirect("a") is None   # loop guarded

    async def test_dangling_redirect_returns_none(self, semantic):
        await semantic.upsert(SemanticEntry(key="a", value="x", source="manual"))
        await semantic.upsert(SemanticEntry(key="b", value="y", source="manual"))
        a, b = await semantic.get("a"), await semantic.get("b")
        await semantic.consolidate([a, b], refined_value="r", survivor_key="b",
                                   sacrificed_content=a.value, merge_rationale="r")  # a→b
        await semantic.delete("b")                              # survivor gone
        assert await semantic.resolve_redirect("a") is None    # dangling

    async def test_merge_into_stub_rejected(self, semantic):
        await semantic.upsert(SemanticEntry(key="a", value="x", source="manual"))
        await semantic.upsert(SemanticEntry(key="b", value="y", source="manual"))
        await semantic.upsert(SemanticEntry(key="c", value="z", source="manual"))
        a, b = await semantic.get("a"), await semantic.get("b")
        await semantic.consolidate([a, b], refined_value="r", survivor_key="b",
                                   sacrificed_content=a.value, merge_rationale="r")  # a→b (a is stub)
        a_stub, c = await semantic.get("a"), await semantic.get("c")
        with pytest.raises(ValueError):
            # survivor 'a' is now a stub — merging into it is malformed
            await semantic.consolidate([c, a_stub], refined_value="r", survivor_key="a",
                                       sacrificed_content=c.value, merge_rationale="r")


class TestRedirectRecall:
    """Codex review #494 — recall must resolve/suppress redirect stubs."""

    async def _consolidate_pair(self, db_conn):
        sem = SemanticMemory(db_conn)
        proc = ProceduralMemory(db_conn)
        await sem.upsert(SemanticEntry(key="user:tea", value="user drinks tea every single morning", source="manual"))
        await sem.upsert(SemanticEntry(key="user:tea_dup", value="user has tea each morning daily", source="manual"))
        a, b = await sem.get("user:tea"), await sem.get("user:tea_dup")
        await sem.consolidate([a, b], refined_value="user drinks tea every morning",
                              survivor_key="user:tea", sacrificed_content=b.value,
                              merge_rationale="r")
        return sem, proc

    async def test_stub_suppressed_from_recall(self, db_conn):
        sem, proc = await self._consolidate_pair(db_conn)
        search = MemorySearch(sem, proc)
        # "consolidated" appears ONLY in the stub's placeholder value — if the
        # stub is not suppressed it is the one row that matches.
        results = await search.recall("consolidated", type="semantic", limit=10)
        assert "user:tea_dup" not in [r.key for r in results]

    async def test_survivor_still_findable_after_merge(self, db_conn):
        sem, proc = await self._consolidate_pair(db_conn)
        search = MemorySearch(sem, proc)
        results = await search.recall("tea morning", type="semantic", limit=10)
        assert "user:tea" in [r.key for r in results]
        assert "user:tea_dup" not in [r.key for r in results]

    async def test_get_fact_follows_redirect(self, db_conn):
        sem, proc = await self._consolidate_pair(db_conn)
        facade = MemoryFacade(semantic=sem, procedural=proc, episodic=MagicMock(),
                              search=MemorySearch(sem, proc), governor=None)
        resolved = await facade.get_fact("user:tea_dup")
        assert resolved is not None
        assert resolved.key == "user:tea"
        assert resolved.value == "user drinks tea every morning"
