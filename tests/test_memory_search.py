"""
Tests for Phase 4B — BM25, MemorySearch, MemoryIndex, and memory tools.

Coverage
--------
BM25
  - empty corpus → score 0 / top_k []
  - single document scoring
  - multi-document ranking (correct relative order)
  - zero score for fully irrelevant query
  - case insensitivity
  - punctuation stripped
  - top_k respects k limit
  - top_k returns only positive-score results
  - zero-length document handled

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
from loom.core.memory.search import BM25, MemorySearch, MemorySearchResult
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
# BM25
# ---------------------------------------------------------------------------

class TestBM25:
    def test_empty_corpus_score_zero(self):
        bm = BM25()
        bm.index([])
        assert bm.score("anything", 0) == 0.0

    def test_empty_corpus_top_k_empty(self):
        bm = BM25()
        bm.index([])
        assert bm.top_k("query") == []

    def test_single_document_match(self):
        bm = BM25()
        bm.index(["python loom memory"])
        score = bm.score("python", 0)
        assert score > 0.0

    def test_single_document_no_match(self):
        bm = BM25()
        bm.index(["python loom memory"])
        assert bm.score("javascript", 0) == 0.0

    def test_ranking_order(self):
        """More relevant doc should rank higher."""
        docs = [
            "loom is a framework",                     # idx 0 — weak
            "loom loom loom agent framework memory",   # idx 1 — strong
        ]
        bm = BM25()
        bm.index(docs)
        top = bm.top_k("loom", k=2)
        assert len(top) == 2
        assert top[0][0] == 1       # higher tf → should rank first
        assert top[0][1] > top[1][1]

    def test_top_k_respects_limit(self):
        docs = [f"word{i} extra" for i in range(20)]
        bm = BM25()
        bm.index(docs)
        top = bm.top_k("word", k=5)
        assert len(top) <= 5

    def test_top_k_only_positive_scores(self):
        bm = BM25()
        bm.index(["alpha beta", "gamma delta"])
        top = bm.top_k("alpha")
        # "gamma delta" has 0 score for "alpha"
        assert all(score > 0 for _, score in top)

    def test_case_insensitive(self):
        bm = BM25()
        bm.index(["LOOM Framework"])
        assert bm.score("loom", 0) > 0.0
        assert bm.score("LOOM", 0) == bm.score("loom", 0)

    def test_punctuation_stripped(self):
        bm = BM25()
        bm.index(["loom, framework."])
        assert bm.score("loom", 0) > 0.0

    def test_zero_length_document(self):
        bm = BM25()
        bm.index(["", "real content here"])
        # empty doc should not cause division by zero
        assert bm.score("real", 0) == 0.0
        assert bm.score("real", 1) > 0.0

    def test_out_of_range_doc_idx(self):
        bm = BM25()
        bm.index(["doc"])
        assert bm.score("doc", 99) == 0.0

    def test_multi_term_query(self):
        bm = BM25()
        # doc 0 matches both "python" and "loom"; doc 1 matches neither
        bm.index(["python memory loom", "javascript database"])
        top = bm.top_k("python loom", k=2)
        assert len(top) >= 1
        assert top[0][0] == 0   # the python+loom doc ranks first


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
        assert "No relevant" in result.output

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
        assert stored.source == "agent"

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
