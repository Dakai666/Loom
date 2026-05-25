"""
Tests for issue #451 — relational triples on top of the semantic store.

Phase A (additive bridge + dual-write) → Phase B (cutover; relational
table dropped). After phase B the surface is:

* The bridge encodes / decodes (S, P, O) ↔ ``SemanticEntry`` rows keyed
  ``rel:{subject}::{predicate}``.
* Legacy ``relational_entries`` rows get a one-shot backfill into
  ``semantic_entries`` on the first boot after upgrade, then the table
  is dropped.
* ``MemorySearch.recall`` returns the bridged rows tagged
  ``type="relational"`` so the agent sees the kind explicitly without
  switching verbs.
* Embedding-tier coverage for backfilled rows is filled lazily by
  ``SemanticMemory.ensure_embeddings_for_prefix("rel:")`` from
  ``LoomSession.start()``.
* ``MemorySearch._mark_accessed`` refreshes ``last_accessed_at`` on the
  semantic row for ``"relational"`` hits.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio

from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.relational_bridge import (
    RelationalEntry,
    delete_triple,
    get_triple,
    make_rel_key,
    query_triples,
    semantic_to_triple,
    triple_to_semantic,
    upsert_triple,
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
    e = SemanticEntry(key="user_pref:tts", value="something")
    assert semantic_to_triple(e) is None


# ---------------------------------------------------------------------------
# Bridge helpers — replace the retired ``RelationalMemory`` class
# ---------------------------------------------------------------------------

async def test_upsert_triple_writes_into_semantic(tmp_db):
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        sem = SemanticMemory(db)
        await upsert_triple(sem, RelationalEntry(
            subject="user", predicate="prefers_voice", object="eve",
        ))

        # Semantic table has the bridge row
        sem_entry = await sem.get("rel:user::prefers_voice")
        assert sem_entry is not None
        assert sem_entry.value == "user prefers_voice eve"
        assert sem_entry.metadata["subject"] == "user"

        # Bridge query path returns the round-tripped triple
        triple = await get_triple(sem, "user", "prefers_voice")
        assert triple is not None
        assert triple.object == "eve"


async def test_delete_triple_removes_from_semantic(tmp_db):
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        sem = SemanticMemory(db)
        await upsert_triple(sem, RelationalEntry(
            subject="a", predicate="b", object="c",
        ))
        assert await sem.get("rel:a::b") is not None

        deleted = await delete_triple(sem, "a", "b")
        assert deleted is True
        assert await sem.get("rel:a::b") is None


# ---------------------------------------------------------------------------
# Backfill + table drop in SQLiteStore.initialize()
# ---------------------------------------------------------------------------

async def _create_legacy_relational_table(db: aiosqlite.Connection) -> None:
    """Recreate the pre-#451-phase-B ``relational_entries`` schema so we
    can seed legacy rows and exercise the migration path."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS relational_entries (
            id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            source TEXT NOT NULL DEFAULT 'agent',
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            domain TEXT NOT NULL DEFAULT 'knowledge',
            temporal TEXT NOT NULL DEFAULT 'recent',
            last_accessed_at TEXT,
            UNIQUE(subject, predicate)
        )
    """)
    await db.commit()


async def _seed_legacy_relational_row(db: aiosqlite.Connection, **kwargs) -> None:
    """Insert directly into ``relational_entries`` (recreated for the test),
    simulating a row that pre-dates phase B."""
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


async def test_backfill_copies_legacy_relational_into_semantic(tmp_db):
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        await _create_legacy_relational_table(db)
        await _seed_legacy_relational_row(
            db, subject="legacy", predicate="written_by", object="dream_cycle",
        )

    # Re-init triggers the backfill + DROP TABLE path.
    await store.initialize()
    async with store.connect() as db:
        sem = SemanticMemory(db)
        entry = await sem.get("rel:legacy::written_by")
        assert entry is not None
        assert entry.value == "legacy written_by dream_cycle"
        assert entry.metadata["subject"] == "legacy"

        # Table must be gone after phase B drop.
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='relational_entries'"
        )
        assert await cur.fetchone() is None


async def test_initialize_is_a_noop_when_legacy_table_already_absent(tmp_db):
    """Fresh installs never have the table — initialize() must complete
    cleanly and the bridge-only write path must work afterwards."""
    store = SQLiteStore(tmp_db)
    await store.initialize()
    # Run a second time — exercises the no-table-found branch.
    await store.initialize()

    async with store.connect() as db:
        sem = SemanticMemory(db)
        await upsert_triple(sem, RelationalEntry(
            subject="s", predicate="p", object="o",
        ))
        got = await get_triple(sem, "s", "p")
        assert got is not None
        assert got.object == "o"


# ---------------------------------------------------------------------------
# Recall surfacing
# ---------------------------------------------------------------------------

async def test_recall_returns_relational_triples_tagged_as_relational(tmp_db):
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        sem = SemanticMemory(db)
        proc = ProceduralMemory(db)
        await upsert_triple(sem, RelationalEntry(
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
# Embedding backfill (PR #452 review P1 regression)
# ---------------------------------------------------------------------------

async def test_ensure_embeddings_for_prefix_fills_missing_rows(tmp_db):
    """``ensure_embeddings_for_prefix`` should only touch ``embedding IS NULL``."""
    store = SQLiteStore(tmp_db)
    await store.initialize()

    # Seed semantic rows directly without an embedding provider so they
    # land without vectors — simulating the migrated-but-not-yet-embedded
    # state legacy DBs hit after phase B's first boot.
    async with store.connect() as db:
        sem = SemanticMemory(db)
        await upsert_triple(sem, RelationalEntry(
            subject="user", predicate="likes_tts_voice", object="xAI eve",
        ))
        await upsert_triple(sem, RelationalEntry(
            subject="user", predicate="prefers_lang", object="zh-TW",
        ))

    async with store.connect() as db:
        mock_provider = AsyncMock()
        mock_provider.embed.return_value = [
            [0.1, 0.2, 0.3],
            [0.4, 0.5, 0.6],
        ]
        sem = SemanticMemory(db, embedding_provider=mock_provider)
        n = await sem.ensure_embeddings_for_prefix("rel:")
        assert n == 2

        pairs = await sem.list_with_embeddings(10)
        rel_pairs = [(e, v) for (e, v) in pairs if e.key.startswith("rel:")]
        assert len(rel_pairs) == 2
        for _entry, vec in rel_pairs:
            assert vec is not None

        # Idempotent — second call is a no-op.
        assert await sem.ensure_embeddings_for_prefix("rel:") == 0


async def test_ensure_embeddings_no_op_without_provider(tmp_db):
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        sem = SemanticMemory(db, embedding_provider=None)
        assert await sem.ensure_embeddings_for_prefix("rel:") == 0


async def test_embedding_recall_finds_rel_rows_after_ensure(tmp_db):
    """With an embedding provider configured, ``recall`` short-circuits
    on the embedding tier. ``rel:*`` rows that lack embeddings must end
    up with vectors via ``ensure_embeddings_for_prefix`` or they're
    invisible (PR #452 review P1)."""
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        sem = SemanticMemory(db)  # No provider — write w/o embedding
        await upsert_triple(sem, RelationalEntry(
            subject="user", predicate="likes_tts_voice",
            object="xAI eve Chinese sounds natural",
        ))

    async with store.connect() as db:
        # Deterministic vector — every text gets the same one, so cosine == 1.
        provider = AsyncMock()
        provider.embed.side_effect = lambda texts: [[1.0, 0.0, 0.0] for _ in texts]

        sem = SemanticMemory(db, embedding_provider=provider)
        proc = ProceduralMemory(db)

        await sem.ensure_embeddings_for_prefix("rel:")
        await sem.upsert(SemanticEntry(key="ordinary", value="harness-first"))

        search = MemorySearch(sem, proc)
        results = await search.recall("likes_tts_voice", limit=10)
        keys = {r.key for r in results}
        assert "rel:user::likes_tts_voice" in keys, (
            "rel:* row invisible to embedding-tier recall — P1 regression"
        )


# ---------------------------------------------------------------------------
# last_accessed_at refresh (PR #452 review P2 regression)
# ---------------------------------------------------------------------------

async def test_recall_refreshes_last_accessed_for_relational_hits(tmp_db):
    """``_mark_accessed`` must treat ``"relational"`` results as markable
    (same backing table). The phase-A relational_entries mirror was
    retired with the table in phase B, so verifying the semantic row's
    timestamp is sufficient."""
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        sem = SemanticMemory(db)
        proc = ProceduralMemory(db)
        await upsert_triple(sem, RelationalEntry(
            subject="user", predicate="likes_tts_voice", object="xAI eve",
        ))

        sem_row = await sem.get("rel:user::likes_tts_voice")
        assert sem_row.last_accessed_at is None

        search = MemorySearch(sem, proc)
        results = await search.recall("likes_tts_voice", limit=5)
        assert any(r.type == "relational" for r in results)

        sem_after = await sem.get("rel:user::likes_tts_voice")
        assert sem_after.last_accessed_at is not None, (
            "rel:* row last_accessed_at not refreshed — P2 regression"
        )


async def test_mark_accessed_handles_empty_keys(tmp_db):
    """Hot recall path: empty result list must not hit the database."""
    store = SQLiteStore(tmp_db)
    await store.initialize()
    async with store.connect() as db:
        sem = SemanticMemory(db)
        await sem.mark_accessed([])  # No raise, no SQL touch.
