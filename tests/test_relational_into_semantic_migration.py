"""
Tests for issue #451 phase A — routing relational triples through the
semantic store so ``recall`` can naturally surface them.

Covers
------
* Backfill at ``SQLiteStore.initialize()`` copies existing
  ``relational_entries`` rows into ``semantic_entries`` with
  ``rel:{S}::{P}`` keys.
* Backfill is idempotent (re-running ``initialize()`` does not duplicate).
* ``RelationalMemory.upsert()`` dual-writes into the semantic store when
  a SemanticMemory instance is plumbed in.
* ``RelationalMemory.delete()`` mirrors deletion into semantic.
* ``MemorySearch.recall()`` returns matching triples with
  ``type="relational"`` and the relational ``key`` / ``value`` shape.
* ``relational_bridge`` round-trips ``RelationalEntry`` ↔ ``SemanticEntry``.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
import pytest_asyncio

from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.relational import RelationalEntry, RelationalMemory
from loom.core.memory.relational_bridge import (
    make_rel_key,
    semantic_to_triple,
    triple_to_semantic,
)
from loom.core.memory.search import MemorySearch
from loom.core.memory.semantic import SemanticMemory
from loom.core.memory.store import SQLiteStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest_asyncio.fixture
async def fresh_store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


# ---------------------------------------------------------------------------
# Bridge encoding/decoding
# ---------------------------------------------------------------------------

def test_bridge_round_trip_preserves_triple():
    original = RelationalEntry(
        subject="user",
        predicate="likes_tts_voice",
        object="xAI eve",
        confidence=0.9,
        source="agent",
        metadata={"note": "told to me"},
    )
    sem = triple_to_semantic(original)

    assert sem.key == "rel:user::likes_tts_voice"
    assert sem.value == "user likes_tts_voice xAI eve"
    assert sem.metadata["subject"] == "user"
    assert sem.metadata["predicate"] == "likes_tts_voice"
    assert sem.metadata["object"] == "xAI eve"
    assert sem.metadata["note"] == "told to me"
    assert sem.confidence == 0.9
    assert sem.source == "agent"

    decoded = semantic_to_triple(sem)
    assert decoded is not None
    assert decoded.subject == original.subject
    assert decoded.predicate == original.predicate
    assert decoded.object == original.object
    assert decoded.confidence == original.confidence
    assert decoded.source == original.source
    assert "subject" not in decoded.metadata


def test_bridge_handles_subject_with_colons():
    """Subjects like ``openai:musk-trial:2026-05-15`` must round-trip cleanly."""
    e = RelationalEntry(
        subject="openai:musk-trial:2026-05-15",
        predicate="関連到",
        object="OpenAI IPO 進程",
    )
    sem = triple_to_semantic(e)
    assert sem.key == "rel:openai:musk-trial:2026-05-15::関連到"
    decoded = semantic_to_triple(sem)
    assert decoded.subject == "openai:musk-trial:2026-05-15"
    assert decoded.predicate == "関連到"


def test_semantic_to_triple_rejects_non_relational_keys():
    from loom.core.memory.semantic import SemanticEntry
    e = SemanticEntry(key="user_pref:tts", value="something")
    assert semantic_to_triple(e) is None


# ---------------------------------------------------------------------------
# Dual-write via RelationalMemory.upsert
# ---------------------------------------------------------------------------

async def test_upsert_dual_writes_into_semantic(tmp_db):
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        sem = SemanticMemory(db)
        rel = RelationalMemory(db, semantic=sem)
        await rel.upsert(RelationalEntry(
            subject="user",
            predicate="prefers_voice",
            object="eve",
        ))

        # Relational table has the original row
        triple = await rel.get("user", "prefers_voice")
        assert triple is not None
        assert triple.object == "eve"

        # Semantic table has the bridge row
        sem_entry = await sem.get("rel:user::prefers_voice")
        assert sem_entry is not None
        assert sem_entry.value == "user prefers_voice eve"
        assert sem_entry.metadata["subject"] == "user"
        assert sem_entry.metadata["predicate"] == "prefers_voice"
        assert sem_entry.metadata["object"] == "eve"


async def test_upsert_without_semantic_does_not_break(tmp_db):
    """Backward-compat: passing semantic=None still works (no dual-write)."""
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        rel = RelationalMemory(db, semantic=None)
        await rel.upsert(RelationalEntry(
            subject="x", predicate="y", object="z",
        ))
        triple = await rel.get("x", "y")
        assert triple is not None


async def test_delete_mirrors_into_semantic(tmp_db):
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        sem = SemanticMemory(db)
        rel = RelationalMemory(db, semantic=sem)
        await rel.upsert(RelationalEntry(
            subject="a", predicate="b", object="c",
        ))
        assert await sem.get("rel:a::b") is not None

        deleted = await rel.delete("a", "b")
        assert deleted is True
        assert await sem.get("rel:a::b") is None


# ---------------------------------------------------------------------------
# Backfill in SQLiteStore.initialize()
# ---------------------------------------------------------------------------

async def _seed_legacy_relational_row(db: aiosqlite.Connection, **kwargs) -> None:
    """Insert directly into ``relational_entries`` bypassing dual-write,
    simulating a row that pre-dates phase A."""
    import uuid
    from datetime import UTC, datetime
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """
        INSERT INTO relational_entries
            (id, subject, predicate, object, confidence, source, metadata,
             created_at, updated_at, domain, temporal, last_accessed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            kwargs["subject"],
            kwargs["predicate"],
            kwargs["object"],
            kwargs.get("confidence", 1.0),
            kwargs.get("source", "dreaming"),
            json.dumps(kwargs.get("metadata", {})),
            now,
            now,
            kwargs.get("domain", "knowledge"),
            kwargs.get("temporal", "recent"),
            None,
        ),
    )
    await db.commit()


async def test_backfill_copies_existing_triples_into_semantic(tmp_db):
    # Pre-seed the relational table without dual-write, then re-initialize.
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        await _seed_legacy_relational_row(
            db, subject="legacy", predicate="written_by", object="dream_cycle",
        )

    # Second initialize() should backfill the legacy row.
    await store.initialize()
    async with store.connect() as db:
        sem = SemanticMemory(db)
        entry = await sem.get("rel:legacy::written_by")
        assert entry is not None
        assert entry.value == "legacy written_by dream_cycle"
        assert entry.metadata["subject"] == "legacy"
        assert entry.metadata["predicate"] == "written_by"
        assert entry.metadata["object"] == "dream_cycle"


async def test_backfill_is_idempotent(tmp_db):
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        await _seed_legacy_relational_row(
            db, subject="s", predicate="p", object="o",
        )

    # Run initialize multiple times — should still be exactly one row.
    for _ in range(3):
        await store.initialize()

    async with store.connect() as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM semantic_entries WHERE key = ?",
            ("rel:s::p",),
        )
        (count,) = await cur.fetchone()
        assert count == 1


# ---------------------------------------------------------------------------
# Recall surfacing
# ---------------------------------------------------------------------------

async def test_recall_returns_relational_triples_tagged_as_relational(tmp_db):
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        sem = SemanticMemory(db)
        rel = RelationalMemory(db, semantic=sem)
        proc = ProceduralMemory(db)
        await rel.upsert(RelationalEntry(
            subject="user",
            predicate="likes_tts_voice",
            object="xAI eve Chinese output sounds natural",
        ))

        search = MemorySearch(sem, proc)
        results = await search.recall("likes_tts_voice", limit=5)

        assert results, "recall returned nothing for a directly-indexed triple"
        match = next(
            (r for r in results if r.key == "rel:user::likes_tts_voice"), None,
        )
        assert match is not None
        assert match.type == "relational"
        assert "xAI eve" in match.value


async def test_recall_does_not_relabel_ordinary_semantic_facts(tmp_db):
    """Regression guard — ``type="semantic"`` must still apply to plain facts."""
    from loom.core.memory.semantic import SemanticEntry

    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        sem = SemanticMemory(db)
        proc = ProceduralMemory(db)
        await sem.upsert(SemanticEntry(
            key="ordinary_fact",
            value="Loom is harness-first and memory-native.",
        ))
        search = MemorySearch(sem, proc)
        results = await search.recall("harness-first memory-native", limit=5)
        assert results
        ordinary = next(r for r in results if r.key == "ordinary_fact")
        assert ordinary.type == "semantic"
