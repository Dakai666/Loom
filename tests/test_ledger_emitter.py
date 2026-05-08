"""Tests for LedgerEmitter + correlation contextvar (#322 commit 1)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from loom.core.ledger import (
    LedgerEmitter,
    LedgerStore,
    ModelEventPayload,
    ToolLifecyclePayload,
    async_correlation_scope,
    correlation_scope,
    current_correlation,
    new_correlation_id,
    reset_correlation,
    set_correlation,
)


@pytest.fixture
async def store(tmp_path: Path) -> LedgerStore:
    s = LedgerStore(
        db_path=tmp_path / "ledger.db",
        blob_dir=tmp_path / "blobs",
    )
    await s.open()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def emitter(store: LedgerStore) -> LedgerEmitter:
    return LedgerEmitter(store, session_id="sess_test")


# ---------------------------------------------------------------------------
# correlation contextvar
# ---------------------------------------------------------------------------


def test_default_correlation_is_none() -> None:
    assert current_correlation() is None


def test_correlation_scope_sets_and_restores() -> None:
    assert current_correlation() is None
    with correlation_scope("c1"):
        assert current_correlation() == "c1"
        with correlation_scope("c2"):
            assert current_correlation() == "c2"
        assert current_correlation() == "c1"
    assert current_correlation() is None


async def test_async_correlation_scope() -> None:
    async with async_correlation_scope("c_async"):
        assert current_correlation() == "c_async"
    assert current_correlation() is None


def test_set_and_reset_correlation() -> None:
    token = set_correlation("c_imp")
    try:
        assert current_correlation() == "c_imp"
    finally:
        reset_correlation(token)
    assert current_correlation() is None


def test_new_correlation_id_unique() -> None:
    a = new_correlation_id()
    b = new_correlation_id()
    assert a != b
    assert a.startswith("corr_")


async def test_correlation_isolated_per_task() -> None:
    """contextvar must not bleed between concurrent asyncio tasks."""
    seen: list[tuple[str, str | None]] = []

    async def worker(name: str, corr: str) -> None:
        async with async_correlation_scope(corr):
            await asyncio.sleep(0.01)
            seen.append((name, current_correlation()))

    await asyncio.gather(
        worker("a", "corr_A"),
        worker("b", "corr_B"),
        worker("c", "corr_C"),
    )
    seen_dict = dict(seen)
    assert seen_dict == {"a": "corr_A", "b": "corr_B", "c": "corr_C"}


# ---------------------------------------------------------------------------
# LedgerEmitter — auto-fill behaviour
# ---------------------------------------------------------------------------


async def test_emit_auto_fills_event_id_timestamp_branch(
    emitter: LedgerEmitter, store: LedgerStore
) -> None:
    payload = ModelEventPayload(
        model="claude-opus-4-7", tier=1, token_usage={"prompt": 10, "completion": 5}
    )
    eid = await emitter.emit_model_event(
        turn_id="t1", payload=payload, correlation_id="c1"
    )
    assert eid.startswith("evt_")

    fetched = await store.fetch_event(eid)
    assert fetched is not None
    assert fetched.session_id == "sess_test"
    assert fetched.branch_id == "main"
    assert fetched.timestamp > 0
    assert fetched.payload["model"] == "claude-opus-4-7"


async def test_emit_uses_contextvar_correlation(
    emitter: LedgerEmitter, store: LedgerStore
) -> None:
    payload = ToolLifecyclePayload(
        phase="BEGIN",
        tool_name="run_bash",
        tool_call_id="call_x",
        args_digest="sha256:x",
    )
    async with async_correlation_scope("ctx_corr"):
        eid = await emitter.emit_tool_lifecycle(turn_id="t1", payload=payload)
    fetched = await store.fetch_event(eid)
    assert fetched.correlation_id == "ctx_corr"


async def test_emit_explicit_correlation_overrides_contextvar(
    emitter: LedgerEmitter, store: LedgerStore
) -> None:
    payload = ToolLifecyclePayload(
        phase="BEGIN",
        tool_name="t",
        tool_call_id="c",
        args_digest="d",
    )
    async with async_correlation_scope("ctx_corr"):
        eid = await emitter.emit_tool_lifecycle(
            turn_id="t1", payload=payload, correlation_id="explicit"
        )
    fetched = await store.fetch_event(eid)
    assert fetched.correlation_id == "explicit"


async def test_emit_orphan_correlation_when_no_scope(
    emitter: LedgerEmitter, store: LedgerStore
) -> None:
    """Emit outside any scope and without explicit corr → fresh orphan id."""
    payload = ModelEventPayload(
        model="m", tier=1, token_usage={"prompt": 0, "completion": 0}
    )
    eid = await emitter.emit_model_event(turn_id="t1", payload=payload)
    fetched = await store.fetch_event(eid)
    assert fetched.correlation_id.startswith("orphan_")


async def test_emit_explicit_event_id_respected(
    emitter: LedgerEmitter, store: LedgerStore
) -> None:
    payload = ModelEventPayload(
        model="m", tier=1, token_usage={"prompt": 0, "completion": 0}
    )
    eid = await emitter.emit_model_event(
        turn_id="t1", payload=payload, correlation_id="c", event_id="my_id_42"
    )
    assert eid == "my_id_42"
    assert (await store.fetch_event("my_id_42")) is not None


async def test_parent_event_id_is_threaded(
    emitter: LedgerEmitter, store: LedgerStore
) -> None:
    payload = ModelEventPayload(
        model="m", tier=1, token_usage={"prompt": 0, "completion": 0}
    )
    parent = await emitter.emit_model_event(
        turn_id="t1", payload=payload, correlation_id="c"
    )
    child = await emitter.emit_model_event(
        turn_id="t1", payload=payload, correlation_id="c", parent_event_id=parent
    )
    fetched = await store.fetch_event(child)
    assert fetched.parent_event_id == parent
