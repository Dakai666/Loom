"""Tests for loom.core.ledger storage layer (#321 / doc/53 §5).

Pure-storage tests — no emitter coupling. asyncio_mode=auto in pyproject.toml
means no @pytest.mark.asyncio decorator needed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from loom.core.ledger import (
    DEFAULT_BRANCH,
    LedgerEvent,
    LedgerStore,
    MemoryOpPayload,
    THOUGHT_EXTERNAL_THRESHOLD,
    ToolLifecyclePayload,
)


@pytest.fixture
async def store(tmp_path: Path) -> LedgerStore:
    s = LedgerStore(
        db_path=tmp_path / "ledger.db",
        blob_dir=tmp_path / "ledger_blobs",
    )
    await s.open()
    try:
        yield s
    finally:
        await s.close()


def _evt(
    *,
    event_id: str,
    turn_id: str = "turn_1",
    correlation_id: str = "corr_1",
    event_type: str = "tool_lifecycle",
    payload: dict | object | None = None,
    parent_event_id: str | None = None,
    branch_id: str = DEFAULT_BRANCH,
    session_id: str = "sess_1",
    timestamp: float | None = None,
) -> LedgerEvent:
    return LedgerEvent(
        event_id=event_id,
        session_id=session_id,
        turn_id=turn_id,
        parent_event_id=parent_event_id,
        correlation_id=correlation_id,
        branch_id=branch_id,
        event_type=event_type,
        timestamp=timestamp if timestamp is not None else time.time(),
        payload=payload if payload is not None else {"schema_version": 1},
    )


# ---------------------------------------------------------------------------
# emit / query roundtrip
# ---------------------------------------------------------------------------


async def test_emit_and_fetch_roundtrip(store: LedgerStore) -> None:
    payload = ToolLifecyclePayload(
        phase="BEGIN",
        tool_name="run_bash",
        tool_call_id="call_1",
        args_digest="sha256:abc",
        args={"cmd": "ls"},
    )
    evt = _evt(event_id="e1", event_type="tool_lifecycle", payload=payload)
    await store.emit(evt)

    fetched = await store.fetch_event("e1")
    assert fetched is not None
    assert fetched.event_id == "e1"
    assert fetched.event_type == "tool_lifecycle"
    assert fetched.branch_id == "main"
    assert fetched.payload["tool_name"] == "run_bash"
    assert fetched.payload["schema_version"] == 1


async def test_fetch_by_turn_orders_by_timestamp(store: LedgerStore) -> None:
    base = time.time()
    await store.emit(_evt(event_id="b", timestamp=base + 1.0))
    await store.emit(_evt(event_id="a", timestamp=base + 0.0))
    await store.emit(_evt(event_id="c", timestamp=base + 2.0))

    events = await store.fetch_by_turn("turn_1")
    assert [e.event_id for e in events] == ["a", "b", "c"]


async def test_emit_rejects_duplicate_event_id(store: LedgerStore) -> None:
    await store.emit(_evt(event_id="dup"))
    with pytest.raises(Exception):
        # sqlite3.IntegrityError surfaces through aiosqlite
        await store.emit(_evt(event_id="dup"))


async def test_emit_accepts_dict_payload(store: LedgerStore) -> None:
    """Caller may pass a plain dict instead of a dataclass."""
    await store.emit(
        _evt(
            event_id="raw",
            event_type="env_observation",
            payload={
                "schema_version": 1,
                "observation_type": "anomaly",
                "source": "memory_pulse",
                "detail": {"note": "ok"},
            },
        )
    )
    got = await store.fetch_event("raw")
    assert got.payload["observation_type"] == "anomaly"


# ---------------------------------------------------------------------------
# Index hit (EXPLAIN QUERY PLAN)
# ---------------------------------------------------------------------------


async def test_turn_index_used(store: LedgerStore) -> None:
    plan = await store.explain_query_plan(
        "SELECT * FROM events WHERE branch_id=? AND turn_id=? ORDER BY timestamp",
        ("main", "turn_1"),
    )
    plan_text = " ".join(plan)
    assert "idx_turn" in plan_text, plan_text


async def test_correlation_index_used(store: LedgerStore) -> None:
    plan = await store.explain_query_plan(
        "SELECT * FROM events WHERE branch_id=? AND correlation_id=? ORDER BY timestamp",
        ("main", "corr_1"),
    )
    assert "idx_correlation" in " ".join(plan)


async def test_session_recent_index_used(store: LedgerStore) -> None:
    plan = await store.explain_query_plan(
        "SELECT * FROM events WHERE branch_id=? AND session_id=? ORDER BY timestamp DESC",
        ("main", "sess_1"),
    )
    assert "idx_session_recent" in " ".join(plan)


async def test_tool_index_used(store: LedgerStore) -> None:
    plan = await store.explain_query_plan(
        "SELECT * FROM events WHERE branch_id=? AND tool_name=? ORDER BY timestamp",
        ("main", "run_bash"),
    )
    assert "idx_tool" in " ".join(plan)


async def test_predecessor_index_used(store: LedgerStore) -> None:
    plan = await store.explain_query_plan(
        "SELECT * FROM events WHERE predecessor_memory_id=?",
        ("mem_abc",),
    )
    assert "idx_predecessor" in " ".join(plan)


# ---------------------------------------------------------------------------
# schema_version preserved
# ---------------------------------------------------------------------------


async def test_schema_version_preserved_through_roundtrip(
    store: LedgerStore,
) -> None:
    payload = {
        "schema_version": 7,  # imagine some future version
        "tool_name": "future_tool",
    }
    await store.emit(_evt(event_id="v7", payload=payload))
    fetched = await store.fetch_event("v7")
    assert fetched.payload["schema_version"] == 7


# ---------------------------------------------------------------------------
# Thought blob threshold (§3.3)
# ---------------------------------------------------------------------------


async def test_thought_inline_when_under_threshold(store: LedgerStore) -> None:
    text = "small thought"
    full_text, ext, digest = store.store_thought_text(
        text, turn_id="turn_x", event_id="evt_y"
    )
    assert full_text == text
    assert ext is None
    assert len(digest) == 64  # sha256 hex


async def test_thought_external_when_over_threshold(
    store: LedgerStore, tmp_path: Path
) -> None:
    text = "x" * (THOUGHT_EXTERNAL_THRESHOLD + 1)
    full_text, ext, digest = store.store_thought_text(
        text, turn_id="turn_x", event_id="evt_y"
    )
    assert full_text is None
    assert ext == "turn_x/evt_y.txt"
    blob = store.blob_dir / ext
    assert blob.exists()
    assert blob.read_text(encoding="utf-8") == text
    assert len(digest) == 64


async def test_thought_payload_helper_round_trip(store: LedgerStore) -> None:
    payload = store.build_thought_payload(
        "hello",
        turn_id="t1",
        event_id="e1",
        duration_ms=12,
        produced_tool_calls=0,
    )
    assert payload.full_text == "hello"
    assert payload.external_ref is None
    assert payload.duration_ms == 12


async def test_update_thought_full_text_late_arrival(store: LedgerStore) -> None:
    # Initial emit with full_text=None (buffered capture path).
    await store.emit(
        _evt(
            event_id="th1",
            event_type="thought",
            payload={
                "schema_version": 1,
                "digest": "sha256:placeholder",
                "duration_ms": 5,
                "produced_tool_calls": 0,
                "full_text": None,
                "external_ref": None,
            },
        )
    )
    await store.update_thought_full_text("th1", "the actual text", turn_id="turn_1")

    fetched = await store.fetch_event("th1")
    assert fetched.payload["full_text"] == "the actual text"
    assert fetched.payload["external_ref"] is None
    assert fetched.payload["digest"].startswith(("sha256:",)) or len(
        fetched.payload["digest"]
    ) == 64


async def test_update_thought_unknown_event_raises(store: LedgerStore) -> None:
    with pytest.raises(KeyError):
        await store.update_thought_full_text("nope", "x", turn_id="t")


# ---------------------------------------------------------------------------
# resolve_memory_id walks compaction chain (§5.7)
# ---------------------------------------------------------------------------


async def _emit_compact(
    store: LedgerStore,
    *,
    event_id: str,
    predecessor: str,
    successor: str,
    timestamp: float,
) -> None:
    payload = MemoryOpPayload(
        operation="compact",
        memory_id=successor,
        predecessor_memory_id=predecessor,
        successor_memory_id=successor,
        type_summary="semantic_triple",
        trust_tier="user_explicit",
        content_digest="sha256:" + successor,
    )
    await store.emit(
        _evt(
            event_id=event_id,
            event_type="memory_op",
            payload=payload,
            timestamp=timestamp,
        )
    )


async def test_resolve_memory_id_walks_chain(store: LedgerStore) -> None:
    base = time.time()
    await _emit_compact(
        store, event_id="c1", predecessor="m_a", successor="m_b", timestamp=base
    )
    await _emit_compact(
        store, event_id="c2", predecessor="m_b", successor="m_c", timestamp=base + 1
    )
    await _emit_compact(
        store, event_id="c3", predecessor="m_c", successor="m_d", timestamp=base + 2
    )

    assert await store.resolve_memory_id("m_a") == "m_d"
    assert await store.resolve_memory_id("m_b") == "m_d"
    assert await store.resolve_memory_id("m_d") == "m_d"
    assert await store.resolve_memory_id("m_unknown") == "m_unknown"


async def test_resolve_memory_id_ignores_non_compact(store: LedgerStore) -> None:
    payload = MemoryOpPayload(
        operation="write",
        memory_id="m_x",
        predecessor_memory_id="m_a",  # write events never set this in practice,
        successor_memory_id="m_x",  # but we defend against accidental injection
        content_digest="sha256:x",
    )
    await store.emit(_evt(event_id="w1", event_type="memory_op", payload=payload))
    # Because operation != "compact", resolve should not follow it.
    assert await store.resolve_memory_id("m_a") == "m_a"


# ---------------------------------------------------------------------------
# Maintenance hook smoke
# ---------------------------------------------------------------------------


async def test_maintenance_runs_clean(store: LedgerStore) -> None:
    await store.emit(_evt(event_id="m1"))
    await store.maintenance()  # must not raise
    assert (await store.fetch_event("m1")) is not None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_emit_before_open_raises(tmp_path: Path) -> None:
    s = LedgerStore(db_path=tmp_path / "x.db", blob_dir=tmp_path / "blobs")
    with pytest.raises(RuntimeError):
        await s.emit(_evt(event_id="e"))


async def test_async_context_manager_creates_files(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    blobs = tmp_path / "ledger_blobs"
    async with LedgerStore(db_path=db, blob_dir=blobs):
        pass
    assert db.exists()
    assert blobs.exists()
