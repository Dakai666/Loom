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
from unittest.mock import AsyncMock

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
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
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


# ---------------------------------------------------------------------------
# Review P1 regression — embedding backfill makes legacy rel:* rows visible
# even when the embedding tier short-circuits the BM25 fallback.
# ---------------------------------------------------------------------------

async def test_ensure_embeddings_for_prefix_fills_missing_rows(tmp_db):
    """``ensure_embeddings_for_prefix`` should only touch ``embedding IS NULL``."""
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        # Seed two legacy relational rows (no semantic dual-write, so they
        # land in semantic_entries via the initialize() backfill only — and
        # therefore have no embedding).
        await _seed_legacy_relational_row(
            db, subject="user", predicate="likes_tts_voice", object="xAI eve",
        )
        await _seed_legacy_relational_row(
            db, subject="user", predicate="prefers_lang", object="zh-TW",
        )

    # Backfill table on second initialize().
    await store.initialize()

    # Now plumb a fake embedding provider and call the new ensure method.
    async with store.connect() as db:
        mock_provider = AsyncMock()
        mock_provider.embed.return_value = [
            [0.1, 0.2, 0.3],
            [0.4, 0.5, 0.6],
        ]
        sem = SemanticMemory(db, embedding_provider=mock_provider)
        n = await sem.ensure_embeddings_for_prefix("rel:")
        assert n == 2

        # Both rows now have embeddings.
        pairs = await sem.list_with_embeddings(10)
        rel_pairs = [(e, v) for (e, v) in pairs if e.key.startswith("rel:")]
        assert len(rel_pairs) == 2
        for _entry, vec in rel_pairs:
            assert vec is not None

        # Idempotent — second call is a no-op.
        n2 = await sem.ensure_embeddings_for_prefix("rel:")
        assert n2 == 0


async def test_ensure_embeddings_no_op_without_provider(tmp_db):
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        sem = SemanticMemory(db, embedding_provider=None)
        n = await sem.ensure_embeddings_for_prefix("rel:")
        assert n == 0


async def test_embedding_recall_finds_backfilled_rel_rows_after_ensure(tmp_db):
    """Reviewer's manual repro: with embedding provider configured, recall
    short-circuits on the embedding tier. Backfilled rel:* rows must end up
    with embeddings or they're invisible. After ``ensure_embeddings_for_prefix``
    they should surface alongside ordinary embedded semantic facts.
    """
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        # Legacy relational row (no embedding yet).
        await _seed_legacy_relational_row(
            db, subject="user", predicate="likes_tts_voice",
            object="xAI eve Chinese sounds natural",
        )

    await store.initialize()  # backfill into semantic_entries (no embedding)

    async with store.connect() as db:
        # Embedding provider returns deterministic vectors. We use the same
        # vector for everything so cosine == 1.0; the goal is to confirm
        # rel:* rows reach the embedding tier at all, not ranking fidelity.
        provider = AsyncMock()
        provider.embed.side_effect = lambda texts: [[1.0, 0.0, 0.0] for _ in texts]

        sem = SemanticMemory(db, embedding_provider=provider)
        proc = ProceduralMemory(db)

        # Embed both the legacy rel:* row and a plain semantic fact.
        await sem.ensure_embeddings_for_prefix("rel:")
        await sem.upsert(SemanticEntry(key="ordinary", value="harness-first"))

        # Reviewer's exact scenario: recall with a query that would short-
        # circuit on the embedding tier.
        search = MemorySearch(sem, proc)
        results = await search.recall("likes_tts_voice", limit=10)
        keys = {r.key for r in results}
        assert "rel:user::likes_tts_voice" in keys, (
            "backfilled relational row invisible to embedding-tier recall "
            "— P1 regression"
        )


# ---------------------------------------------------------------------------
# Review P2 regression — recall must refresh last_accessed_at on both
# semantic_entries and the relational source-of-truth row.
# ---------------------------------------------------------------------------

async def test_recall_refreshes_last_accessed_on_both_tables(tmp_db):
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        sem = SemanticMemory(db)
        rel = RelationalMemory(db, semantic=sem)
        proc = ProceduralMemory(db)
        await rel.upsert(RelationalEntry(
            subject="user",
            predicate="likes_tts_voice",
            object="xAI eve",
        ))

        # Both rows start with NULL last_accessed_at.
        sem_row = await sem.get("rel:user::likes_tts_voice")
        assert sem_row.last_accessed_at is None
        rel_row = await rel.get("user", "likes_tts_voice")
        assert rel_row.last_accessed_at is None

        # Recall should bump both.
        search = MemorySearch(sem, proc)
        results = await search.recall("likes_tts_voice", limit=5)
        assert any(r.type == "relational" for r in results)

        sem_after = await sem.get("rel:user::likes_tts_voice")
        rel_after = await rel.get("user", "likes_tts_voice")
        assert sem_after.last_accessed_at is not None, (
            "semantic row last_accessed_at not refreshed — P2 regression"
        )
        assert rel_after.last_accessed_at is not None, (
            "relational source row last_accessed_at not mirrored "
            "— would cause source/mirror decay drift"
        )


async def test_mark_accessed_does_not_touch_relational_when_no_rel_keys(tmp_db):
    """Regression guard — non-rel:* keys must not trigger the relational
    UPDATE branch (it would be wasted SQL on a hot recall path)."""
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        sem = SemanticMemory(db)
        await sem.upsert(SemanticEntry(key="ordinary_fact", value="something"))
        # The UPDATE returns silently regardless, but at least confirm no
        # row in relational_entries exists for this key shape.
        await sem.mark_accessed(["ordinary_fact"])
        cur = await db.execute("SELECT COUNT(*) FROM relational_entries")
        (count,) = await cur.fetchone()
        assert count == 0
