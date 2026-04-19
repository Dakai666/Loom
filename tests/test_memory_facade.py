"""
Issue #147 — MemoryFacade.

Verifies the facade owns the four memory subsystems + search index +
optional governor, and that its read API (``search`` / ``get_fact`` /
``query_relations``) and write API (``memorize`` / ``relate`` /
``prune_decayed``) delegate to the right subsystem.  Also verifies
handle identity so ``LoomSession`` callers that still reach through
``self._semantic`` etc. keep seeing the same object the facade holds.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.facade import MemoryFacade
from loom.core.memory.governance import GovernedWriteResult, MemoryGovernor
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
    results = await facade.search("memory framework", kind="semantic", limit=5)
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


# ── write API: memorize (no governor — fallback path) ──────────────────────

@pytest.mark.asyncio
async def test_memorize_without_governor_falls_back_to_semantic_upsert(facade):
    """When no governor is wired, ``memorize`` writes through the
    semantic subsystem directly and synthesises a ``GovernedWriteResult``
    so callers see a uniform contract."""
    result = await facade.memorize(SemanticEntry(
        key="loom:phaseB", value="memorize goes through facade",
        confidence=0.85, source="test",
    ))

    assert isinstance(result, GovernedWriteResult)
    assert result.written is True
    assert result.contradictions_found == 0
    assert result.trust_tier == "unknown"  # marker for governor-less path

    stored = await facade.semantic.get("loom:phaseB")
    assert stored is not None
    assert stored.value == "memorize goes through facade"


@pytest.mark.asyncio
async def test_memorize_without_governor_marks_overwrite_in_resolution(facade):
    """Synthesised GovernedWriteResult uses 'replaced' resolution when
    upsert reports a value conflict — gives callers parity with the
    governed path's resolution semantics."""
    await facade.semantic.upsert(SemanticEntry(
        key="loom:overwrite", value="original", confidence=0.5, source="test",
    ))
    result = await facade.memorize(SemanticEntry(
        key="loom:overwrite", value="updated", confidence=0.5, source="test",
    ))
    assert result.written is True
    assert result.resolution == "replaced"


# ── write API: memorize (with governor) ────────────────────────────────────

@pytest_asyncio.fixture
async def governed_facade(tmp_path):
    """Facade wired with a real MemoryGovernor so the governance path
    can be exercised end-to-end."""
    store = SQLiteStore(str(tmp_path / "governed.db"))
    await store.initialize()
    async with store.connect() as db:
        semantic = SemanticMemory(db)
        procedural = ProceduralMemory(db)
        relational = RelationalMemory(db)
        episodic = EpisodicMemory(db)
        governor = MemoryGovernor(
            semantic=semantic, procedural=procedural,
            relational=relational, episodic=episodic,
            db=db, session_id="test-session",
        )
        await governor.health.ensure_table()
        semantic._health = governor.health
        search = MemorySearch(semantic, procedural)
        yield MemoryFacade(
            semantic=semantic, procedural=procedural,
            relational=relational, episodic=episodic,
            search=search, governor=governor,
        )


@pytest.mark.asyncio
async def test_memorize_with_governor_routes_through_governed_upsert(governed_facade):
    """When a governor is wired, ``memorize`` delegates so trust
    classification and audit logging happen for every write."""
    result = await governed_facade.memorize(SemanticEntry(
        key="proj:routing", value="facade routes through governor",
        confidence=0.6, source="manual",
    ))
    assert result.written is True
    # "manual" classifies as user_explicit — governor must have run
    assert result.trust_tier == "user_explicit"
    # Trust tier should have lifted confidence to the tier's floor (1.0)
    assert result.adjusted_confidence == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_memorize_surfaces_embedding_failure_via_warn_log(
    governed_facade, caplog,
):
    """The facade snapshots the health tracker's embedding-write
    failure count before/after each write; if it climbs, a structured
    WARN log is emitted so callers don't need to poll memory_health."""
    # Simulate a downstream embedding failure landing during the write.
    governed_facade.governor.health.record_failure(
        "embedding_write", "simulated provider timeout",
    )

    async def fake_governed_upsert(entry):
        # Mimic SemanticMemory recording another failure during upsert.
        governed_facade.governor.health.record_failure(
            "embedding_write", "second failure during upsert",
        )
        return GovernedWriteResult(
            written=True, trust_tier="unknown",
            adjusted_confidence=entry.confidence, contradictions_found=0,
        )

    governed_facade.governor.governed_upsert = fake_governed_upsert  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger="loom.core.memory.facade"):
        await governed_facade.memorize(SemanticEntry(
            key="proj:warn", value="will trip the warn path",
            confidence=0.5, source="test",
        ))

    assert any(
        "embedding write failed" in rec.message and "proj:warn" in rec.message
        for rec in caplog.records
    ), "expected a structured WARN log surfacing the new embedding failure"


@pytest.mark.asyncio
async def test_memorize_no_warn_when_embedding_failure_count_unchanged(
    governed_facade, caplog,
):
    """Negative case: a clean write must not produce the warn log."""
    with caplog.at_level(logging.WARNING, logger="loom.core.memory.facade"):
        await governed_facade.memorize(SemanticEntry(
            key="proj:clean", value="clean write, no failures",
            confidence=0.7, source="test",
        ))

    assert not any(
        "embedding write failed" in rec.message
        for rec in caplog.records
    )


# ── write API: relate ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_relate_upserts_into_relational_memory(facade):
    await facade.relate(RelationalEntry(
        subject="user", predicate="prefers", object="terse output", source="test",
    ))
    rels = await facade.query_relations(subject="user")
    assert len(rels) == 1
    assert rels[0].object == "terse output"


@pytest.mark.asyncio
async def test_relate_uses_facade_held_relational_instance():
    """relate() must write to the same RelationalMemory the facade
    exposes — otherwise tools that read via facade.query_relations
    wouldn't see writes from facade.relate."""
    rel_mock = AsyncMock(spec=RelationalMemory)
    facade = MemoryFacade(
        semantic=AsyncMock(), procedural=AsyncMock(),
        relational=rel_mock, episodic=AsyncMock(),
        search=AsyncMock(),
    )
    entry = RelationalEntry(subject="a", predicate="b", object="c", source="t")
    await facade.relate(entry)
    rel_mock.upsert.assert_awaited_once_with(entry)


# ── write API: prune_decayed ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prune_decayed_delegates_to_semantic(facade):
    """Sanity check — facade.prune_decayed forwards args and returns the
    semantic subsystem's report dict unchanged."""
    # Empty store: examined=0, pruned=0
    report = await facade.prune_decayed(threshold=0.2, dry_run=True)
    assert report["examined"] == 0
    assert report["pruned"] == 0
    assert report["threshold"] == 0.2
    assert report["dry_run"] is True


@pytest.mark.asyncio
async def test_prune_decayed_forwards_kwargs():
    """prune_decayed must pass threshold and dry_run through verbatim."""
    sem_mock = AsyncMock(spec=SemanticMemory)
    sem_mock.prune_decayed.return_value = {
        "examined": 5, "pruned": 1, "retained": 4,
        "threshold": 0.3, "dry_run": False,
    }
    facade = MemoryFacade(
        semantic=sem_mock, procedural=AsyncMock(),
        relational=AsyncMock(), episodic=AsyncMock(), search=AsyncMock(),
    )
    out = await facade.prune_decayed(threshold=0.3, dry_run=False)
    sem_mock.prune_decayed.assert_awaited_once_with(threshold=0.3, dry_run=False)
    assert out["pruned"] == 1


# ── session integration: agent tools wired through facade ──────────────────

@pytest.mark.asyncio
async def test_session_registers_memory_tools_through_facade(monkeypatch, tmp_path):
    """Issue #147 階段 B: the four agent memory tools (recall, memorize,
    relate, query_relations) must be registered with the facade — not
    individual subsystem references — so future Phase C migrations can
    drop the direct subsystem fields without breaking tool wiring."""
    from unittest.mock import MagicMock
    from rich.prompt import Confirm
    import loom as loom_pkg
    from loom.core import session as core_session

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

        # Phase B contract: facade carries the governor, and the four
        # agent memory tools are present on the registry.
        assert session._memory.governor is session._governor
        for name in ("recall", "memorize", "relate", "query_relations"):
            assert session.registry.get(name) is not None, (
                f"tool {name!r} must be registered through the facade"
            )
    finally:
        await session.stop()
        registry._tools.clear()
        registry._tools.update(original_tools)
