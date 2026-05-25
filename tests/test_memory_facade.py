"""
Issue #147 — MemoryFacade.

Verifies the facade owns the memory subsystems + search index +
optional governor, and that its read API (``search`` / ``get_fact``)
and write API (``memorize`` / ``relate`` / ``prune_decayed``) delegate
to the right subsystem. Also verifies handle identity so ``LoomSession``
callers that still reach through ``self._semantic`` etc. keep seeing
the same object the facade holds.

#451 phase B: ``query_relations`` and ``MemoryFacade.relational`` are
retired — relational triples live in the semantic store via the
``relational_bridge`` and surface through ``recall``.
"""
from __future__ import annotations

from datetime import UTC, datetime
import logging
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from loom.core.memory.episodic import EpisodicEntry, EpisodicMemory
from loom.core.memory.facade import MemoryFacade
from loom.core.memory.governance import GovernedWriteResult, MemoryGovernor
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.relational_bridge import (
    RelationalEntry,
    get_triple,
    query_triples,
)
from loom.core.memory.search import MemorySearch
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.session_log import SessionLog
from loom.core.memory.store import SQLiteStore


@pytest_asyncio.fixture
async def facade(tmp_path):
    store = SQLiteStore(str(tmp_path / "facade.db"))
    await store.initialize()
    async with store.connect() as db:
        semantic = SemanticMemory(db)
        procedural = ProceduralMemory(db)
        episodic = EpisodicMemory(db)
        search = MemorySearch(semantic, procedural)
        session_log = SessionLog(db)
        yield MemoryFacade(
            semantic=semantic, procedural=procedural,
            episodic=episodic, search=search,
            session_log=session_log,
        )


# ── construction & handle identity ─────────────────────────────────────────

def test_facade_exposes_subsystem_handles(facade):
    """All four attributes are present and are the exact instances passed in."""
    assert isinstance(facade.semantic, SemanticMemory)
    assert isinstance(facade.procedural, ProceduralMemory)
    assert isinstance(facade.episodic, EpisodicMemory)
    assert isinstance(facade.search_index, MemorySearch)
    assert not hasattr(facade, "relational"), (
        "MemoryFacade.relational retired in #451 phase B"
    )


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


# ── relational triples flow through recall ─────────────────────────────────

@pytest.mark.asyncio
async def test_relate_writes_visible_to_recall(facade):
    """#451 phase B: ``facade.relate`` lands in the semantic store, and
    ``facade.search`` / agent ``recall`` is the sole read verb — there is
    no separate ``query_relations``."""
    await facade.relate(RelationalEntry(
        subject="alice", predicate="knows", object="bob", source="user",
    ))
    await facade.relate(RelationalEntry(
        subject="alice", predicate="likes", object="cake", source="user",
    ))

    # Bridge read path (used by MemoryIndex self-portrait projection).
    triples = await query_triples(facade.semantic, subject="alice")
    assert {t.predicate for t in triples} == {"knows", "likes"}

    # Agent-facing read path: recall surfaces the same rows tagged "relational".
    results = await facade.search("alice knows bob", limit=5)
    keys = {r.key for r in results}
    assert "rel:alice::knows" in keys


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


@pytest.mark.asyncio
async def test_recall_period_returns_grouped_memory_layers(facade):
    await facade.semantic.upsert(SemanticEntry(
        key="inside:fact", value="Loom discussed time-aware recall",
        confidence=0.9, source="memorize",
    ))
    await facade.semantic.upsert(SemanticEntry(
        key="outside:fact", value="Outside range", confidence=0.9,
    ))
    await facade.semantic._db.execute(
        "UPDATE semantic_entries SET updated_at = ? WHERE key = ?",
        (datetime(2026, 5, 10, 12, 0, tzinfo=UTC).isoformat(), "inside:fact"),
    )
    await facade.semantic._db.execute(
        "UPDATE semantic_entries SET updated_at = ? WHERE key = ?",
        (datetime(2026, 5, 9, 12, 0, tzinfo=UTC).isoformat(), "outside:fact"),
    )
    await facade.episodic.write(EpisodicEntry(
        session_id="s1", event_type="message", content="User asked about yesterday",
        created_at=datetime(2026, 5, 10, 13, 0, tzinfo=UTC),
    ))
    await facade.session_log.create_session("s1", "model", title="Memory chat")
    await facade.session_log.update_session(
        "s1", 1, datetime(2026, 5, 10, 14, 0, tzinfo=UTC).isoformat(), None,
    )
    await facade.session_log.log_message("s1", 0, "user", "raw session message")
    await facade.session_log._db.execute(
        "UPDATE session_log SET created_at = ? WHERE content = ?",
        (datetime(2026, 5, 10, 14, 30, tzinfo=UTC).isoformat(), "raw session message"),
    )
    await facade.semantic._db.commit()

    result = await facade.recall_period(
        since=datetime(2026, 5, 10, tzinfo=UTC),
        until=datetime(2026, 5, 11, tzinfo=UTC),
        include_episodic=True,
        include_sessions=True,
        limit=5,
    )

    assert [e.key for e in result["semantic"]] == ["inside:fact"]
    assert [e.content for e in result["episodic"]] == ["User asked about yesterday"]
    assert [s["session_id"] for s in result["sessions"]] == ["s1"]
    assert [m["content"] for m in result["messages"]] == ["raw session message"]


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
    monkeypatch.setattr(core_session, "build_router", lambda *a, **k: MagicMock())
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

        # Issue #147 Phase C.2: per-subsystem session attributes were
        # removed.  The facade is now the only access path — assert
        # every subsystem is wired and live. #451 phase B: relational
        # is no longer a facade attribute (triples live in semantic
        # via the bridge).
        assert hasattr(session, "_memory")
        assert session._memory is not None
        assert session._memory.semantic is not None
        assert session._memory.procedural is not None
        assert session._memory.episodic is not None
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
        episodic = EpisodicMemory(db)
        governor = MemoryGovernor(
            semantic=semantic, procedural=procedural,
            episodic=episodic,
            db=db, session_id="test-session",
        )
        await governor.health.ensure_table()
        semantic._health = governor.health
        search = MemorySearch(semantic, procedural)
        yield MemoryFacade(
            semantic=semantic, procedural=procedural,
            episodic=episodic,
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
async def test_relate_writes_into_semantic_via_bridge(facade):
    """#451 phase B: ``facade.relate`` encodes the triple as a
    ``SemanticEntry`` keyed ``rel:{subject}::{predicate}`` so ``recall``
    naturally surfaces it. No separate relational store any more."""
    await facade.relate(RelationalEntry(
        subject="user", predicate="prefers", object="terse output", source="test",
    ))
    sem_row = await facade.semantic.get("rel:user::prefers")
    assert sem_row is not None
    assert sem_row.value == "user prefers terse output"

    # Bridge round-trip works.
    triple = await get_triple(facade.semantic, "user", "prefers")
    assert triple.object == "terse output"


@pytest.mark.asyncio
async def test_relate_routes_through_facade_semantic_handle():
    """relate() must write to the same SemanticMemory the facade exposes —
    otherwise recall and the bridge readers won't see the new triple."""
    sem_mock = AsyncMock(spec=SemanticMemory)
    facade = MemoryFacade(
        semantic=sem_mock, procedural=AsyncMock(),
        episodic=AsyncMock(),
        search=AsyncMock(),
    )
    entry = RelationalEntry(subject="a", predicate="b", object="c", source="t")
    await facade.relate(entry)
    # Bridge upserts a SemanticEntry — the key must encode (a, b).
    sem_mock.upsert.assert_awaited_once()
    sem_entry = sem_mock.upsert.await_args.args[0]
    assert sem_entry.key == "rel:a::b"
    assert sem_entry.value == "a b c"


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
        episodic=AsyncMock(), search=AsyncMock(),
    )
    out = await facade.prune_decayed(threshold=0.3, dry_run=False)
    sem_mock.prune_decayed.assert_awaited_once_with(threshold=0.3, dry_run=False)
    assert out["pruned"] == 1


# ── session integration: agent tools wired through facade ──────────────────

@pytest.mark.asyncio
async def test_session_registers_memory_tools_through_facade(monkeypatch, tmp_path):
    """Issue #147 階段 B: the agent memory tools (recall, memorize, relate)
    must be registered with the facade — not individual subsystem
    references. #451 phase B retires ``query_relations`` (recall is the
    sole read verb)."""
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
    monkeypatch.setattr(core_session, "build_router", lambda *a, **k: MagicMock())
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
        for name in ("recall", "memorize", "relate"):
            assert session.registry.get(name) is not None, (
                f"tool {name!r} must be registered through the facade"
            )
        # #451 phase B: query_relations retired.
        assert session.registry.get("query_relations") is None
    finally:
        await session.stop()
        registry._tools.clear()
        registry._tools.update(original_tools)
