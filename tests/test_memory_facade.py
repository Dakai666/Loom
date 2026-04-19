"""
Issue #147 階段 A — MemoryFacade.

Verifies the facade owns the four memory subsystems plus the search
index, and that its three high-level read methods (``search`` /
``get_fact`` / ``query_relations``) delegate to the right subsystem.
Also verifies handle identity so ``LoomSession`` callers that still
reach through ``self._semantic`` etc. keep seeing the same object the
facade holds.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.facade import MemoryFacade
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.relational import RelationalEntry, RelationalMemory
from loom.core.memory.search import MemorySearch
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.store import SQLiteStore


@pytest_asyncio.fixture
async def facade(tmp_path):
    store = SQLiteStore(str(tmp_path / "facade.db"))
    await store.initialize()
    async with store.connect() as db:
        semantic = SemanticMemory(db)
        procedural = ProceduralMemory(db)
        relational = RelationalMemory(db)
        episodic = EpisodicMemory(db)
        search = MemorySearch(semantic, procedural)
        yield MemoryFacade(
            semantic=semantic, procedural=procedural,
            relational=relational, episodic=episodic, search=search,
        )


# ── construction & handle identity ─────────────────────────────────────────

def test_facade_exposes_subsystem_handles(facade):
    """All five attributes are present and are the exact instances passed in."""
    assert isinstance(facade.semantic, SemanticMemory)
    assert isinstance(facade.procedural, ProceduralMemory)
    assert isinstance(facade.relational, RelationalMemory)
    assert isinstance(facade.episodic, EpisodicMemory)
    assert isinstance(facade.search_index, MemorySearch)


def test_search_index_wraps_facade_subsystems(facade):
    """The search index uses the same semantic + procedural instances the
    facade exposes — no parallel object trees."""
    assert facade.search_index._semantic is facade.semantic
    assert facade.search_index._procedural is facade.procedural


# ── read API: get_fact ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_fact_returns_entry_when_present(facade):
    await facade.semantic.upsert(SemanticEntry(
        key="loom:test", value="hello", confidence=0.9, source="user_explicit",
    ))
    fact = await facade.get_fact("loom:test")
    assert fact is not None
    assert fact.value == "hello"


@pytest.mark.asyncio
async def test_get_fact_returns_none_for_missing_key(facade):
    assert await facade.get_fact("nope") is None


# ── read API: query_relations ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_query_relations_filters_by_subject(facade):
    await facade.relational.upsert(RelationalEntry(
        subject="alice", predicate="knows", object="bob", source="user",
    ))
    await facade.relational.upsert(RelationalEntry(
        subject="alice", predicate="likes", object="cake", source="user",
    ))
    await facade.relational.upsert(RelationalEntry(
        subject="bob", predicate="knows", object="carol", source="user",
    ))

    alice_rels = await facade.query_relations(subject="alice")
    assert len(alice_rels) == 2
    assert {r.predicate for r in alice_rels} == {"knows", "likes"}

    knows_rels = await facade.query_relations(predicate="knows")
    assert len(knows_rels) == 2
    assert {r.subject for r in knows_rels} == {"alice", "bob"}


# ── read API: search delegates to MemorySearch ─────────────────────────────

@pytest.mark.asyncio
async def test_search_delegates_to_search_index(facade):
    await facade.semantic.upsert(SemanticEntry(
        key="topic:loom", value="loom is a memory-native agent framework",
        confidence=0.9, source="user_explicit",
    ))
    results = await facade.search("memory framework", type="semantic", limit=5)
    assert results, "facade.search should hit the BM25 index for matching content"
    assert any("loom is a memory-native" in r.value for r in results)


# ── session integration ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_session_holds_facade_aliased_to_subsystems(monkeypatch, tmp_path):
    """Issue #147 階段 A: ``LoomSession.start()`` builds the facade after
    the four subsystems exist, and ``self._semantic`` / ``_procedural`` /
    ``_relational`` / ``_episodic`` point at the same instances the
    facade holds.  Existing callers that reach into those private
    attributes must keep working in Phase A."""
    from unittest.mock import MagicMock
    from rich.prompt import Confirm
    import loom as loom_pkg
    from loom.core import session as core_session

    # Isolate default registry so plugin probe doesn't pollute test state
    registry = loom_pkg._get_default_registry()
    original_tools = dict(registry._tools)
    registry._tools.clear()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(core_session, "build_router", lambda: MagicMock())
    monkeypatch.setattr(core_session, "_load_loom_config", lambda: {})
    monkeypatch.setattr(core_session, "_load_env", lambda project_root=None: {})
    monkeypatch.setattr(core_session, "build_embedding_provider", lambda env, cfg: None)
    monkeypatch.setattr(Confirm, "ask", lambda *args, **kwargs: True)

    from loom.core.session import LoomSession
    session = LoomSession(
        model="gpt-test",
        db_path=str(tmp_path / "loom.db"),
        workspace=workspace,
    )
    try:
        await session.start()

        assert hasattr(session, "_memory")
        assert session._memory.semantic is session._semantic
        assert session._memory.procedural is session._procedural
        assert session._memory.relational is session._relational
        assert session._memory.episodic is session._episodic
    finally:
        await session.stop()
        registry._tools.clear()
        registry._tools.update(original_tools)
