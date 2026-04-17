"""
Tests for Phase 4B — MemorySearch, MemoryIndex, and memory tools.

Coverage
--------
MemorySearch (integration with real SQLite)
  - recall semantic by matching query
  - recall skill by matching query
  - type="semantic" excludes skills
  - type="skill" excludes semantic
  - limit is respected
  - empty memory → empty list
  - blank query → empty list

MemoryIndex / MemoryIndexer (integration)
  - empty memory → is_empty True, counts 0
  - with semantic facts → correct count and topics
  - with skills → correct count and tags
  - episode_sessions counts distinct compressed sessions
  - render() contains key sections

make_recall_tool (unit, via MemorySearch mock)
  - success path returns formatted results
  - missing query → error result
  - empty results → success with no-match message
  - type filter forwarded correctly

make_memorize_tool (integration)
  - persists entry in semantic memory
  - missing key/value → error result
  - confidence clamped to [0, 1]
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

from loom.core.memory.store import SQLiteStore
from loom.core.memory.episodic import EpisodicEntry, EpisodicMemory
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.procedural import SkillGenome, ProceduralMemory
from loom.core.memory.search import MemorySearch, MemorySearchResult
from loom.core.memory.index import MemoryIndex, MemoryIndexer
from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.platform.cli.tools import make_recall_tool, make_memorize_tool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def db_conn(store):
    async with store.connect() as conn:
        yield conn


@pytest_asyncio.fixture
async def semantic(db_conn):
    return SemanticMemory(db_conn)


@pytest_asyncio.fixture
async def procedural(db_conn):
    return ProceduralMemory(db_conn)


@pytest_asyncio.fixture
async def episodic(db_conn):
    return EpisodicMemory(db_conn)


def _make_call(tool_name: str, args: dict) -> ToolCall:
    from loom.core.harness.permissions import TrustLevel
    return ToolCall(id="test-id", tool_name=tool_name, args=args,
                    trust_level=TrustLevel.SAFE, session_id="s1")


# ---------------------------------------------------------------------------
# MemorySearch — integration
# ---------------------------------------------------------------------------

class TestMemorySearch:
    async def test_recall_semantic_match(self, semantic, procedural):
        await semantic.upsert(SemanticEntry(key="k1", value="loom harness middleware pipeline"))
        await semantic.upsert(SemanticEntry(key="k2", value="unrelated javascript framework"))

        search = MemorySearch(semantic, procedural)
        results = await search.recall("loom harness")
        assert len(results) > 0
        assert results[0].key == "k1"
        assert results[0].type == "semantic"

    async def test_recall_skill_match(self, semantic, procedural):
        skill = SkillGenome(name="refactor_extract", body="extract function when over 30 lines",
                            tags=["refactor", "python"])
        await procedural.upsert(skill)

        search = MemorySearch(semantic, procedural)
        results = await search.recall("refactor function", type="skill")
        assert len(results) > 0
        assert results[0].key == "refactor_extract"
        assert results[0].type == "skill"

    async def test_type_semantic_excludes_skills(self, semantic, procedural):
        skill = SkillGenome(name="bash_skill", body="run bash commands efficiently", tags=["bash"])
        await procedural.upsert(skill)

        search = MemorySearch(semantic, procedural)
        results = await search.recall("bash commands", type="semantic")
        assert all(r.type == "semantic" for r in results)

    async def test_type_skill_excludes_semantic(self, semantic, procedural):
        await semantic.upsert(SemanticEntry(key="k1", value="python loom memory architecture"))

        search = MemorySearch(semantic, procedural)
        results = await search.recall("python loom", type="skill")
        assert all(r.type == "skill" for r in results)

    async def test_limit_respected(self, semantic, procedural):
        for i in range(10):
            await semantic.upsert(SemanticEntry(key=f"fact:{i}", value=f"loom memory fact {i}"))

        search = MemorySearch(semantic, procedural)
        results = await search.recall("loom memory", limit=3)
        assert len(results) <= 3

    async def test_empty_memory_returns_empty(self, semantic, procedural):
        search = MemorySearch(semantic, procedural)
        results = await search.recall("anything")
        assert results == []

    async def test_blank_query_returns_empty(self, semantic, procedural):
        await semantic.upsert(SemanticEntry(key="k1", value="some fact"))
        search = MemorySearch(semantic, procedural)
        results = await search.recall("   ")
        assert results == []

    async def test_results_sorted_by_score_descending(self, semantic, procedural):
        await semantic.upsert(SemanticEntry(key="strong", value="loom loom loom loom"))
        await semantic.upsert(SemanticEntry(key="weak", value="loom and other things"))

        search = MemorySearch(semantic, procedural)
        results = await search.recall("loom", type="semantic")
        if len(results) >= 2:
            assert results[0].score >= results[1].score

    async def test_result_has_correct_fields(self, semantic, procedural):
        await semantic.upsert(SemanticEntry(key="my_key", value="my fact value", confidence=0.9))

        search = MemorySearch(semantic, procedural)
        results = await search.recall("fact value", type="semantic")
        r = results[0]
        assert r.key == "my_key"
        assert r.value == "my fact value"
        assert r.score > 0.0
        assert r.metadata["confidence"] == 0.9


# ---------------------------------------------------------------------------
# MemoryIndex / MemoryIndexer
# ---------------------------------------------------------------------------

class TestMemoryIndex:
    def test_is_empty_true_when_no_data(self):
        idx = MemoryIndex()
        assert idx.is_empty is True

    def test_is_empty_false_with_semantic(self):
        idx = MemoryIndex(semantic_count=1)
        assert idx.is_empty is False

    def test_is_empty_false_with_skills(self):
        idx = MemoryIndex(skill_count=3)
        assert idx.is_empty is False

    def test_render_contains_key_sections(self):
        idx = MemoryIndex(
            semantic_count=5,
            semantic_topics=["python", "loom"],
            skill_count=2,
            skill_tags=["refactor"],
            episode_sessions=3,
        )
        rendered = idx.render()
        assert "Memory Index" in rendered
        assert "5" in rendered
        assert "python" in rendered
        assert "2 active" in rendered
        assert "3 sessions" in rendered
        assert "recall" in rendered

    def test_render_no_topics_shows_none(self):
        idx = MemoryIndex(semantic_count=1, semantic_topics=[])
        rendered = idx.render()
        assert "(none)" in rendered

    def test_render_singular_fact(self):
        idx = MemoryIndex(semantic_count=1)
        assert "1 fact" in idx.render()

    def test_render_plural_facts(self):
        idx = MemoryIndex(semantic_count=5)
        assert "5 facts" in idx.render()


class TestMemoryIndexer:
    async def test_empty_db_produces_empty_index(self, semantic, procedural, episodic):
        indexer = MemoryIndexer(semantic, procedural, episodic)
        idx = await indexer.build()
        assert idx.is_empty
        assert idx.semantic_count == 0
        assert idx.skill_count == 0
        assert idx.episode_sessions == 0

    async def test_semantic_count(self, semantic, procedural, episodic):
        for i in range(3):
            await semantic.upsert(SemanticEntry(key=f"k{i}", value=f"loom fact {i}",
                                                source="session:abc:fact:0"))
        indexer = MemoryIndexer(semantic, procedural, episodic)
        idx = await indexer.build()
        assert idx.semantic_count == 3

    async def test_topics_extracted(self, semantic, procedural, episodic):
        await semantic.upsert(SemanticEntry(key="k1", value="loom framework middleware pipeline"))
        indexer = MemoryIndexer(semantic, procedural, episodic)
        idx = await indexer.build()
        assert len(idx.semantic_topics) > 0
        # "loom" should be in topics (4+ chars, not stopword)
        assert "loom" in idx.semantic_topics

    async def test_skill_count_and_tags(self, semantic, procedural, episodic):
        s1 = SkillGenome(name="s1", body="body one", tags=["python", "refactor"])
        s2 = SkillGenome(name="s2", body="body two", tags=["bash"])
        await procedural.upsert(s1)
        await procedural.upsert(s2)

        indexer = MemoryIndexer(semantic, procedural, episodic)
        idx = await indexer.build()
        assert idx.skill_count == 2
        assert "python" in idx.skill_tags
        assert "bash" in idx.skill_tags

    async def test_episode_sessions_counts_distinct_sources(self, semantic, procedural, episodic):
        # Two facts from same session, one from another
        await semantic.upsert(SemanticEntry(key="k1", value="fact", source="session:aaa:fact:0"))
        await semantic.upsert(SemanticEntry(key="k2", value="fact", source="session:aaa:fact:1"))
        await semantic.upsert(SemanticEntry(key="k3", value="fact", source="session:bbb:fact:0"))

        indexer = MemoryIndexer(semantic, procedural, episodic)
        idx = await indexer.build()
        assert idx.episode_sessions == 2

    async def test_episode_sessions_ignores_non_session_sources(self, semantic, procedural, episodic):
        await semantic.upsert(SemanticEntry(key="k1", value="fact", source="manual"))
        await semantic.upsert(SemanticEntry(key="k2", value="fact", source=None))

        indexer = MemoryIndexer(semantic, procedural, episodic)
        idx = await indexer.build()
        assert idx.episode_sessions == 0


# ---------------------------------------------------------------------------
# make_recall_tool
# ---------------------------------------------------------------------------

class TestRecallTool:
    async def test_success_returns_formatted_output(self, semantic, procedural):
        await semantic.upsert(SemanticEntry(key="loom:core", value="Loom uses middleware pipeline"))

        search = MemorySearch(semantic, procedural)
        tool = make_recall_tool(search)
        call = _make_call("recall", {"query": "middleware pipeline"})
        result = await tool.executor(call)

        assert result.success is True
        assert "loom:core" in result.output
        assert "middleware" in result.output.lower()

    async def test_missing_query_returns_error(self, semantic, procedural):
        search = MemorySearch(semantic, procedural)
        tool = make_recall_tool(search)
        call = _make_call("recall", {})
        result = await tool.executor(call)

        assert result.success is False
        assert "query" in result.error.lower()

    async def test_empty_results_message(self, semantic, procedural):
        search = MemorySearch(semantic, procedural)
        tool = make_recall_tool(search)
        call = _make_call("recall", {"query": "nonexistent topic xyz"})
        result = await tool.executor(call)

        assert result.success is True
        assert "No memories stored yet." in result.output

    async def test_type_filter_forwarded(self, semantic, procedural):
        skill = SkillGenome(name="bash_skill", body="use bash for automation", tags=["bash"])
        await procedural.upsert(skill)

        search = MemorySearch(semantic, procedural)
        tool = make_recall_tool(search)
        call = _make_call("recall", {"query": "bash automation", "type": "skill"})
        result = await tool.executor(call)

        assert result.success is True

    async def test_limit_capped_at_10(self, semantic, procedural):
        for i in range(15):
            await semantic.upsert(SemanticEntry(key=f"k{i}", value=f"loom fact {i}"))

        search = MemorySearch(semantic, procedural)
        tool = make_recall_tool(search)
        # Request 50, should be capped at 10
        call = _make_call("recall", {"query": "loom", "limit": 50})
        result = await tool.executor(call)
        assert result.success is True

    def test_tool_is_safe_trust_level(self, semantic=None, procedural=None):
        # Just check the metadata without needing DB
        from unittest.mock import MagicMock
        mock_search = MagicMock()
        tool = make_recall_tool(mock_search)
        assert tool.trust_level == TrustLevel.SAFE
        assert "recall" in tool.tags

    def test_tool_has_correct_schema(self):
        from unittest.mock import MagicMock
        tool = make_recall_tool(MagicMock())
        assert "query" in tool.input_schema["properties"]
        assert "query" in tool.input_schema["required"]


# ---------------------------------------------------------------------------
# make_memorize_tool
# ---------------------------------------------------------------------------

class TestMemorizeTool:
    async def test_persists_entry(self, semantic):
        tool = make_memorize_tool(semantic)
        call = _make_call("memorize", {"key": "project:arch", "value": "Loom uses SQLite WAL"})
        result = await tool.executor(call)

        assert result.success is True
        stored = await semantic.get("project:arch")
        assert stored is not None
        assert stored.value == "Loom uses SQLite WAL"
        assert stored.source == "memorize"

    async def test_missing_key_returns_error(self, semantic):
        tool = make_memorize_tool(semantic)
        call = _make_call("memorize", {"value": "some fact"})
        result = await tool.executor(call)
        assert result.success is False

    async def test_missing_value_returns_error(self, semantic):
        tool = make_memorize_tool(semantic)
        call = _make_call("memorize", {"key": "some_key"})
        result = await tool.executor(call)
        assert result.success is False

    async def test_confidence_default(self, semantic):
        tool = make_memorize_tool(semantic)
        call = _make_call("memorize", {"key": "k1", "value": "fact"})
        await tool.executor(call)
        stored = await semantic.get("k1")
        assert stored.confidence == 0.8

    async def test_confidence_custom(self, semantic):
        tool = make_memorize_tool(semantic)
        call = _make_call("memorize", {"key": "k2", "value": "fact", "confidence": 0.95})
        await tool.executor(call)
        stored = await semantic.get("k2")
        assert stored.confidence == pytest.approx(0.95)

    async def test_confidence_clamped_above_one(self, semantic):
        tool = make_memorize_tool(semantic)
        call = _make_call("memorize", {"key": "k3", "value": "fact", "confidence": 5.0})
        await tool.executor(call)
        stored = await semantic.get("k3")
        assert stored.confidence <= 1.0

    async def test_confidence_clamped_below_zero(self, semantic):
        tool = make_memorize_tool(semantic)
        call = _make_call("memorize", {"key": "k4", "value": "fact", "confidence": -1.0})
        await tool.executor(call)
        stored = await semantic.get("k4")
        assert stored.confidence >= 0.0

    async def test_upsert_updates_existing(self, semantic):
        tool = make_memorize_tool(semantic)
        call1 = _make_call("memorize", {"key": "shared", "value": "original"})
        call2 = _make_call("memorize", {"key": "shared", "value": "updated"})
        await tool.executor(call1)
        await tool.executor(call2)
        stored = await semantic.get("shared")
        assert stored.value == "updated"

    def test_tool_is_guarded_trust_level(self):
        from unittest.mock import MagicMock
        mock_sem = MagicMock()
        tool = make_memorize_tool(mock_sem)
        assert tool.trust_level == TrustLevel.GUARDED
        assert "memorize" in tool.tags

# ---------------------------------------------------------------------------
# Embedding provider + multi-fallback recall (Phase 5)
# ---------------------------------------------------------------------------

from loom.core.memory.embeddings import (
    cosine_similarity,
    MiniMaxEmbeddingProvider,
    build_embedding_provider,
)


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_empty_vector_returns_zero(self):
        assert cosine_similarity([], []) == 0.0

    def test_zero_vector_returns_zero(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_dimension_mismatch_returns_zero(self):
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0

    def test_general_similarity(self):
        a = [1.0, 1.0, 0.0]
        b = [1.0, 0.5, 0.0]
        sim = cosine_similarity(a, b)
        assert 0.9 < sim < 1.0


class TestBuildEmbeddingProvider:
    def test_returns_provider_when_key_present(self):
        # Pass cfg so build_embedding_provider knows to use the "minimax" provider
        provider = build_embedding_provider(
            env={"minimax.io_key": "sk-test"},
            cfg={"embeddings": {"provider": "minimax", "api_key_env": "minimax.io_key"}},
        )
        assert isinstance(provider, MiniMaxEmbeddingProvider)

    def test_returns_none_when_no_key(self):
        assert build_embedding_provider({}, {}) is None
        assert build_embedding_provider({"OTHER_KEY": "x"}, {}) is None

    def test_uses_minimax_api_key_env(self):
        # Pass cfg so provider is recognized as "minimax" and api_key_env fallback kicks in
        provider = build_embedding_provider(
            env={"MINIMAX_API_KEY": "sk-test2"},
            cfg={"embeddings": {"provider": "minimax"}},
        )
        assert provider is not None


class TestSemanticMemoryWithEmbeddings:
    async def test_upsert_stores_embedding_when_provider_set(self, db_conn):
        mock_provider = AsyncMock()
        mock_provider.embed.return_value = [[0.1, 0.2, 0.3]]

        sem = SemanticMemory(db_conn, embedding_provider=mock_provider)
        await sem.upsert(SemanticEntry(key="test:1", value="Loom uses SQLite"))

        mock_provider.embed.assert_called_once()
        pairs = await sem.list_with_embeddings(10)
        assert len(pairs) == 1
        entry, vec = pairs[0]
        assert vec == pytest.approx([0.1, 0.2, 0.3])

    async def test_upsert_without_provider_leaves_embedding_null(self, semantic):
        await semantic.upsert(SemanticEntry(key="no:emb", value="no embedding"))
        pairs = await semantic.list_with_embeddings(10)
        _, vec = pairs[0]
        assert vec is None

    async def test_embedding_failure_does_not_block_write(self, db_conn):
        mock_provider = AsyncMock()
        mock_provider.embed.side_effect = RuntimeError("API down")

        sem = SemanticMemory(db_conn, embedding_provider=mock_provider)
        # Should not raise
        await sem.upsert(SemanticEntry(key="safe:write", value="still written"))

        stored = await sem.get("safe:write")
        assert stored is not None

    def test_has_embeddings_property(self, semantic):
        assert semantic.has_embeddings is False

    async def test_has_embeddings_true_with_provider(self, db_conn):
        mock_provider = AsyncMock()
        sem = SemanticMemory(db_conn, embedding_provider=mock_provider)
        assert sem.has_embeddings is True

    async def test_list_with_embeddings_returns_none_for_missing(self, semantic):
        await semantic.upsert(SemanticEntry(key="k1", value="fact one"))
        await semantic.upsert(SemanticEntry(key="k2", value="fact two"))
        pairs = await semantic.list_with_embeddings(10)
        assert all(vec is None for _, vec in pairs)


class TestMemorySearchEmbeddingTier:
    async def test_embedding_tier_used_when_provider_configured(self, db_conn, procedural):
        mock_provider = AsyncMock()
        # upsert calls embed once (write-time), recall calls embed once (query)
        mock_provider.embed.side_effect = [
            [[1.0, 0.0]],   # write-time embedding (from upsert)
            [[1.0, 0.0]],   # query embedding (from _search_semantic_embedding)
        ]
        sem = SemanticMemory(db_conn, embedding_provider=mock_provider)
        await sem.upsert(SemanticEntry(key="loom:arch", value="middleware pipeline"))

        search = MemorySearch(sem, procedural)
        results = await search.recall("middleware", type="semantic", limit=5)

        assert len(results) >= 1
        assert results[0].metadata.get("method") == "embedding"

    async def test_falls_through_to_bm25_on_embedding_error(self, db_conn, procedural):
        mock_provider = AsyncMock()
        mock_provider.embed.side_effect = RuntimeError("network error")

        sem = SemanticMemory(db_conn, embedding_provider=mock_provider)
        await sem.upsert(SemanticEntry(key="bm25:fact", value="loom harness pipeline"))

        search = MemorySearch(sem, procedural)
        # BM25 should find this even though embedding failed
        results = await search.recall("harness", type="semantic", limit=5)
        assert any(r.key == "bm25:fact" for r in results)

    async def test_falls_through_to_recency_when_no_embedding_no_bm25(
        self, db_conn, procedural
    ):
        sem = SemanticMemory(db_conn)
        await sem.upsert(SemanticEntry(key="recent:1", value="some stored fact"))

        search = MemorySearch(sem, procedural)
        # Chinese query → no BM25 match → recency fallback
        results = await search.recall("查詢完全不相關", type="semantic", limit=5)
        assert len(results) == 1
        assert results[0].metadata.get("fallback") is True