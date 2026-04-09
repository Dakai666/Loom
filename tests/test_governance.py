"""
Tests for Issue #43 — Advanced Memory Governance.

Covers:
  - Trust-tier classification (TRUST_TIERS + classify_source)
  - ContradictionDetector (detection + auto-resolution)
  - MemoryGovernor (governed_upsert, admission gate, decay cycle)
  - Backward compatibility with existing memory tests
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
import aiosqlite

from loom.core.memory.store import SQLiteStore
from loom.core.memory.semantic import (
    SemanticEntry,
    SemanticMemory,
    TRUST_TIERS,
    classify_source,
)
from loom.core.memory.episodic import EpisodicEntry, EpisodicMemory
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.relational import RelationalMemory
from loom.core.memory.contradiction import (
    ContradictionDetector,
    ConflictType,
    Resolution,
    Contradiction,
    _text_overlap,
)
from loom.core.memory.governance import (
    MemoryGovernor,
    GovernedWriteResult,
    AdmissionResult,
    DecayCycleResult,
    _word_overlap,
)


# ---------------------------------------------------------------------------
# Fixtures — use a fresh temp DB for every test (matches test_memory.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_governance.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def db(store):
    async with store.connect() as conn:
        yield conn


@pytest_asyncio.fixture
async def semantic(db):
    return SemanticMemory(db)


@pytest_asyncio.fixture
async def episodic(db):
    return EpisodicMemory(db)


@pytest_asyncio.fixture
async def procedural(db):
    return ProceduralMemory(db)


@pytest_asyncio.fixture
async def relational(db):
    return RelationalMemory(db)


@pytest_asyncio.fixture
async def governor(db, semantic, procedural, relational, episodic):
    return MemoryGovernor(
        semantic=semantic,
        procedural=procedural,
        relational=relational,
        episodic=episodic,
        db=db,
        config={
            "admission_threshold": 0.5,
            "episodic_ttl_days": 30,
            "semantic_decay_threshold": 0.1,
            "relational_decay_factor": 1.5,
        },
    )


# ===========================================================================
# 1. Trust Tier Classification
# ===========================================================================

class TestTrustTiers:
    """Verify that source strings map to the correct trust tiers."""

    def test_user_explicit_sources(self):
        for src in ("manual", "user"):
            tier, conf = classify_source(src)
            assert tier == "user_explicit"
            assert conf == 1.0

    def test_agent_memorize(self):
        tier, conf = classify_source("memorize")
        assert tier == "agent_memorize"
        assert conf == 0.85

    def test_session_compress(self):
        tier, conf = classify_source("session:abc123")
        assert tier == "session_compress"
        assert conf == 0.8

    def test_session_compress_with_fact(self):
        tier, conf = classify_source("session:abc123:fact:0")
        assert tier == "session_compress"

    def test_counter_factual(self):
        tier, conf = classify_source("counter_factual:sess_id")
        assert tier == "counter_factual"
        assert conf == 0.75

    def test_dreaming(self):
        tier, conf = classify_source("dreaming")
        assert tier == "dreaming"
        assert conf == 0.6

    def test_skill_evolution(self):
        tier, conf = classify_source("skill_evolution")
        assert tier == "skill_evolution"

    def test_agent_inferred(self):
        tier, conf = classify_source("skill_eval:some_id")
        assert tier == "agent_inferred"

    def test_tool_verified(self):
        tier, conf = classify_source("tool:run_bash")
        assert tier == "tool_verified"
        assert conf == 0.9

    def test_unknown_source(self):
        tier, conf = classify_source("random_thing")
        assert tier == "unknown"
        assert conf == 0.5

    def test_none_source(self):
        tier, conf = classify_source(None)
        assert tier == "unknown"

    def test_empty_source(self):
        tier, conf = classify_source("")
        assert tier == "unknown"

    def test_case_insensitive(self):
        tier, _ = classify_source("MANUAL")
        assert tier == "user_explicit"
        tier2, _ = classify_source("Session:ABC")
        assert tier2 == "session_compress"

    def test_external_fetch(self):
        tier, conf = classify_source("fetch:https://example.com")
        assert tier == "external"
        assert conf == 0.5

    def test_external_web(self):
        tier, conf = classify_source("web:search_query")
        assert tier == "external"
        assert conf == 0.5


# ===========================================================================
# 2. Contradiction Detection
# ===========================================================================

class TestContradictionDetector:
    """Test multi-strategy contradiction detection."""

    @pytest.mark.asyncio
    async def test_detect_exact_key_match(self, semantic):
        """Same key, different value → should detect."""
        await semantic.upsert(SemanticEntry(
            key="user:name", value="Alice", source="manual",
        ))

        proposed = SemanticEntry(
            key="user:name", value="Bob", source="session:abc",
        )

        detector = ContradictionDetector(semantic)
        results = await detector.detect(proposed)

        assert len(results) == 1
        assert results[0].conflict_type == ConflictType.KEY_MATCH
        assert results[0].existing.value == "Alice"
        assert results[0].proposed.value == "Bob"

    @pytest.mark.asyncio
    async def test_no_contradiction_same_value(self, semantic):
        """Same key, same value → no contradiction."""
        await semantic.upsert(SemanticEntry(
            key="user:name", value="Alice", source="manual",
        ))

        proposed = SemanticEntry(
            key="user:name", value="Alice", source="session:abc",
        )

        detector = ContradictionDetector(semantic)
        results = await detector.detect(proposed)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_no_contradiction_new_key(self, semantic):
        """New key → no contradiction."""
        proposed = SemanticEntry(
            key="user:email", value="alice@example.com", source="manual",
        )

        detector = ContradictionDetector(semantic)
        results = await detector.detect(proposed)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_detect_prefix_match(self, semantic):
        """Same 3-segment prefix with same depth and different values → conflict."""
        await semantic.upsert(SemanticEntry(
            key="user:preference:theme:main", value="dark mode with blue accents",
            source="manual",
        ))

        proposed = SemanticEntry(
            key="user:preference:theme:alt", value="bright yellow everywhere please",
            source="session:abc",
        )

        detector = ContradictionDetector(semantic)
        results = await detector.detect(proposed)
        # Same 3-segment prefix + same depth → should detect
        assert any(c.conflict_type == ConflictType.KEY_PREFIX for c in results)

    @pytest.mark.asyncio
    async def test_no_prefix_conflict_different_third_segment(self, semantic):
        """Different 3rd segment should NOT flag as prefix conflict."""
        await semantic.upsert(SemanticEntry(
            key="user:preference:theme", value="dark mode with blue accents",
            source="manual",
        ))

        proposed = SemanticEntry(
            key="user:preference:font", value="JetBrains Mono for everything",
            source="session:abc",
        )

        detector = ContradictionDetector(semantic)
        results = await detector.detect(proposed)
        # Different 3rd segment → no conflict
        assert not any(c.conflict_type == ConflictType.KEY_PREFIX for c in results)

    def test_resolve_trust_wins(self):
        """Higher trust tier wins."""
        c = Contradiction(
            existing=SemanticEntry(
                key="k", value="old", source="dreaming",  # trust=0.6
            ),
            proposed=SemanticEntry(
                key="k", value="new", source="manual",  # trust=1.0
            ),
            conflict_type=ConflictType.KEY_MATCH,
        )
        detector = ContradictionDetector(MagicMock())  # semantic not used in resolve
        result = detector.resolve(c)

        assert result.resolution == Resolution.REPLACE
        assert result.winning_entry.value == "new"

    def test_resolve_existing_higher_trust(self):
        """Existing entry has higher trust → KEEP."""
        c = Contradiction(
            existing=SemanticEntry(
                key="k", value="old", source="manual",  # trust=1.0
            ),
            proposed=SemanticEntry(
                key="k", value="new", source="dreaming",  # trust=0.6
            ),
            conflict_type=ConflictType.KEY_MATCH,
        )
        detector = ContradictionDetector(MagicMock())
        result = detector.resolve(c)

        assert result.resolution == Resolution.KEEP
        assert result.winning_entry.value == "old"

    def test_resolve_same_trust_recency(self):
        """Same trust tier → SUPERSEDE (most recent wins)."""
        c = Contradiction(
            existing=SemanticEntry(
                key="k", value="old", source="session:abc",  # trust=0.8
            ),
            proposed=SemanticEntry(
                key="k", value="new", source="session:def",  # trust=0.8
            ),
            conflict_type=ConflictType.KEY_MATCH,
        )
        detector = ContradictionDetector(MagicMock())
        result = detector.resolve(c)

        assert result.resolution == Resolution.SUPERSEDE
        assert result.winning_entry.value == "new"


# ===========================================================================
# 3. Text Overlap Helpers
# ===========================================================================

class TestTextOverlap:
    def test_identical(self):
        assert _text_overlap("hello world", "hello world") == 1.0

    def test_no_overlap(self):
        assert _text_overlap("hello world", "foo bar") == 0.0

    def test_partial_overlap(self):
        score = _text_overlap("hello world foo", "hello world bar")
        assert 0.3 < score < 0.8

    def test_empty_strings(self):
        assert _text_overlap("", "") == 0.0
        assert _text_overlap("hello", "") == 0.0

    def test_word_overlap_function(self):
        assert _word_overlap("hello world", "hello world") == 1.0
        assert _word_overlap("hello world", "foo bar") == 0.0


# ===========================================================================
# 4. MemoryGovernor — Governed Upsert
# ===========================================================================

class TestGovernedUpsert:

    @pytest.mark.asyncio
    async def test_basic_upsert(self, governor, semantic):
        """A simple fact should be written with trust-adjusted confidence."""
        entry = SemanticEntry(
            key="project:name", value="Loom", source="memorize",
        )
        result = await governor.governed_upsert(entry)

        assert result.written is True
        assert result.trust_tier == "agent_memorize"
        assert result.contradictions_found == 0

        # Verify it was actually written
        stored = await semantic.get("project:name")
        assert stored is not None
        assert stored.value == "Loom"

    @pytest.mark.asyncio
    async def test_upsert_adjusts_confidence_floor(self, governor, semantic):
        """Low confidence should be raised to trust tier default."""
        entry = SemanticEntry(
            key="fact:x", value="something", confidence=0.1,
            source="memorize",  # agent_memorize → trust=0.85 → floor=0.85
        )
        result = await governor.governed_upsert(entry)

        assert result.written is True
        assert result.adjusted_confidence >= 0.85

    @pytest.mark.asyncio
    async def test_upsert_with_contradiction_replace(self, governor, semantic):
        """Higher trust proposed entry should replace lower trust existing."""
        # Write a low-trust entry first
        await semantic.upsert(SemanticEntry(
            key="user:color", value="blue", source="dreaming",
        ))

        # Now write a high-trust entry via governor
        entry = SemanticEntry(
            key="user:color", value="red", source="memorize",
        )
        result = await governor.governed_upsert(entry)

        assert result.written is True
        assert result.contradictions_found == 1
        assert result.resolution in ("replace", "replaced", "supersede", "superseded")

        stored = await semantic.get("user:color")
        assert stored.value == "red"

    @pytest.mark.asyncio
    async def test_upsert_with_contradiction_keep(self, governor, semantic):
        """Lower trust proposed entry should be dropped when existing has higher trust."""
        # Write a high-trust entry first
        await semantic.upsert(SemanticEntry(
            key="user:color", value="blue", source="memorize",
        ))

        # Now try to write a low-trust entry via governor
        entry = SemanticEntry(
            key="user:color", value="red", source="dreaming",
        )
        result = await governor.governed_upsert(entry)

        assert result.written is False
        assert result.contradictions_found == 1
        assert result.resolution == "kept"

        # Original should be preserved
        stored = await semantic.get("user:color")
        assert stored.value == "blue"

    @pytest.mark.asyncio
    async def test_log_governance_warns_once_then_debug(self, caplog):
        db = MagicMock()
        db.execute = AsyncMock(side_effect=RuntimeError("audit write failed"))
        db.commit = AsyncMock()

        governor = MemoryGovernor(
            semantic=MagicMock(),
            procedural=MagicMock(),
            relational=MagicMock(),
            episodic=MagicMock(),
            db=db,
            config={},
        )

        with caplog.at_level(logging.DEBUG, logger="loom.core.memory.governance"):
            await governor._log_governance("governance:test", "note", {})
            await governor._log_governance("governance:test", "note", {})

        warnings = [
            record for record in caplog.records
            if record.levelno == logging.WARNING
            and "Governance audit log write failed" in record.message
        ]
        debugs = [
            record for record in caplog.records
            if record.levelno == logging.DEBUG
            and "Governance audit log write failed" in record.message
        ]

        assert len(warnings) == 1
        assert len(debugs) == 1


# ===========================================================================
# 5. MemoryGovernor — Admission Gate
# ===========================================================================

class TestAdmissionGate:

    @pytest.mark.asyncio
    async def test_admits_novel_fact(self, governor):
        """A substantial, novel fact should be admitted."""
        results = await governor.evaluate_admission(
            ["User prefers TypeScript over JavaScript for backend services"],
            source="session:test",
        )
        assert len(results) == 1
        assert results[0].admitted is True
        assert results[0].score >= 0.5

    @pytest.mark.asyncio
    async def test_rejects_too_short(self, governor):
        """Very short facts should be rejected."""
        results = await governor.evaluate_admission(
            ["ok"],
            source="session:test",
        )
        assert len(results) == 1
        assert results[0].admitted is False
        assert results[0].reason == "too_short"

    @pytest.mark.asyncio
    async def test_rejects_duplicate(self, governor, semantic):
        """Facts that duplicate existing memory should be rejected."""
        # Write an existing fact
        await semantic.upsert(SemanticEntry(
            key="fact:1", value="user prefers dark mode themes",
            source="session:old",
        ))

        results = await governor.evaluate_admission(
            ["user prefers dark mode themes"],
            source="session:new",
        )
        assert len(results) == 1
        assert results[0].admitted is False
        assert results[0].reason == "duplicate"

    @pytest.mark.asyncio
    async def test_multiple_facts_mixed(self, governor):
        """Mix of admissible and non-admissible facts."""
        results = await governor.evaluate_admission(
            [
                "Loom uses SQLite for memory storage with FTS5 indexing",
                "ok",
                "The system architecture follows a seven-layer design with harness at the core",
            ],
            source="session:test",
        )
        assert len(results) == 3
        # First and third should be admitted, second rejected
        assert results[0].admitted is True
        assert results[1].admitted is False
        assert results[2].admitted is True

    @pytest.mark.asyncio
    async def test_threshold_respected(self, db, semantic, procedural, relational, episodic):
        """Facts at different quality levels should respect the threshold."""
        strict_governor = MemoryGovernor(
            semantic=semantic,
            procedural=procedural,
            relational=relational,
            episodic=episodic,
            db=db,
            config={"admission_threshold": 0.8},  # strict
        )

        results = await strict_governor.evaluate_admission(
            ["A moderate quality fact about something"],
            source="session:test",
        )
        # With strict threshold, borderline facts might be rejected
        assert len(results) == 1


# ===========================================================================
# 6. MemoryGovernor — Decay Cycle
# ===========================================================================

class TestDecayCycle:

    @pytest.mark.asyncio
    async def test_episodic_ttl_prunes_old(self, governor, db):
        """Episodic entries older than TTL should be pruned."""
        old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        await db.execute(
            "INSERT INTO episodic_entries (id, session_id, event_type, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), "old-session", "user", "old message", old_date),
        )
        await db.commit()

        result = await governor.run_decay_cycle()
        assert result.episodic_pruned >= 1

    @pytest.mark.asyncio
    async def test_episodic_ttl_keeps_recent(self, governor, db):
        """Recent episodic entries should survive the decay cycle."""
        recent_date = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO episodic_entries (id, session_id, event_type, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), "new-session", "user", "recent message", recent_date),
        )
        await db.commit()

        result = await governor.run_decay_cycle()
        assert result.episodic_pruned == 0

    @pytest.mark.asyncio
    async def test_semantic_decay_prunes_ancient(self, governor, semantic):
        """Semantic entries with very old update timestamps should be pruned."""
        ancient_date = (datetime.now(UTC) - timedelta(days=365)).isoformat()
        await semantic._db.execute(
            """INSERT INTO semantic_entries
                (id, key, value, confidence, source, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), "ancient:fact", "very old info", 0.3, "unknown",
             "{}", ancient_date, ancient_date),
        )
        await semantic._db.commit()

        result = await governor.run_decay_cycle()
        assert result.semantic_pruned >= 1

    @pytest.mark.asyncio
    async def test_decay_cycle_returns_correct_totals(self, governor):
        """Even with nothing to prune, the result shape should be valid."""
        result = await governor.run_decay_cycle()

        assert isinstance(result, DecayCycleResult)
        assert result.semantic_pruned >= 0
        assert result.episodic_pruned >= 0
        assert result.relational_pruned >= 0
        assert result.total_pruned >= 0


# ===========================================================================
# 7. Integration: compress_session with Admission Gate
# ===========================================================================

class TestCompressWithGovernance:

    @pytest.mark.asyncio
    async def test_compress_filters_via_admission(
        self, db, semantic, episodic, governor
    ):
        """compress_session with governor should pass facts through admission."""
        from loom.platform.cli.main import compress_session

        # Write some episodic entries
        session_id = "test-session"
        for i in range(3):
            await episodic.write(EpisodicEntry(
                session_id=session_id,
                event_type="user",
                content=f"User said something interesting about topic {i} "
                        f"that is long enough to be a useful fact",
            ))

        # Mock the router
        mock_router = MagicMock()
        mock_response = MagicMock()
        mock_response.text = (
            "FACT: User prefers working with TypeScript for backend development\n"
            "FACT: ok\n"
            "FACT: The project uses SQLite for persistence with FTS5 for search\n"
        )
        mock_router.chat = AsyncMock(return_value=mock_response)

        count = await compress_session(
            session_id, episodic, semantic, mock_router, "test-model",
            governor=governor,
        )

        # "ok" should be filtered out by admission, so 2 facts instead of 3
        assert count == 2


# ===========================================================================
# 8. Backward Compatibility
# ===========================================================================

class TestBackwardCompat:

    @pytest.mark.asyncio
    async def test_semantic_upsert_still_works_directly(self, semantic):
        """Direct semantic.upsert() bypassing governance should still work."""
        entry = SemanticEntry(
            key="test:direct", value="direct write", source="agent",
        )
        conflicted = await semantic.upsert(entry)
        assert conflicted is False

        stored = await semantic.get("test:direct")
        assert stored is not None
        assert stored.value == "direct write"

    @pytest.mark.asyncio
    async def test_compress_without_governor(self, db, semantic, episodic):
        """compress_session without governor should work as before."""
        from loom.platform.cli.main import compress_session

        session_id = "compat-session"
        await episodic.write(EpisodicEntry(
            session_id=session_id,
            event_type="user",
            content="A test message for compatibility",
        ))

        mock_router = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "FACT: User sent a test message for compatibility check\n"
        mock_router.chat = AsyncMock(return_value=mock_response)

        count = await compress_session(
            session_id, episodic, semantic, mock_router, "test-model",
        )
        assert count == 1

    @pytest.mark.asyncio
    async def test_existing_entries_readable(self, semantic):
        """Entries written before governance should still be retrievable."""
        # Simulate a pre-governance entry with source="agent"
        entry = SemanticEntry(
            key="old:fact", value="written before governance",
            source="agent", confidence=0.8,
        )
        await semantic.upsert(entry)

        stored = await semantic.get("old:fact")
        assert stored is not None
        assert stored.value == "written before governance"

        # Classify its source — should be "unknown"
        tier, _ = classify_source(stored.source)
        assert tier == "unknown"


# ===========================================================================
# 9. Audit Log Integration
# ===========================================================================

class TestAuditLogIntegration:

    @pytest.mark.asyncio
    async def test_governance_events_in_audit_log(self, governor, semantic, db):
        """Governance events should appear in audit_log with governance: prefix."""
        # Trigger a contradiction to generate an audit log entry
        await semantic.upsert(SemanticEntry(
            key="test:audit", value="original", source="dreaming",
        ))

        await governor.governed_upsert(SemanticEntry(
            key="test:audit", value="updated", source="memorize",
        ))

        cursor = await db.execute(
            "SELECT tool_name, trust_level, details FROM audit_log "
            "WHERE tool_name LIKE 'governance:%'"
        )
        rows = await cursor.fetchall()
        assert len(rows) >= 1

        # Verify the details field is valid JSON
        for _, trust_level, details_json in rows:
            assert trust_level == "GOVERNANCE"
            details = json.loads(details_json)
            assert isinstance(details, dict)
