"""Session wiring + turn_start / turn_end emit (#322 commit 6).

Verifies that LoomSession.start() opens a LedgerStore, builds a session-scoped
LedgerEmitter, and threads it through every subsystem that has a
``ledger_emitter`` kwarg (MemoryFacade, task_write tool, LifecycleMiddleware,
BlastRadiusMiddleware). Also verifies the turn_start / turn_end emit helpers.

Deferred (Step 2 follow-up — see commit message):
- thought event with §3.3 buffered full_text capture
- model_event after each LLM call
- judge_verdict
- artifact_emit
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

import loom as loom_pkg


@pytest.fixture(autouse=True)
def _isolate_default_registry():
    registry = loom_pkg._get_default_registry()
    original = dict(registry._tools)
    registry._tools.clear()
    try:
        yield
    finally:
        registry._tools.clear()
        registry._tools.update(original)


@pytest_asyncio.fixture
async def session_module():
    from loom.core import session as core_session

    return core_session


async def _start_session(monkeypatch, tmp_path: Path, session_module):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(session_module, "build_router", lambda: MagicMock())
    monkeypatch.setattr(session_module, "_load_loom_config", lambda: {})
    monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {})
    monkeypatch.setattr(session_module, "build_embedding_provider", lambda env, cfg: None)
    from rich.prompt import Confirm

    monkeypatch.setattr(Confirm, "ask", lambda *args, **kwargs: True)

    from loom.core.session import LoomSession

    session = LoomSession(
        model="gpt-test",
        db_path=str(tmp_path / "loom.db"),
        workspace=workspace,
    )
    await session.start()
    return session


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


async def test_start_opens_ledger_and_wires_emitter_into_subsystems(
    monkeypatch, tmp_path, session_module
):
    session = await _start_session(monkeypatch, tmp_path, session_module)
    try:
        # Ledger handles exist
        assert session._ledger_store is not None
        assert session._ledger_emitter is not None
        assert session._ledger_emitter.session_id == session.session_id

        # MemoryFacade got the emitter
        assert session._memory.ledger_emitter is session._ledger_emitter

        # Middleware pipeline got the emitter (LifecycleMiddleware +
        # BlastRadiusMiddleware are inside _pipeline._middlewares).
        from loom.core.harness.middleware import (
            BlastRadiusMiddleware,
            LifecycleMiddleware,
        )

        seen = {LifecycleMiddleware: False, BlastRadiusMiddleware: False}
        for mw in session._pipeline._middlewares:
            for cls in seen:
                if isinstance(mw, cls):
                    assert mw._ledger_emitter is session._ledger_emitter
                    seen[cls] = True
        assert all(seen.values()), f"unwired middlewares: {seen}"
    finally:
        await session.stop()


async def test_stop_closes_ledger(monkeypatch, tmp_path, session_module):
    session = await _start_session(monkeypatch, tmp_path, session_module)
    store = session._ledger_store
    assert store is not None
    await session.stop()
    # store reference cleared on session
    assert session._ledger_store is None
    assert session._ledger_emitter is None


async def test_disabled_via_config(monkeypatch, tmp_path, session_module):
    """``[ledger].enabled = false`` opts out — every subsystem gets None."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(session_module, "build_router", lambda: MagicMock())
    monkeypatch.setattr(
        session_module, "_load_loom_config", lambda: {"ledger": {"enabled": False}}
    )
    monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {})
    monkeypatch.setattr(session_module, "build_embedding_provider", lambda env, cfg: None)
    from rich.prompt import Confirm

    monkeypatch.setattr(Confirm, "ask", lambda *args, **kwargs: True)

    from loom.core.session import LoomSession

    session = LoomSession(
        model="gpt-test",
        db_path=str(tmp_path / "loom.db"),
        workspace=workspace,
    )
    await session.start()
    try:
        assert session._ledger_store is None
        assert session._ledger_emitter is None
        assert session._memory.ledger_emitter is None
    finally:
        await session.stop()


# ---------------------------------------------------------------------------
# Turn boundary emit helpers
# ---------------------------------------------------------------------------


async def test_emit_turn_start_carries_prompt_stack_snapshot(
    monkeypatch, tmp_path, session_module
):
    session = await _start_session(monkeypatch, tmp_path, session_module)
    try:
        await session._emit_ledger_turn_start("turn_abc")

        rows = await session._ledger_store.fetch_by_turn("turn_abc")
        starts = [r for r in rows if r.event_type == "turn_start"]
        assert len(starts) == 1
        p = starts[0].payload
        assert p["prompt_stack_hash"].startswith("sha256:")
        assert "tool_catalog_size" in p["prompt_stack_components"]
        assert p["prompt_stack_components"]["tool_catalog_size"] >= 0
    finally:
        await session.stop()


async def test_emit_turn_end_links_to_turn_start(
    monkeypatch, tmp_path, session_module
):
    session = await _start_session(monkeypatch, tmp_path, session_module)
    try:
        await session._emit_ledger_turn_start("turn_xyz")
        await session._emit_ledger_turn_end("turn_xyz", "clean", 1234)

        rows = await session._ledger_store.fetch_by_turn("turn_xyz")
        starts = [r for r in rows if r.event_type == "turn_start"]
        ends = [r for r in rows if r.event_type == "turn_end"]
        assert len(starts) == 1 and len(ends) == 1
        assert ends[0].parent_event_id == starts[0].event_id
        assert ends[0].payload["outcome"] == "clean"
        assert ends[0].payload["duration_ms"] == 1234
    finally:
        await session.stop()
