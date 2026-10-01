"""
Admission gate: whole-corpus semantic dedup + reinforcement (#603 step D).

Measured on the live store (2026-10-01): of the last 30 days' session-compress
writes, 81/311 already had a ≥0.85-cosine sibling, and 64 of those siblings
were older than the 7-day window the embedding check used — the gate never
saw them. The embedding lookup is a SQL vector scan, so widening it to the
whole corpus costs ~nothing; the lexical Jaccard pass (Python, O(n)) keeps its
7-day / 500-row window.

A rejected re-learning is a signal, not waste: the existing fact it repeats is
reinforced (``last_accessed_at`` bumped), which restarts its decay clock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest_asyncio

from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.governance import MemoryGovernor
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.store import SQLiteStore


@pytest_asyncio.fixture
async def db(tmp_path):
    store = SQLiteStore(str(tmp_path / "adm.db"))
    await store.initialize()
    async with store.connect() as conn:
        yield conn


def _same_vector_provider():
    """Every text embeds to the same unit vector → cosine 1.0 with any seed."""
    provider = AsyncMock()
    provider.embed.return_value = [[1.0, 0.0, 0.0]]
    return provider


async def _age(db, key: str, days: int) -> None:
    ts = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    await db.execute(
        "UPDATE semantic_entries SET created_at = ?, updated_at = ?, "
        "last_accessed_at = NULL WHERE key = ?",
        (ts, ts, key),
    )
    await db.commit()


async def _governor(db, semantic, config=None):
    return MemoryGovernor(
        semantic=semantic,
        procedural=ProceduralMemory(db),
        episodic=EpisodicMemory(db),
        db=db,
        config={"admission_threshold": 0.5, **(config or {})},
    )


async def _seed_old(db, semantic, *, days: int = 60) -> None:
    await semantic.upsert(SemanticEntry(
        key="old:circadian-structure",
        value="evening_closure has three fixed parts: good night, distill, set tomorrow",
        source="session:old",
    ))
    await _age(db, "old:circadian-structure", days)


class TestWholeCorpusSemanticDedup:
    async def test_paraphrase_older_than_lexical_window_is_rejected(self, db):
        semantic = SemanticMemory(db, embedding_provider=_same_vector_provider())
        await _seed_old(db, semantic, days=60)
        gov = await _governor(db, semantic)

        results = await gov.evaluate_admission(
            ["the nightly closure phase: say good night, journal the day, pick a program"],
            source="session:new",
        )
        assert results[0].admitted is False
        assert results[0].reason == "duplicate_semantic"
        assert results[0].duplicate_of == "old:circadian-structure"

    async def test_window_is_configurable(self, db):
        semantic = SemanticMemory(db, embedding_provider=_same_vector_provider())
        await _seed_old(db, semantic, days=60)
        gov = await _governor(db, semantic, {"semantic_dup_window_days": 7})

        results = await gov.evaluate_admission(
            ["the nightly closure phase: say good night, journal the day, pick a program"],
            source="session:new",
        )
        assert results[0].admitted is True
        assert results[0].duplicate_of is None


class TestReinforcement:
    async def test_rejected_duplicate_reinforces_existing_fact(self, db):
        semantic = SemanticMemory(db, embedding_provider=_same_vector_provider())
        await _seed_old(db, semantic, days=60)
        gov = await _governor(db, semantic)
        before = datetime.now(UTC)

        await gov.evaluate_admission(
            ["the nightly closure phase: say good night, journal the day, pick a program"],
            source="session:new",
        )
        entry = await semantic.get("old:circadian-structure")
        assert entry.last_accessed_at is not None
        assert entry.last_accessed_at >= before - timedelta(seconds=1)

    async def test_admitted_fact_touches_nothing(self, db):
        provider = AsyncMock()
        # seed vs candidate orthogonal → no semantic dup
        provider.embed.side_effect = [[[1.0, 0.0, 0.0]], [[0.0, 1.0, 0.0]]]
        semantic = SemanticMemory(db, embedding_provider=provider)
        await semantic.upsert(SemanticEntry(
            key="old:tea", value="user drinks oolong tea every afternoon", source="memorize",
        ))
        await _age(db, "old:tea", 60)
        gov = await _governor(db, semantic)

        results = await gov.evaluate_admission(
            ["the weather station API rate limit is sixty calls per hour"],
            source="session:new",
        )
        assert results[0].admitted is True
        assert (await semantic.get("old:tea")).last_accessed_at is None

    async def test_reinforcement_failure_does_not_break_admission(self, db):
        semantic = SemanticMemory(db, embedding_provider=_same_vector_provider())
        await _seed_old(db, semantic, days=60)
        gov = await _governor(db, semantic)

        async def boom(keys):
            raise RuntimeError("db locked")
        semantic.mark_accessed = boom  # type: ignore[method-assign]

        results = await gov.evaluate_admission(
            ["the nightly closure phase: say good night, journal the day, pick a program"],
            source="session:new",
        )
        assert results[0].admitted is False
        assert results[0].reason == "duplicate_semantic"


class TestObservability:
    async def test_audit_event_counts_semantic_rejections_and_reinforcements(self, db):
        semantic = SemanticMemory(db, embedding_provider=_same_vector_provider())
        await _seed_old(db, semantic, days=60)
        gov = await _governor(db, semantic)

        await gov.evaluate_admission(
            ["the nightly closure phase: say good night, journal the day, pick a program"],
            source="session:new",
        )
        cur = await db.execute(
            "SELECT details FROM audit_log WHERE tool_name = 'governance:admission'"
        )
        details = json.loads((await cur.fetchone())[0])
        assert details["rejected_semantic"] == 1
        assert details["reinforced"] == 1

    async def test_all_admitted_batch_is_still_audited(self, db):
        semantic = SemanticMemory(db)  # no embeddings → lexical only
        gov = await _governor(db, semantic)
        await gov.evaluate_admission(
            ["the weather station API rate limit is sixty calls per hour"],
            source="session:new",
        )
        cur = await db.execute(
            "SELECT details FROM audit_log WHERE tool_name = 'governance:admission'"
        )
        details = json.loads((await cur.fetchone())[0])
        assert details["admitted"] == 1 and details["reinforced"] == 0
