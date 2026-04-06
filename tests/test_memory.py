"""
Tests for the Memory Layer:
  - SQLiteStore: initialization, schema creation
  - EpisodicMemory: write, read_session, count
  - SemanticMemory: upsert, get, search, list_recent, upsert-update
  - ProceduralMemory / SkillGenome: upsert, get, list_active, record_outcome,
                                     deprecation threshold
"""

import asyncio
import pytest
import pytest_asyncio
import tempfile
import os
from pathlib import Path

from loom.core.memory.store import SQLiteStore
from loom.core.memory.episodic import EpisodicEntry, EpisodicMemory
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.procedural import SkillGenome, ProceduralMemory


# ---------------------------------------------------------------------------
# Fixtures — use a fresh temp DB for every test
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_memory.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def db_conn(store):
    async with store.connect() as db:
        yield db


# ---------------------------------------------------------------------------
# SQLiteStore
# ---------------------------------------------------------------------------

class TestSQLiteStore:
    @pytest.mark.asyncio
    async def test_initialize_creates_file(self, tmp_db):
        s = SQLiteStore(tmp_db)
        await s.initialize()
        assert Path(tmp_db).exists()

    @pytest.mark.asyncio
    async def test_double_initialize_is_idempotent(self, tmp_db):
        s = SQLiteStore(tmp_db)
        await s.initialize()
        await s.initialize()   # should not raise

    @pytest.mark.asyncio
    async def test_connect_returns_working_connection(self, store):
        async with store.connect() as db:
            cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in await cur.fetchall()}
        expected = {"episodic_entries", "semantic_entries",
                    "skill_genomes", "audit_log"}
        assert expected.issubset(tables)


# ---------------------------------------------------------------------------
# EpisodicMemory
# ---------------------------------------------------------------------------

class TestEpisodicMemory:
    @pytest.mark.asyncio
    async def test_write_and_read_session(self, db_conn):
        em = EpisodicMemory(db_conn)
        entry = EpisodicEntry(
            session_id="s1",
            event_type="tool_result",
            content="read_file ok → 512 chars",
            metadata={"tool_name": "read_file", "duration_ms": 12.3},
        )
        await em.write(entry)
        results = await em.read_session("s1")
        assert len(results) == 1
        r = results[0]
        assert r.event_type == "tool_result"
        assert r.content == "read_file ok → 512 chars"
        assert r.metadata["tool_name"] == "read_file"

    @pytest.mark.asyncio
    async def test_read_session_isolation(self, db_conn):
        em = EpisodicMemory(db_conn)
        await em.write(EpisodicEntry(session_id="s1", event_type="message", content="hello"))
        await em.write(EpisodicEntry(session_id="s2", event_type="message", content="world"))
        s1_entries = await em.read_session("s1")
        assert len(s1_entries) == 1
        assert s1_entries[0].content == "hello"

    @pytest.mark.asyncio
    async def test_read_session_ordered_by_created_at(self, db_conn):
        em = EpisodicMemory(db_conn)
        for i in range(5):
            await em.write(EpisodicEntry(
                session_id="s3", event_type="tool_result",
                content=f"step {i}",
            ))
        entries = await em.read_session("s3")
        contents = [e.content for e in entries]
        assert contents == [f"step {i}" for i in range(5)]

    @pytest.mark.asyncio
    async def test_count_session(self, db_conn):
        em = EpisodicMemory(db_conn)
        assert await em.count_session("empty") == 0
        for _ in range(3):
            await em.write(EpisodicEntry(
                session_id="s4", event_type="message", content="x"
            ))
        assert await em.count_session("s4") == 3

    @pytest.mark.asyncio
    async def test_metadata_round_trips(self, db_conn):
        em = EpisodicMemory(db_conn)
        meta = {"nested": {"key": "value"}, "list": [1, 2, 3]}
        await em.write(EpisodicEntry(
            session_id="s5", event_type="system",
            content="test", metadata=meta,
        ))
        entries = await em.read_session("s5")
        assert entries[0].metadata == meta


# ---------------------------------------------------------------------------
# SemanticMemory
# ---------------------------------------------------------------------------

class TestSemanticMemory:
    @pytest.mark.asyncio
    async def test_upsert_and_get(self, db_conn):
        sm = SemanticMemory(db_conn)
        entry = SemanticEntry(key="project:lang", value="Python 3.14",
                              confidence=0.95, source="session:abc")
        await sm.upsert(entry)
        fetched = await sm.get("project:lang")
        assert fetched is not None
        assert fetched.value == "Python 3.14"
        assert fetched.confidence == 0.95
        assert fetched.source == "session:abc"

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, db_conn):
        sm = SemanticMemory(db_conn)
        assert await sm.get("does_not_exist") is None

    @pytest.mark.asyncio
    async def test_upsert_updates_existing_by_key(self, db_conn):
        sm = SemanticMemory(db_conn)
        await sm.upsert(SemanticEntry(key="k", value="old", confidence=0.5))
        await sm.upsert(SemanticEntry(key="k", value="new", confidence=0.9))
        fetched = await sm.get("k")
        assert fetched.value == "new"
        assert fetched.confidence == 0.9

    @pytest.mark.asyncio
    async def test_search_finds_substring(self, db_conn):
        sm = SemanticMemory(db_conn)
        await sm.upsert(SemanticEntry(key="f1", value="The project uses FastAPI"))
        await sm.upsert(SemanticEntry(key="f2", value="Database is PostgreSQL"))
        await sm.upsert(SemanticEntry(key="f3", value="FastAPI docs auto-generated"))

        results = await sm.search("FastAPI")
        assert len(results) == 2
        keys = {r.key for r in results}
        assert keys == {"f1", "f3"}

    @pytest.mark.asyncio
    async def test_search_empty_when_no_match(self, db_conn):
        sm = SemanticMemory(db_conn)
        await sm.upsert(SemanticEntry(key="x", value="something unrelated"))
        results = await sm.search("nonexistent_term_xyz")
        assert results == []

    @pytest.mark.asyncio
    async def test_list_recent_limit(self, db_conn):
        sm = SemanticMemory(db_conn)
        for i in range(10):
            await sm.upsert(SemanticEntry(key=f"key:{i}", value=f"fact {i}"))
        results = await sm.list_recent(limit=5)
        assert len(results) == 5

    @pytest.mark.asyncio
    async def test_metadata_round_trips(self, db_conn):
        sm = SemanticMemory(db_conn)
        meta = {"tags": ["python", "framework"], "priority": 1}
        await sm.upsert(SemanticEntry(key="m1", value="test", metadata=meta))
        fetched = await sm.get("m1")
        assert fetched.metadata == meta


# ---------------------------------------------------------------------------
# ProceduralMemory / SkillGenome
# ---------------------------------------------------------------------------

class TestSkillGenome:
    def test_is_deprecated_false_when_above_threshold(self):
        skill = SkillGenome(name="s", body="do something",
                            confidence=0.8, deprecation_threshold=0.3)
        assert skill.is_deprecated is False

    def test_is_deprecated_true_when_below_threshold(self):
        # Needs usage_count >= MIN_SAMPLES_BEFORE_DEPRECATION to be eligible
        skill = SkillGenome(name="s", body="do something",
                            confidence=0.2, deprecation_threshold=0.3,
                            usage_count=3)
        assert skill.is_deprecated is True

    def test_is_deprecated_true_when_equal_to_threshold(self):
        skill = SkillGenome(name="s", body="x", confidence=0.3,
                            deprecation_threshold=0.3, usage_count=3)
        assert skill.is_deprecated is True

    def test_is_deprecated_false_before_min_samples(self):
        # Even below threshold, a skill with few observations is not deprecated
        skill = SkillGenome(name="s", body="x", confidence=0.1,
                            deprecation_threshold=0.3, usage_count=2)
        assert skill.is_deprecated is False

    def test_record_outcome_increments_usage_count(self):
        skill = SkillGenome(name="s", body="x")
        skill.record_outcome(True)
        skill.record_outcome(False)
        assert skill.usage_count == 2

    def test_record_outcome_success_keeps_high_confidence(self):
        skill = SkillGenome(name="s", body="x", confidence=1.0, success_rate=1.0)
        for _ in range(10):
            skill.record_outcome(True)
        assert skill.confidence > 0.9

    def test_record_outcome_failures_lower_confidence(self):
        skill = SkillGenome(name="s", body="x", confidence=1.0, success_rate=1.0)
        for _ in range(30):
            skill.record_outcome(False)
        assert skill.confidence < 0.5

    def test_record_outcome_updates_timestamp(self):
        skill = SkillGenome(name="s", body="x")
        ts_before = skill.updated_at
        skill.record_outcome(True)
        assert skill.updated_at >= ts_before


class TestProceduralMemory:
    @pytest.mark.asyncio
    async def test_upsert_and_get(self, db_conn):
        pm = ProceduralMemory(db_conn)
        skill = SkillGenome(
            name="extract_function",
            body="When a function exceeds 30 lines...",
            tags=["refactor", "python"],
            confidence=0.9,
        )
        await pm.upsert(skill)
        fetched = await pm.get("extract_function")
        assert fetched is not None
        assert fetched.body == "When a function exceeds 30 lines..."
        assert fetched.tags == ["refactor", "python"]
        assert fetched.confidence == 0.9

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, db_conn):
        pm = ProceduralMemory(db_conn)
        assert await pm.get("no_such_skill") is None

    @pytest.mark.asyncio
    async def test_upsert_updates_existing(self, db_conn):
        pm = ProceduralMemory(db_conn)
        await pm.upsert(SkillGenome(name="sk", body="v1", confidence=0.7))
        await pm.upsert(SkillGenome(name="sk", body="v2", confidence=0.9))
        fetched = await pm.get("sk")
        assert fetched.body == "v2"
        assert fetched.confidence == 0.9

    @pytest.mark.asyncio
    async def test_list_active_excludes_deprecated(self, db_conn):
        pm = ProceduralMemory(db_conn)
        await pm.upsert(SkillGenome(
            name="good_skill", body="x",
            confidence=0.8, deprecation_threshold=0.3,
        ))
        # usage_count >= MIN_SAMPLES_BEFORE_DEPRECATION so it is truly deprecated
        await pm.upsert(SkillGenome(
            name="bad_skill", body="y",
            confidence=0.1, deprecation_threshold=0.3,
            usage_count=3,
        ))
        active = await pm.list_active()
        names = {s.name for s in active}
        assert "good_skill" in names
        assert "bad_skill" not in names

    @pytest.mark.asyncio
    async def test_list_active_ordered_by_confidence_desc(self, db_conn):
        pm = ProceduralMemory(db_conn)
        for i, conf in enumerate([0.6, 0.9, 0.75]):
            await pm.upsert(SkillGenome(
                name=f"skill_{i}", body="x",
                confidence=conf, deprecation_threshold=0.3,
            ))
        active = await pm.list_active()
        confidences = [s.confidence for s in active]
        assert confidences == sorted(confidences, reverse=True)

    @pytest.mark.asyncio
    async def test_tags_round_trip(self, db_conn):
        pm = ProceduralMemory(db_conn)
        tags = ["python", "refactor", "clean-code"]
        await pm.upsert(SkillGenome(name="tagged", body="x", tags=tags))
        fetched = await pm.get("tagged")
        assert fetched.tags == tags
