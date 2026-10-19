"""Dream output governance (#613).

Free/themed dreams became the largest memory producer (61% of writes in the
28 days to 2026-10-19) while bypassing the admission gate, and their
association triples never left the store. Contracts:

  A. dream_cycle routes triples through the admission gate + governed_upsert
     when a governor is wired: near-duplicates are not written and instead
     reinforce the fact they repeat.
  C. dreaming-sourced rows decay on a short half-life
     (DREAMING_HALF_LIFE_DAYS); a recall resets the clock, so used triples
     survive and unused ones flow out through the normal lifecycle.
  +  the lifecycle never deletes rows without a fresh snapshot.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import pytest_asyncio

import loom.core.memory.backup as backup_mod
from loom.core.cognition.dreaming import dream_cycle
from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.governance import MemoryGovernor
from loom.core.memory.lifecycle import (
    DREAMING_HALF_LIFE_DAYS,
    MemoryLifecycle,
    effective_confidence,
    half_life_for,
)
from loom.core.memory.ontology import (
    DOMAIN_KNOWLEDGE,
    DOMAIN_SELF,
    TEMPORAL_ARCHIVED,
    TEMPORAL_MILESTONE,
    TEMPORAL_RECENT,
)
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.store import SQLiteStore


class _MarkerEmbeddings:
    """Same GROUP marker → identical vector; otherwise orthogonal."""

    _BASIS = {
        "GROUPA": [1.0, 0.0, 0.0, 0.0],
        "GROUPB": [0.0, 1.0, 0.0, 0.0],
    }

    async def embed(self, texts):
        out = []
        for t in texts:
            vec = [0.0, 0.0, 1.0, 0.0]
            for marker, basis in self._BASIS.items():
                if marker in t:
                    vec = basis
                    break
            out.append(vec)
        return out


@pytest_asyncio.fixture
async def db_conn(tmp_path):
    s = SQLiteStore(str(tmp_path / "dream_gov.db"))
    await s.initialize()
    async with s.connect() as db:
        yield db


@pytest_asyncio.fixture
async def semantic(db_conn):
    return SemanticMemory(db_conn, embedding_provider=_MarkerEmbeddings())


@pytest_asyncio.fixture
async def governor(db_conn, semantic):
    return MemoryGovernor(
        semantic, ProceduralMemory(db_conn), EpisodicMemory(db_conn), db_conn,
    )


def _llm_returning(triples: list[dict]):
    async def fn(messages):
        return json.dumps(triples)
    return fn


async def _insert(db, key, *, source, age_days, accessed_days=None,
                  domain=DOMAIN_KNOWLEDGE, temporal=TEMPORAL_RECENT, conf=0.7):
    ts = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    acc = (
        (datetime.now(UTC) - timedelta(days=accessed_days)).isoformat()
        if accessed_days is not None else None
    )
    await db.execute(
        "INSERT INTO semantic_entries (id, key, value, confidence, source, "
        "metadata, created_at, updated_at, domain, temporal, last_accessed_at) "
        "VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?)",
        (key, key, f"value of {key}", conf, source, ts, ts, domain, temporal, acc),
    )
    await db.commit()


# ── A. admission gate on the dream write path ─────────────────────────────

class TestDreamAdmission:
    async def test_near_duplicate_triple_not_written_and_reinforces(
        self, semantic, governor,
    ):
        await semantic.upsert(SemanticEntry(
            key="fact:a", value="GROUPA memory search mixes bm25 and vectors",
            source="manual",
        ))
        triples = [
            {"subject": "GROUPA memory search", "predicate": "parallels",
             "object": "hybrid retrieval design", "insight": "dup"},
            {"subject": "GROUPB circadian rhythm", "predicate": "enables",
             "object": "predictable autonomy windows", "insight": "new"},
        ]
        result = await dream_cycle(
            semantic=semantic, llm_fn=_llm_returning(triples), governor=governor,
        )

        assert result["triples_found"] == 2
        assert result["triples_written"] == 1
        assert result["triples_rejected"] == 1
        assert await semantic.get("rel:GROUPA memory search::parallels") is None
        assert await semantic.get("rel:GROUPB circadian rhythm::enables") is not None
        # The repeated fact was re-learned, so its decay clock restarts.
        assert (await semantic.get("fact:a")).last_accessed_at is not None

    async def test_without_governor_still_writes(self, semantic):
        """Callers that don't wire a governor keep the old write-through."""
        await semantic.upsert(SemanticEntry(
            key="fact:a", value="GROUPA seed fact for sampling", source="manual",
        ))
        triples = [{"subject": "GROUPB x", "predicate": "enables",
                    "object": "y things happen", "insight": ""}]
        result = await dream_cycle(semantic=semantic, llm_fn=_llm_returning(triples))
        assert result["triples_written"] == 1


# ── C. short half-life for dreaming output ────────────────────────────────

class TestDreamingHalfLife:
    def test_dreaming_source_caps_half_life(self):
        assert half_life_for(DOMAIN_KNOWLEDGE, TEMPORAL_RECENT, source="dreaming") \
            == DREAMING_HALF_LIFE_DAYS
        # Even "never decays" milestone rows are capped for dream output.
        assert half_life_for(DOMAIN_SELF, TEMPORAL_MILESTONE, source="dreaming") \
            == DREAMING_HALF_LIFE_DAYS
        # The cap never lengthens a shorter table value.
        assert half_life_for(DOMAIN_KNOWLEDGE, TEMPORAL_ARCHIVED, source="dreaming") \
            == min(14.0, DREAMING_HALF_LIFE_DAYS)

    def test_other_sources_unchanged(self):
        assert half_life_for(DOMAIN_KNOWLEDGE, TEMPORAL_RECENT, source="session:x") == 90.0
        assert half_life_for(DOMAIN_SELF, TEMPORAL_MILESTONE, source="manual") == math.inf
        assert half_life_for(DOMAIN_KNOWLEDGE, TEMPORAL_RECENT) == 90.0

    def test_effective_confidence_halves_after_one_half_life(self):
        then = datetime.now(UTC) - timedelta(days=DREAMING_HALF_LIFE_DAYS)
        eff = effective_confidence(
            0.8, then, None, DOMAIN_KNOWLEDGE, TEMPORAL_RECENT, source="dreaming",
        )
        assert abs(eff - 0.4) < 0.01

    def test_semantic_entry_uses_its_source(self):
        then = datetime.now(UTC) - timedelta(days=DREAMING_HALF_LIFE_DAYS)
        dream = SemanticEntry(key="k", value="v", confidence=0.8, source="dreaming",
                              updated_at=then)
        other = SemanticEntry(key="k2", value="v", confidence=0.8, source="session:x",
                              updated_at=then)
        assert dream.effective_confidence() < other.effective_confidence()

    async def test_lifecycle_demotes_unused_dream_but_not_session_fact(
        self, db_conn, semantic,
    ):
        await _insert(db_conn, "rel:old dream::enables", source="dreaming", age_days=70)
        await _insert(db_conn, "session:old", source="session:x", age_days=70)
        await _insert(db_conn, "rel:used dream::enables", source="dreaming",
                      age_days=70, accessed_days=1)

        result = await MemoryLifecycle(db_conn).run()

        assert result.semantic_archived == 1
        assert (await semantic.get("rel:old dream::enables")).temporal == TEMPORAL_ARCHIVED
        assert (await semantic.get("session:old")).temporal == TEMPORAL_RECENT
        # Recalled yesterday → clock reset → kept.
        assert (await semantic.get("rel:used dream::enables")).temporal == TEMPORAL_RECENT


# ── lifecycle deletes only behind a snapshot ──────────────────────────────

class TestPruneBackup:
    async def test_delete_takes_snapshot_first(self, db_conn, semantic, tmp_path):
        await _insert(db_conn, "doomed", source="test", age_days=1000,
                      temporal=TEMPORAL_ARCHIVED)
        result = await MemoryLifecycle(db_conn).run()
        assert result.semantic_deleted == 1
        snaps = list(backup_mod.DEFAULT_BACKUP_DIR.glob("memory-pre-prune-*.db"))
        assert len(snaps) == 1

    async def test_no_snapshot_when_nothing_to_delete(self, db_conn, semantic):
        await _insert(db_conn, "fresh", source="test", age_days=1)
        await MemoryLifecycle(db_conn).run()
        assert not list(backup_mod.DEFAULT_BACKUP_DIR.glob("memory-pre-prune-*.db"))

    async def test_failed_snapshot_blocks_delete(self, db_conn, semantic, monkeypatch):
        async def failed(db, **_):
            return None

        monkeypatch.setattr(backup_mod, "snapshot_before_write", failed)
        await _insert(db_conn, "doomed", source="test", age_days=1000,
                      temporal=TEMPORAL_ARCHIVED)
        result = await MemoryLifecycle(db_conn).run()
        assert result.semantic_deleted == 0
        assert await semantic.get("doomed") is not None

    async def test_dry_run_takes_no_snapshot(self, db_conn, semantic):
        await _insert(db_conn, "doomed", source="test", age_days=1000,
                      temporal=TEMPORAL_ARCHIVED)
        await MemoryLifecycle(db_conn).run(dry_run=True)
        assert not list(backup_mod.DEFAULT_BACKUP_DIR.glob("memory-pre-prune-*.db"))
