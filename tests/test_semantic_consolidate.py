"""
Tests for SemanticMemory.consolidate() + resolve_redirect() (#489, P2).

The merge primitive: fuse N near-duplicate facts into one refined survivor,
preserving every sacrificed fact's FULL original text (Loom Agent's hard
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


class TestProvenanceAcrossPasses:
    """A survivor merged again, or a past survivor sacrificed, must not lose
    the originals an earlier pass preserved (live DB lost 10 this way)."""

    async def _merge(self, semantic, keys, survivor, refined, **kw):
        entries = [await semantic.get(k) for k in keys]
        return await semantic.consolidate(
            entries, refined_value=refined, survivor_key=survivor,
            sacrificed_content="x", merge_rationale=f"merge into {survivor}", **kw)

    async def test_survivor_original_value_is_kept(self, semantic):
        a, _ = await _seed_pair(semantic)
        await self._merge(semantic, ["user:tea", "user:tea_dup"], "user:tea", "refined")
        ci = (await semantic.get("user:tea")).metadata["consolidated_into"]
        assert ci["survivor_original"]["value"] == a.value
        assert ci["survivor_original"]["source"] == a.source

    async def test_merging_a_survivor_again_keeps_the_earlier_record(self, semantic):
        await _seed_pair(semantic)
        await self._merge(semantic, ["user:tea", "user:tea_dup"], "user:tea", "pass one")
        first = (await semantic.get("user:tea")).metadata["consolidated_into"]
        await semantic.upsert(SemanticEntry(key="user:tea3", value="tea at dawn", source="session:y:fact:1"))
        await self._merge(semantic, ["user:tea", "user:tea3"], "user:tea", "pass two")
        meta = (await semantic.get("user:tea")).metadata
        assert meta["consolidation_history"] == [first]
        assert meta["consolidated_into"]["sacrificed_entries"][0]["key"] == "user:tea3"

    async def test_history_accumulates_over_three_passes(self, semantic):
        await _seed_pair(semantic)
        await self._merge(semantic, ["user:tea", "user:tea_dup"], "user:tea", "one")
        for i in (3, 4):
            await semantic.upsert(SemanticEntry(key=f"user:tea{i}", value=f"tea {i}", source="session:y:fact:1"))
            await self._merge(semantic, ["user:tea", f"user:tea{i}"], "user:tea", f"pass {i}")
        hist = (await semantic.get("user:tea")).metadata["consolidation_history"]
        assert [h["rationale"] for h in hist] == ["merge into user:tea"] * 2
        assert [h["sacrificed_entries"][0]["key"] for h in hist] == ["user:tea_dup", "user:tea3"]

    async def test_sacrificing_a_past_survivor_carries_its_record(self, semantic):
        await _seed_pair(semantic)
        await self._merge(semantic, ["user:tea", "user:tea_dup"], "user:tea", "one")
        earlier = (await semantic.get("user:tea")).metadata["consolidated_into"]
        await semantic.upsert(SemanticEntry(key="user:drinks", value="tea and coffee",
                                            source="manual", confidence=1.0))
        await self._merge(semantic, ["user:drinks", "user:tea"], "user:drinks", "drinks")
        sac = (await semantic.get("user:drinks")).metadata["consolidated_into"]["sacrificed_entries"]
        rec = next(s for s in sac if s["key"] == "user:tea")
        assert rec["consolidated_into"] == earlier
        assert rec["value"] == "one"

    async def test_extra_metadata_lands_on_survivor(self, semantic):
        await _seed_pair(semantic)
        await self._merge(semantic, ["user:tea", "user:tea_dup"], "user:tea", "r",
                          extra_metadata={"produced_by": "distill_dream"})
        assert (await semantic.get("user:tea")).metadata["produced_by"] == "distill_dream"

    @pytest.mark.parametrize("key", ["consolidated_into", "consolidation_history", "redirected_to"])
    async def test_extra_metadata_cannot_overwrite_provenance(self, semantic, key):
        await _seed_pair(semantic)
        with pytest.raises(ValueError):
            await self._merge(semantic, ["user:tea", "user:tea_dup"], "user:tea", "r",
                              extra_metadata={key: "x"})
        assert (await semantic.get("user:tea_dup")).metadata.get("redirected_to") is None


class TestProvenanceSurvivesOverwrite:
    """A later write to a survivor's key (same-key triple, SUPERSEDE) replaces
    its value but must not drop the originals it holds."""

    async def _survivor(self, semantic, key="user:tea"):
        await _seed_pair(semantic)
        entries = [await semantic.get("user:tea"), await semantic.get("user:tea_dup")]
        await semantic.consolidate(
            entries, refined_value="refined", survivor_key=key,
            sacrificed_content="x", merge_rationale="r",
            extra_metadata={"produced_by": "distill_dream", "distilled_at": "2026-10-19"})
        return (await semantic.get(key)).metadata

    async def test_overwrite_retires_the_record_to_history(self, semantic):
        before = await self._survivor(semantic)
        await semantic.upsert(SemanticEntry(key="user:tea", value="user now drinks coffee",
                                            source="session:z:fact:0"))
        after = await semantic.get("user:tea")
        assert after.value == "user now drinks coffee"
        # the new value is not a merge product, but the originals stay
        assert "consolidated_into" not in after.metadata
        (rec,) = after.metadata["consolidation_history"]
        assert rec == {**before["consolidated_into"], "superseded_value": "refined"}

    async def test_overwrite_keeps_consolidation_history(self, semantic):
        await self._survivor(semantic)
        await semantic.upsert(SemanticEntry(key="user:tea3", value="tea at dawn", source="session:y:fact:1"))
        await semantic.consolidate(
            [await semantic.get("user:tea"), await semantic.get("user:tea3")],
            refined_value="second", survivor_key="user:tea",
            sacrificed_content="x", merge_rationale="r2")
        meta = (await semantic.get("user:tea")).metadata
        hist, current = meta["consolidation_history"], meta["consolidated_into"]
        await semantic.upsert(SemanticEntry(key="user:tea", value="v3", source="session:z:fact:0"))
        assert (await semantic.get("user:tea")).metadata["consolidation_history"] == [
            *hist, {**current, "superseded_value": "second"}]

    async def test_overwritten_value_is_no_longer_a_distill_product(self, semantic):
        await self._survivor(semantic)
        await semantic.upsert(SemanticEntry(key="user:tea", value="other", source="session:z:fact:0"))
        meta = (await semantic.get("user:tea")).metadata
        assert "produced_by" not in meta and "distilled_at" not in meta

    async def test_same_value_rewrite_keeps_everything(self, semantic):
        before = await self._survivor(semantic)
        await semantic.upsert(SemanticEntry(key="user:tea", value="refined", source="session:s:fact:0"))
        meta = (await semantic.get("user:tea")).metadata
        assert meta["consolidated_into"] == before["consolidated_into"]
        assert meta["produced_by"] == "distill_dream"

    async def test_governed_supersede_keeps_originals(self, semantic, db_conn):
        from loom.core.memory.episodic import EpisodicMemory
        from loom.core.memory.governance import MemoryGovernor

        # session-sourced survivor + same-tier newcomer → SUPERSEDE writes through
        before = await self._survivor(semantic, key="user:tea_dup")
        gov = MemoryGovernor(semantic, ProceduralMemory(db_conn), EpisodicMemory(db_conn), db_conn)
        res = await gov.governed_upsert(SemanticEntry(
            key="user:tea_dup", value="user switched to coffee", source="session:z:fact:0"))
        assert res.written, res
        after = await semantic.get("user:tea_dup")
        assert after.value == "user switched to coffee"
        (rec,) = after.metadata["consolidation_history"]
        assert rec["sacrificed_entries"] == before["consolidated_into"]["sacrificed_entries"]
        assert rec["superseded_value"] == "refined"

    async def test_plain_overwrite_metadata_unchanged(self, semantic):
        await semantic.upsert(SemanticEntry(key="k", value="a", source="manual", metadata={"x": 1}))
        await semantic.upsert(SemanticEntry(key="k", value="b", source="manual", metadata={"y": 2}))
        meta = (await semantic.get("k")).metadata
        assert "x" not in meta and meta["y"] == 2
        assert "consolidated_into" not in meta and "consolidation_history" not in meta


class TestConsolidateWriteChecks:
    async def test_merging_a_distilled_survivor_drops_its_marks(self, semantic):
        await _seed_pair(semantic)
        await semantic.consolidate(
            [await semantic.get("user:tea"), await semantic.get("user:tea_dup")],
            refined_value="distilled", survivor_key="user:tea", sacrificed_content="x",
            merge_rationale="r", extra_metadata={"produced_by": "distill_dream",
                                                 "distilled_at": "2026-10-19"})
        await semantic.upsert(SemanticEntry(key="user:tea3", value="tea 3", source="session:y:fact:1"))
        await semantic.consolidate(   # a later convergent merge, no extra_metadata
            [await semantic.get("user:tea"), await semantic.get("user:tea3")],
            refined_value="merged", survivor_key="user:tea", sacrificed_content="x",
            merge_rationale="r2")
        meta = (await semantic.get("user:tea")).metadata
        assert "produced_by" not in meta and "distilled_at" not in meta

    async def test_expected_versions_refuse_a_changed_fact(self, semantic, db_conn):
        from loom.core.memory.semantic import StaleWriteError

        a, b = await _seed_pair(semantic)
        versions = {e.key: e.updated_at.isoformat() for e in (a, b)}
        await db_conn.execute("UPDATE semantic_entries SET value='newer', "
                              "updated_at='2099-01-01T00:00:00+00:00' WHERE key='user:tea_dup'")
        await db_conn.commit()
        with pytest.raises(StaleWriteError):
            await semantic.consolidate(
                [a, b], refined_value="r", survivor_key="user:tea", sacrificed_content="x",
                merge_rationale="r", expected_versions=versions)
        assert (await semantic.get("user:tea_dup")).value == "newer"
        assert (await semantic.get("user:tea")).value == a.value

    async def test_expected_versions_preserve_what_the_store_holds(self, semantic):
        a, b = await _seed_pair(semantic)
        versions = {e.key: e.updated_at.isoformat() for e in (a, b)}
        stale_copy = SemanticEntry(key=b.key, value="caller's outdated copy", source=b.source,
                                   updated_at=b.updated_at)
        await semantic.consolidate(
            [a, stale_copy], refined_value="r", survivor_key="user:tea", sacrificed_content="x",
            merge_rationale="r", expected_versions=versions)
        (rec,) = (await semantic.get("user:tea")).metadata["consolidated_into"]["sacrificed_entries"]
        assert rec["value"] == b.value
