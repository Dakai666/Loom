"""MemoryFacade → ledger memory_op emit (#322 commit 2 / doc/53 §3.5, §11.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from loom.core.ledger import (
    LedgerEmitter,
    LedgerStore,
    async_correlation_scope,
    async_turn_scope,
)
from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.facade import MemoryFacade
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.relational_bridge import RelationalEntry
from loom.core.memory.search import MemorySearch
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.store import SQLiteStore


@pytest_asyncio.fixture
async def ledger(tmp_path: Path) -> LedgerStore:
    s = LedgerStore(
        db_path=tmp_path / "ledger.db",
        blob_dir=tmp_path / "blobs",
    )
    await s.open()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
async def facade(tmp_path: Path, ledger: LedgerStore) -> MemoryFacade:
    store = SQLiteStore(str(tmp_path / "facade.db"))
    await store.initialize()
    async with store.connect() as db:
        semantic = SemanticMemory(db)
        procedural = ProceduralMemory(db)
        episodic = EpisodicMemory(db)
        search = MemorySearch(semantic, procedural)
        emitter = LedgerEmitter(ledger, session_id="sess_mem")
        yield MemoryFacade(
            semantic=semantic,
            procedural=procedural,
            episodic=episodic,
            search=search,
            ledger_emitter=emitter,
        )


async def _events_of_type(ledger: LedgerStore, evt_type: str) -> list:
    rows = await ledger.fetch_by_turn("turn_mem")
    return [r for r in rows if r.event_type == evt_type]


# ---------------------------------------------------------------------------
# Read API → memory_op.read
# ---------------------------------------------------------------------------


async def test_search_emits_memory_op_read(
    facade: MemoryFacade, ledger: LedgerStore
) -> None:
    async with async_turn_scope("turn_mem"), async_correlation_scope("c1"):
        await facade.search("anything", limit=3)

    events = await _events_of_type(ledger, "memory_op")
    assert len(events) == 1
    p = events[0].payload
    assert p["operation"] == "read"
    assert p["trigger"] == "agent_search"
    assert p["type_summary"].startswith("search:")
    assert events[0].correlation_id == "c1"


async def test_get_fact_emits_memory_op_read(
    facade: MemoryFacade, ledger: LedgerStore
) -> None:
    async with async_turn_scope("turn_mem"), async_correlation_scope("c1"):
        await facade.get_fact("nonexistent_key")

    events = await _events_of_type(ledger, "memory_op")
    assert len(events) == 1
    p = events[0].payload
    assert p["operation"] == "read"
    assert p["trigger"] == "agent_get_fact"
    assert p["type_summary"] == "semantic_fact"


# #451 phase B: ``MemoryFacade.query_relations`` retired — relational
# triples flow through ``facade.search`` / agent ``recall`` instead.
# The search emit test above already covers the read-side emit; no
# separate ``agent_query_relations`` event exists any more.


# ---------------------------------------------------------------------------
# Write API → memory_op.write
# ---------------------------------------------------------------------------


async def test_memorize_emits_memory_op_write(
    facade: MemoryFacade, ledger: LedgerStore
) -> None:
    entry = SemanticEntry(
        key="fact_1",
        value="Loom is a memory-native agent framework",
        confidence=0.9,
        source="user_explicit",
    )
    async with async_turn_scope("turn_mem"), async_correlation_scope("c1"):
        await facade.memorize(entry)

    events = await _events_of_type(ledger, "memory_op")
    assert len(events) == 1
    p = events[0].payload
    assert p["operation"] == "write"
    assert p["memory_id"] == "fact_1"
    assert p["type_summary"] == "semantic_fact"
    assert p["content_digest"].startswith("sha256:")
    assert p["trigger"] == "agent_memorize"
    # trust_tier comes from the fallback path (no governor) → "unknown"
    assert p["trust_tier"] == "unknown"


async def test_relate_emits_memory_op_write(
    facade: MemoryFacade, ledger: LedgerStore
) -> None:
    triple = RelationalEntry(subject="alice", predicate="knows", object="bob")
    async with async_turn_scope("turn_mem"), async_correlation_scope("c1"):
        await facade.relate(triple)

    events = await _events_of_type(ledger, "memory_op")
    assert len(events) == 1
    p = events[0].payload
    assert p["operation"] == "write"
    assert p["type_summary"] == "relational_triple"
    assert p["content_digest"].startswith("sha256:")
    assert p["trigger"] == "agent_relate"


# ---------------------------------------------------------------------------
# Failure isolation — ledger must never break memory writes
# ---------------------------------------------------------------------------


async def test_memorize_survives_ledger_emit_failure(
    facade: MemoryFacade, ledger: LedgerStore
) -> None:
    # Close the ledger so emit raises. memorize() must still succeed.
    await ledger.close()
    entry = SemanticEntry(
        key="fact_x", value="resilient", confidence=0.5, source="external"
    )
    async with async_turn_scope("turn_mem"), async_correlation_scope("c1"):
        result = await facade.memorize(entry)
    assert result.written is True


# ---------------------------------------------------------------------------
# No-emitter mode is a clean no-op (other tests rely on this)
# ---------------------------------------------------------------------------


async def test_facade_without_emitter_is_silent(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "x.db"))
    await store.initialize()
    async with store.connect() as db:
        semantic = SemanticMemory(db)
        procedural = ProceduralMemory(db)
        episodic = EpisodicMemory(db)
        search = MemorySearch(semantic, procedural)
        f = MemoryFacade(
            semantic=semantic,
            procedural=procedural,
            episodic=episodic,
            search=search,
        )  # no ledger_emitter
        # No turn_scope set either — must not raise.
        await f.get_fact("nope")
