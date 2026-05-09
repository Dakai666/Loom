"""#336 — memory compaction subscriber.

Tests the shape of the new background trigger:

- ``_run_compaction_check`` does the threshold gate + ``compress_session``
  call extracted from the prior inline trigger
- ``_compaction_subscriber_loop`` reacts to ledger ``turn_end`` events
- ``_compaction_lock`` serialises concurrent runs so two near-simultaneous
  turn_end events don't cause double-write of semantic facts
- ``_pending_compactions`` buffers CompressDone signals for the next
  stream_turn to yield (Discord display path)

The actual ``compress_session`` is heavy (LLM call) — every test
monkeypatches the module-level binding so we can drive the gate logic
deterministically and instrument call counts.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path
from typing import Any

import pytest_asyncio

from loom.core.events import CompressDone
from loom.core.ledger import (
    LedgerEmitter,
    LedgerStore,
    TurnEndPayload,
    async_correlation_scope,
    async_turn_scope,
)


# ---------------------------------------------------------------------------
# Fixtures + stub session
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def ledger(tmp_path: Path) -> LedgerStore:
    s = LedgerStore(
        db_path=tmp_path / "ledger.db",
        blob_dir=tmp_path / "blobs",
    )
    await s.open()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
def emitter(ledger: LedgerStore) -> LedgerEmitter:
    return LedgerEmitter(ledger, session_id="sess_compact")


def _make_stub(
    *,
    ep_count: int,
    threshold: int = 30,
    fact_count: int = 4,
    raise_in_compress: bool = False,
):
    """Bind the three #336 helpers from LoomSession onto an Any-shaped
    stub. Avoids spinning up a full LoomSession (which needs router /
    Discord / Anthropic creds). The helpers only touch episodic /
    semantic / governor / telemetry / threshold / lock / pending list."""
    from loom.core.session import LoomSession

    class _StubMemory:
        class episodic:
            @staticmethod
            async def count_session(_sid: str, *, uncompressed_only: bool = False):
                return ep_count

        semantic = None  # not touched in fake compress

    s = types.SimpleNamespace()
    s.session_id = "sess_compact"
    s._memory = _StubMemory()
    s.router = None
    s.model = "test-model"
    s._governor = None
    s._telemetry = None
    s._episodic_compress_threshold = threshold
    s._compaction_lock = asyncio.Lock()
    s._pending_compactions: list[Any] = []
    s._ledger_store = None  # subscriber path tests set this when needed

    # Track calls via attributes on the stub.
    s.compress_called = 0
    s.refresh_called = 0

    async def _fake_compress(*_a, **_kw):
        s.compress_called += 1
        if raise_in_compress:
            raise RuntimeError("simulated compress_session failure")
        return fact_count

    async def _fake_refresh():
        s.refresh_called += 1

    # Monkey-patch the module-level binding via session_module import path.
    # _run_compaction_check resolves compress_session via the module-level
    # binding — patching the imported reference is sufficient.
    import loom.core.session as session_module

    s._fake_compress = _fake_compress
    s._fake_refresh = _fake_refresh
    s._session_module = session_module

    s._run_compaction_check = LoomSession._run_compaction_check.__get__(s)
    s._refresh_memory_index = _fake_refresh
    s._compaction_subscriber_loop = (
        LoomSession._compaction_subscriber_loop.__get__(s)
    )
    return s


# ---------------------------------------------------------------------------
# _run_compaction_check — threshold gate
# ---------------------------------------------------------------------------


async def test_under_threshold_no_compress_call(monkeypatch) -> None:
    s = _make_stub(ep_count=10, threshold=30)
    monkeypatch.setattr(s._session_module, "compress_session", s._fake_compress)
    await s._run_compaction_check()
    assert s.compress_called == 0
    assert s.refresh_called == 0
    assert s._pending_compactions == []


async def test_at_threshold_compresses_and_buffers_compress_done(
    monkeypatch,
) -> None:
    s = _make_stub(ep_count=30, threshold=30, fact_count=7)
    monkeypatch.setattr(s._session_module, "compress_session", s._fake_compress)
    await s._run_compaction_check()
    assert s.compress_called == 1
    assert s.refresh_called == 1
    assert len(s._pending_compactions) == 1
    assert isinstance(s._pending_compactions[0], CompressDone)
    assert s._pending_compactions[0].fact_count == 7


async def test_zero_facts_no_compress_done_emitted(monkeypatch) -> None:
    """compress_session can return 0 (LLM produced nothing useful);
    nothing to surface to Discord in that case."""
    s = _make_stub(ep_count=50, threshold=30, fact_count=0)
    monkeypatch.setattr(s._session_module, "compress_session", s._fake_compress)
    await s._run_compaction_check()
    assert s.compress_called == 1
    assert s._pending_compactions == []  # 0 facts → no signal


async def test_compress_failure_swallowed_no_pending_compaction(
    monkeypatch,
) -> None:
    """Compression failures must not propagate — turn flow can't break
    on a background subsystem."""
    s = _make_stub(
        ep_count=50, threshold=30, fact_count=4, raise_in_compress=True,
    )
    monkeypatch.setattr(s._session_module, "compress_session", s._fake_compress)
    await s._run_compaction_check()  # must not raise
    assert s.compress_called == 1
    assert s._pending_compactions == []


# ---------------------------------------------------------------------------
# _compaction_lock — serialisation
# ---------------------------------------------------------------------------


async def test_lock_serialises_concurrent_compactions(monkeypatch) -> None:
    """Two near-simultaneous turn_end events must not double-compact.
    Drive _run_compaction_check twice in parallel under the lock and
    confirm one waits for the other."""
    s = _make_stub(ep_count=30, threshold=30, fact_count=2)

    in_flight = 0
    max_in_flight = 0

    async def _slow_compress(*_a, **_kw):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return 2

    monkeypatch.setattr(s._session_module, "compress_session", _slow_compress)

    async def _gated() -> None:
        async with s._compaction_lock:
            await s._run_compaction_check()

    await asyncio.gather(_gated(), _gated())
    assert max_in_flight == 1  # never two parallel compress calls


# ---------------------------------------------------------------------------
# _compaction_subscriber_loop — wired to ledger turn_end events
# ---------------------------------------------------------------------------


async def test_subscriber_triggers_on_turn_end_event(
    monkeypatch, ledger: LedgerStore, emitter: LedgerEmitter,
) -> None:
    s = _make_stub(ep_count=40, threshold=30, fact_count=3)
    s._ledger_store = ledger

    monkeypatch.setattr(s._session_module, "compress_session", s._fake_compress)

    task = asyncio.create_task(s._compaction_subscriber_loop())
    # Give the subscriber a chance to register.
    await asyncio.sleep(0.05)

    async with async_turn_scope("turn_sub"), async_correlation_scope("c1"):
        await emitter.emit_turn_end(
            turn_id="turn_sub",
            payload=TurnEndPayload(
                outcome="clean", duration_ms=100, token_usage={},
            ),
        )

    # Wait for the subscriber to drain and run compaction.
    for _ in range(50):
        if s.compress_called >= 1:
            break
        await asyncio.sleep(0.02)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert s.compress_called == 1
    assert len(s._pending_compactions) == 1


async def test_subscriber_ignores_other_event_types(
    monkeypatch, ledger: LedgerStore, emitter: LedgerEmitter,
) -> None:
    """Subscriber filters event_types=[turn_end]; turn_start / other
    events must not trigger compaction."""
    s = _make_stub(ep_count=40, threshold=30, fact_count=3)
    s._ledger_store = ledger

    monkeypatch.setattr(s._session_module, "compress_session", s._fake_compress)

    task = asyncio.create_task(s._compaction_subscriber_loop())
    await asyncio.sleep(0.05)

    from loom.core.ledger import TurnStartPayload

    async with async_turn_scope("turn_other"), async_correlation_scope("c1"):
        await emitter.emit_turn_start(
            turn_id="turn_other",
            payload=TurnStartPayload(
                prompt_stack_hash="sha256:x", prompt_stack_components={},
            ),
        )

    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert s.compress_called == 0


async def test_subscriber_no_op_without_ledger_store() -> None:
    """If ledger init failed (or [ledger].enabled=false), the subscriber
    loop is a no-op rather than crashing."""
    s = _make_stub(ep_count=40)
    s._ledger_store = None
    # Should return immediately without raising.
    await s._compaction_subscriber_loop()
    assert s.compress_called == 0
