"""
Tests for Memory Ontology v0.1 (issue #281):
  - ontology.normalize_domain / normalize_temporal — invalid → default
  - classifier.infer_domain — prefix-rule matching
  - SemanticEntry / RelationalEntry — dataclass normalization
  - governed_upsert — auto-classify when domain is the safe default
  - MemorySearch.recall — domain/temporal filter
  - SemanticMemory.mark_accessed — last_accessed_at update
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from loom.core.memory.classifier import infer_domain
from loom.core.memory.governance import MemoryGovernor
from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.ontology import (
    DEFAULT_DOMAIN,
    DEFAULT_TEMPORAL,
    DOMAIN_KNOWLEDGE,
    DOMAIN_PROJECT,
    DOMAIN_SELF,
    DOMAIN_USER,
    DOMAINS,
    TEMPORAL_MILESTONE,
    TEMPORAL_RECENT,
    TEMPORALS,
    normalize_domain,
    normalize_temporal,
)
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.relational import RelationalEntry, RelationalMemory
from loom.core.memory.search import MemorySearch
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.store import SQLiteStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def store(tmp_path):
    s = SQLiteStore(str(tmp_path / "ontology.db"))
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def db_conn(store):
    async with store.connect() as db:
        yield db


@pytest_asyncio.fixture
async def memories(db_conn):
    semantic = SemanticMemory(db_conn)
    relational = RelationalMemory(db_conn)
    procedural = ProceduralMemory(db_conn)
    episodic = EpisodicMemory(db_conn)
    return semantic, relational, procedural, episodic


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------

class TestOntologyNormalizers:
    def test_domains_are_closed_set(self):
        assert DOMAINS == {DOMAIN_SELF, DOMAIN_USER, DOMAIN_PROJECT, DOMAIN_KNOWLEDGE}

    def test_temporals_are_closed_set(self):
        assert "ephemeral" in TEMPORALS
        assert "recent" in TEMPORALS
        assert "milestone" in TEMPORALS
        assert "archived" in TEMPORALS
        assert len(TEMPORALS) == 4

    @pytest.mark.parametrize("good", list(DOMAINS))
    def test_normalize_domain_passes_known_values(self, good):
        assert normalize_domain(good) == good

    @pytest.mark.parametrize("bad", ["", None, "weird", "SELF", "Knowledge"])
    def test_normalize_domain_falls_back_for_unknown(self, bad):
        assert normalize_domain(bad) == DEFAULT_DOMAIN

    @pytest.mark.parametrize("good", list(TEMPORALS))
    def test_normalize_temporal_passes_known_values(self, good):
        assert normalize_temporal(good) == good

    @pytest.mark.parametrize("bad", ["", None, "soon", "RECENT"])
    def test_normalize_temporal_falls_back_for_unknown(self, bad):
        assert normalize_temporal(bad) == DEFAULT_TEMPORAL


class TestClassifier:
    @pytest.mark.parametrize("key,expected", [
        ("loom:identity:establishment_date", DOMAIN_SELF),
        ("user:prefers:concise", DOMAIN_USER),
        ("project:loom:db_schema", DOMAIN_PROJECT),
        ("loom:config:model_default", DOMAIN_PROJECT),
        ("knowledge:gitnexus:cypher_syntax", DOMAIN_KNOWLEDGE),
        ("skill:code_weaver:v1", DOMAIN_KNOWLEDGE),
        ("random_key_with_no_namespace", DOMAIN_KNOWLEDGE),
        ("", DOMAIN_KNOWLEDGE),
        (None, DOMAIN_KNOWLEDGE),
    ])
    def test_infer_domain_known_prefixes(self, key, expected):
        assert infer_domain(key) == expected

    def test_infer_domain_is_case_insensitive(self):
        assert infer_domain("USER:prefers:bar") == DOMAIN_USER
        assert infer_domain("Project:Loom:Foo") == DOMAIN_PROJECT


class TestDataclassNormalization:
    def test_semantic_entry_normalizes_invalid_domain(self):
        e = SemanticEntry(key="k", value="v", domain="nonsense")
        assert e.domain == DEFAULT_DOMAIN

    def test_semantic_entry_preserves_valid_domain(self):
        e = SemanticEntry(key="k", value="v", domain=DOMAIN_SELF)
        assert e.domain == DOMAIN_SELF

    def test_semantic_entry_normalizes_invalid_temporal(self):
        e = SemanticEntry(key="k", value="v", temporal="bogus")
        assert e.temporal == DEFAULT_TEMPORAL

    def test_relational_entry_normalizes(self):
        e = RelationalEntry(
            subject="user", predicate="prefers", object="x",
            domain="???", temporal="?",
        )
        assert e.domain == DEFAULT_DOMAIN
        assert e.temporal == DEFAULT_TEMPORAL


# ---------------------------------------------------------------------------
# Round-trip tests — schema actually persists and returns the new fields
# ---------------------------------------------------------------------------

class TestSemanticPersistence:
    @pytest.mark.asyncio
    async def test_upsert_persists_domain_and_temporal(self, memories):
        semantic, *_ = memories
        await semantic.upsert(SemanticEntry(
            key="loom:identity:foo", value="hello",
            domain=DOMAIN_SELF, temporal=TEMPORAL_MILESTONE,
        ))
        got = await semantic.get("loom:identity:foo")
        assert got is not None
        assert got.domain == DOMAIN_SELF
        assert got.temporal == TEMPORAL_MILESTONE

    @pytest.mark.asyncio
    async def test_upsert_uses_defaults_when_omitted(self, memories):
        semantic, *_ = memories
        await semantic.upsert(SemanticEntry(key="k", value="v"))
        got = await semantic.get("k")
        assert got.domain == DEFAULT_DOMAIN
        assert got.temporal == DEFAULT_TEMPORAL
        assert got.last_accessed_at is None

    @pytest.mark.asyncio
    async def test_mark_accessed_updates_last_accessed_at(self, memories):
        semantic, *_ = memories
        await semantic.upsert(SemanticEntry(key="k", value="v"))
        assert (await semantic.get("k")).last_accessed_at is None
        await semantic.mark_accessed(["k"])
        got = await semantic.get("k")
        assert got.last_accessed_at is not None

    @pytest.mark.asyncio
    async def test_mark_accessed_empty_list_is_noop(self, memories):
        semantic, *_ = memories
        await semantic.mark_accessed([])  # must not raise


class TestRelationalPersistence:
    @pytest.mark.asyncio
    async def test_relational_round_trip(self, memories):
        _, relational, *_ = memories
        await relational.upsert(RelationalEntry(
            subject="user", predicate="prefers", object="brevity",
            domain=DOMAIN_USER, temporal=TEMPORAL_MILESTONE,
        ))
        got = await relational.get("user", "prefers")
        assert got.domain == DOMAIN_USER
        assert got.temporal == TEMPORAL_MILESTONE


# ---------------------------------------------------------------------------
# Governance: classifier upgrades default domain when key has a known prefix
# ---------------------------------------------------------------------------

class TestGovernorClassifier:
    @pytest.mark.asyncio
    async def test_governor_upgrades_default_domain_via_classifier(
        self, db_conn, memories
    ):
        semantic, relational, procedural, episodic = memories
        gov = MemoryGovernor(
            semantic=semantic, procedural=procedural,
            relational=relational, episodic=episodic, db=db_conn,
        )
        # Default domain on the entry, but key has a `user:` prefix —
        # governor should rewrite to DOMAIN_USER before persisting.
        entry = SemanticEntry(
            key="user:prefers:concise",
            value="user wants short responses",
            source="memorize",
        )
        assert entry.domain == DEFAULT_DOMAIN
        await gov.governed_upsert(entry)

        got = await semantic.get("user:prefers:concise")
        assert got.domain == DOMAIN_USER

    @pytest.mark.asyncio
    async def test_governor_preserves_explicit_non_default_domain(
        self, db_conn, memories
    ):
        semantic, relational, procedural, episodic = memories
        gov = MemoryGovernor(
            semantic=semantic, procedural=procedural,
            relational=relational, episodic=episodic, db=db_conn,
        )
        # Caller explicitly set DOMAIN_SELF — classifier must not override
        # even though key prefix says ``user:``.
        entry = SemanticEntry(
            key="user:weird_key",
            value="agent thinks this is about itself",
            source="memorize",
            domain=DOMAIN_SELF,
        )
        await gov.governed_upsert(entry)
        got = await semantic.get("user:weird_key")
        assert got.domain == DOMAIN_SELF


# ---------------------------------------------------------------------------
# Recall: domain / temporal filter and last_accessed_at side-effect
# ---------------------------------------------------------------------------

class TestRecallAxisFilter:
    @pytest_asyncio.fixture
    async def populated(self, memories):
        semantic, _, procedural, _ = memories
        for key, value, domain, temporal in [
            ("user:prefers", "concise responses", DOMAIN_USER, TEMPORAL_RECENT),
            ("project:db", "uses sqlite wal", DOMAIN_PROJECT, TEMPORAL_RECENT),
            ("project:milestone", "release v1 launched", DOMAIN_PROJECT, TEMPORAL_MILESTONE),
            ("knowledge:tool", "gitnexus indexes code", DOMAIN_KNOWLEDGE, TEMPORAL_RECENT),
        ]:
            await semantic.upsert(SemanticEntry(
                key=key, value=value, domain=domain, temporal=temporal,
            ))
        return MemorySearch(semantic=semantic, procedural=procedural)

    @pytest.mark.asyncio
    async def test_recall_filters_by_domain(self, populated):
        # Query word "uses" appears in the project entry; without filter
        # we'd also pick up unrelated results. With domain=user, that
        # entry must not appear because it's project-domain.
        results = await populated.recall("responses", domain=DOMAIN_USER, limit=5)
        assert all(r.type != "semantic" or r.key.startswith("user:") for r in results)

    @pytest.mark.asyncio
    async def test_recall_filters_by_temporal_milestone(self, populated):
        results = await populated.recall(
            "release", temporal=TEMPORAL_MILESTONE, type="semantic", limit=5,
        )
        assert results, "milestone entry should match"
        assert all(r.key == "project:milestone" for r in results)

    @pytest.mark.asyncio
    async def test_recall_marks_accessed_on_hit(self, populated, memories):
        semantic, *_ = memories
        before = await semantic.get("user:prefers")
        assert before.last_accessed_at is None
        await populated.recall("concise", type="semantic", limit=5)
        after = await semantic.get("user:prefers")
        assert after.last_accessed_at is not None
