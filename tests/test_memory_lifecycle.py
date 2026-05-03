"""
Tests for Memory Lifecycle (issue #281 P2):

  - half_life_for / effective_confidence — pure-function math
  - last_accessed_at refresh keeps a fact alive even when stale
  - State transitions: recent → archived → deleted (two-stage)
  - Milestone facts never decay (except knowledge / 365d)
  - Relational decay covers all sources (fixes #299)
  - DecayCycleResult preserves legacy contract
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, UTC

import pytest
import pytest_asyncio

from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.governance import MemoryGovernor
from loom.core.memory.lifecycle import (
    MemoryLifecycle,
    effective_confidence,
    half_life_for,
)
from loom.core.memory.ontology import (
    DOMAIN_KNOWLEDGE,
    DOMAIN_PROJECT,
    DOMAIN_SELF,
    DOMAIN_USER,
    TEMPORAL_ARCHIVED,
    TEMPORAL_MILESTONE,
    TEMPORAL_RECENT,
)
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.relational import RelationalEntry, RelationalMemory
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.store import SQLiteStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_conn(tmp_path):
    s = SQLiteStore(str(tmp_path / "lifecycle.db"))
    await s.initialize()
    async with s.connect() as db:
        yield db


@pytest_asyncio.fixture
async def memories(db_conn):
    return (
        SemanticMemory(db_conn),
        RelationalMemory(db_conn),
        ProceduralMemory(db_conn),
        EpisodicMemory(db_conn),
    )


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------

class TestHalfLifeTable:
    def test_self_recent_180d(self):
        assert half_life_for(DOMAIN_SELF, TEMPORAL_RECENT) == 180.0

    def test_user_project_knowledge_recent_90d(self):
        # Per N1 concession: knowledge/recent raised to 90 (was 60)
        assert half_life_for(DOMAIN_USER, TEMPORAL_RECENT) == 90.0
        assert half_life_for(DOMAIN_PROJECT, TEMPORAL_RECENT) == 90.0
        assert half_life_for(DOMAIN_KNOWLEDGE, TEMPORAL_RECENT) == 90.0

    def test_milestone_inf_except_knowledge(self):
        assert half_life_for(DOMAIN_SELF, TEMPORAL_MILESTONE) == math.inf
        assert half_life_for(DOMAIN_USER, TEMPORAL_MILESTONE) == math.inf
        assert half_life_for(DOMAIN_PROJECT, TEMPORAL_MILESTONE) == math.inf
        assert half_life_for(DOMAIN_KNOWLEDGE, TEMPORAL_MILESTONE) == 365.0

    def test_archived_short(self):
        assert half_life_for(DOMAIN_SELF, TEMPORAL_ARCHIVED) == 30.0
        assert half_life_for(DOMAIN_KNOWLEDGE, TEMPORAL_ARCHIVED) == 14.0

    def test_unknown_pair_falls_back_90d(self):
        assert half_life_for("ghost", "void") == 90.0


class TestEffectiveConfidence:
    def test_fresh_entry_has_full_confidence(self):
        now = datetime.now(UTC)
        eff = effective_confidence(
            confidence=1.0, updated_at=now, last_accessed_at=None,
            domain=DOMAIN_PROJECT, temporal=TEMPORAL_RECENT,
        )
        assert eff > 0.99

    def test_one_half_life_halves_confidence(self):
        old = datetime.now(UTC) - timedelta(days=90)
        eff = effective_confidence(
            confidence=1.0, updated_at=old, last_accessed_at=None,
            domain=DOMAIN_PROJECT, temporal=TEMPORAL_RECENT,
        )
        # 90d half-life on 90d-old entry → ~0.5
        assert 0.49 < eff < 0.51

    def test_milestone_self_never_decays(self):
        ancient = datetime.now(UTC) - timedelta(days=10_000)
        eff = effective_confidence(
            confidence=0.8, updated_at=ancient, last_accessed_at=None,
            domain=DOMAIN_SELF, temporal=TEMPORAL_MILESTONE,
        )
        assert eff == 0.8  # half-life is inf — no decay applied

    def test_last_accessed_keeps_fact_alive(self):
        old = datetime.now(UTC) - timedelta(days=200)
        recent_access = datetime.now(UTC) - timedelta(days=5)
        eff_with_access = effective_confidence(
            confidence=1.0, updated_at=old, last_accessed_at=recent_access,
            domain=DOMAIN_PROJECT, temporal=TEMPORAL_RECENT,
        )
        eff_without_access = effective_confidence(
            confidence=1.0, updated_at=old, last_accessed_at=None,
            domain=DOMAIN_PROJECT, temporal=TEMPORAL_RECENT,
        )
        assert eff_with_access > eff_without_access
        assert eff_with_access > 0.95  # recent access → ~no decay
        assert eff_without_access < 0.3  # 200d on 90d half-life → much decay

    def test_floor_is_001(self):
        ancient = datetime.now(UTC) - timedelta(days=100_000)
        eff = effective_confidence(
            confidence=1.0, updated_at=ancient, last_accessed_at=None,
            domain=DOMAIN_KNOWLEDGE, temporal=TEMPORAL_ARCHIVED,
        )
        assert eff >= 0.01


# ---------------------------------------------------------------------------
# State transition tests
# ---------------------------------------------------------------------------

class TestSemanticTransitions:
    @pytest.mark.asyncio
    async def test_fresh_recent_row_untouched(self, db_conn, memories):
        semantic, *_ = memories
        await semantic.upsert(SemanticEntry(
            key="fresh", value="just written",
            domain=DOMAIN_PROJECT, temporal=TEMPORAL_RECENT,
        ))
        result = await MemoryLifecycle(db_conn).run()
        assert result.semantic_archived == 0
        assert result.semantic_deleted == 0
        assert (await semantic.get("fresh")).temporal == TEMPORAL_RECENT

    @pytest.mark.asyncio
    async def test_old_recent_row_demoted_to_archived(self, db_conn, memories):
        semantic, *_ = memories
        # Hand-write an entry with a 1000d-old updated_at — well past
        # archive threshold for any (domain, recent) half-life.
        old = (datetime.now(UTC) - timedelta(days=1000)).isoformat()
        await db_conn.execute(
            "INSERT INTO semantic_entries (id, key, value, confidence, source, "
            "metadata, created_at, updated_at, domain, temporal) "
            "VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)",
            ("id1", "stale", "old fact", 1.0, "test", old, old,
             DOMAIN_PROJECT, TEMPORAL_RECENT),
        )
        await db_conn.commit()

        result = await MemoryLifecycle(db_conn).run()
        assert result.semantic_archived == 1
        assert result.semantic_deleted == 0
        got = await semantic.get("stale")
        assert got.temporal == TEMPORAL_ARCHIVED

    @pytest.mark.asyncio
    async def test_old_archived_row_deleted(self, db_conn, memories):
        semantic, *_ = memories
        # Already-archived row that's also too old → DELETE
        old = (datetime.now(UTC) - timedelta(days=1000)).isoformat()
        await db_conn.execute(
            "INSERT INTO semantic_entries (id, key, value, confidence, source, "
            "metadata, created_at, updated_at, domain, temporal) "
            "VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)",
            ("id2", "doomed", "very old", 1.0, "test", old, old,
             DOMAIN_KNOWLEDGE, TEMPORAL_ARCHIVED),
        )
        await db_conn.commit()

        result = await MemoryLifecycle(db_conn).run()
        assert result.semantic_deleted == 1
        assert (await semantic.get("doomed")) is None

    @pytest.mark.asyncio
    async def test_milestone_self_survives_extreme_age(self, db_conn, memories):
        semantic, *_ = memories
        ancient = (datetime.now(UTC) - timedelta(days=10_000)).isoformat()
        await db_conn.execute(
            "INSERT INTO semantic_entries (id, key, value, confidence, source, "
            "metadata, created_at, updated_at, domain, temporal) "
            "VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)",
            ("id3", "core", "agent identity", 1.0, "manual", ancient, ancient,
             DOMAIN_SELF, TEMPORAL_MILESTONE),
        )
        await db_conn.commit()

        result = await MemoryLifecycle(db_conn).run()
        assert result.semantic_archived == 0
        assert result.semantic_deleted == 0
        assert (await semantic.get("core")) is not None

    @pytest.mark.asyncio
    async def test_demote_does_not_double_into_delete_same_cycle(
        self, db_conn, memories,
    ):
        """A row demoted in this cycle must not be deleted in the same pass.
        Otherwise the second-chance archived state is meaningless."""
        semantic, *_ = memories
        old = (datetime.now(UTC) - timedelta(days=1000)).isoformat()
        await db_conn.execute(
            "INSERT INTO semantic_entries (id, key, value, confidence, source, "
            "metadata, created_at, updated_at, domain, temporal) "
            "VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)",
            ("id4", "borderline", "old", 1.0, "test", old, old,
             DOMAIN_PROJECT, TEMPORAL_RECENT),
        )
        await db_conn.commit()

        result = await MemoryLifecycle(db_conn).run()
        # First cycle: row was recent → archived, NOT deleted
        assert result.semantic_archived == 1
        assert result.semantic_deleted == 0
        assert (await semantic.get("borderline")).temporal == TEMPORAL_ARCHIVED


class TestRelationalCoverage:
    """Issue #299 fix: relational decay must not be limited to
    ``source='dreaming'``."""

    @pytest.mark.asyncio
    async def test_non_dreaming_source_is_examined(self, db_conn, memories):
        _, relational, *_ = memories
        old = (datetime.now(UTC) - timedelta(days=1000)).isoformat()
        for sub, src in [("a", "user"), ("b", "agent"), ("c", "skill_eval")]:
            await db_conn.execute(
                "INSERT INTO relational_entries (id, subject, predicate, object, "
                "confidence, source, metadata, created_at, updated_at, "
                "domain, temporal) "
                "VALUES (?, ?, 'rel', 'x', ?, ?, '{}', ?, ?, ?, ?)",
                (f"id_{sub}", sub, 1.0, src, old, old,
                 DOMAIN_PROJECT, TEMPORAL_RECENT),
            )
        await db_conn.commit()

        result = await MemoryLifecycle(db_conn).run()
        # All three should have been demoted to archived, regardless of source
        assert result.relational_examined == 3
        assert result.relational_archived == 3


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_changes_nothing(self, db_conn, memories):
        semantic, *_ = memories
        old = (datetime.now(UTC) - timedelta(days=1000)).isoformat()
        await db_conn.execute(
            "INSERT INTO semantic_entries (id, key, value, confidence, source, "
            "metadata, created_at, updated_at, domain, temporal) "
            "VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)",
            ("id5", "doomed", "old", 1.0, "test", old, old,
             DOMAIN_KNOWLEDGE, TEMPORAL_ARCHIVED),
        )
        await db_conn.commit()

        result = await MemoryLifecycle(db_conn).run(dry_run=True)
        assert result.semantic_deleted == 1  # would delete
        # ...but DB unchanged
        assert (await semantic.get("doomed")) is not None


# ---------------------------------------------------------------------------
# Integration: governance.run_decay_cycle preserves legacy contract
# ---------------------------------------------------------------------------

class TestGovernanceIntegration:
    @pytest.mark.asyncio
    async def test_run_decay_cycle_returns_archived_and_pruned(
        self, db_conn, memories,
    ):
        semantic, relational, procedural, episodic = memories
        old = (datetime.now(UTC) - timedelta(days=1000)).isoformat()
        # one to archive, one already-archived to delete
        await db_conn.execute(
            "INSERT INTO semantic_entries (id, key, value, confidence, source, "
            "metadata, created_at, updated_at, domain, temporal) "
            "VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)",
            ("a", "to_archive", "old", 1.0, "test", old, old,
             DOMAIN_PROJECT, TEMPORAL_RECENT),
        )
        await db_conn.execute(
            "INSERT INTO semantic_entries (id, key, value, confidence, source, "
            "metadata, created_at, updated_at, domain, temporal) "
            "VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)",
            ("b", "to_delete", "very old", 1.0, "test", old, old,
             DOMAIN_KNOWLEDGE, TEMPORAL_ARCHIVED),
        )
        await db_conn.commit()

        gov = MemoryGovernor(
            semantic=semantic, procedural=procedural,
            relational=relational, episodic=episodic, db=db_conn,
        )
        result = await gov.run_decay_cycle()

        assert result.semantic_archived == 1
        # semantic_pruned = archived + deleted (legacy contract)
        assert result.semantic_pruned == 2
        assert result.total_pruned >= 2
        assert result.total_archived == 1
