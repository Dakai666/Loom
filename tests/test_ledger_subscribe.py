"""LedgerStore.subscribe — Push API (#324 / doc/53 §6.1, §6.5).

Asserts:
- live event delivery to async iterator subscribers
- multi-subscriber fan-out (each gets all matching events)
- event_types / correlation_id / branch_id / session_id filters
- bounded buffer + drop-oldest with is_live=False after drop
- replay_from atomic handoff (historical pulled, then live, no gaps,
  no duplicates)
- subscriber close cleans up registration
- read-only boundary (subscriber holds no mutator API)
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
import pytest_asyncio

from loom.core.ledger import (
    JudgeVerdictPayload,
    LedgerEmitter,
    LedgerEvent,
    LedgerStore,
    ToolLifecyclePayload,
)


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> LedgerStore:
    s = LedgerStore(
        db_path=tmp_path / "ledger.db", blob_dir=tmp_path / "blobs"
    )
    await s.open()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
def emitter(store: LedgerStore) -> LedgerEmitter:
    return LedgerEmitter(store, session_id="sess_sub")


def _tlc(call_id: str = "c") -> ToolLifecyclePayload:
    return ToolLifecyclePayload(
        phase="BEGIN",
        tool_name="run_bash",
        tool_call_id=call_id,
        args_digest="sha256:x",
    )


def _jv(verdict: str = "PASS") -> JudgeVerdictPayload:
    return JudgeVerdictPayload(
        verdict=verdict, confidence=0.9, reason="ok", judged_subject="turn"
    )


async def _drain(sub, n: int, timeout: float = 1.0) -> list[LedgerEvent]:
    """Read n events from sub or raise on timeout."""
    out: list[LedgerEvent] = []

    async def _read():
        async for ev in sub:
            out.append(ev)
            if len(out) >= n:
                break

    await asyncio.wait_for(_read(), timeout=timeout)
    return out


# ---------------------------------------------------------------------------
# Live delivery
# ---------------------------------------------------------------------------


async def test_live_delivery(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    async with store.subscribe() as sub:
        for i in range(3):
            await emitter.emit(
                "tool_lifecycle",
                _tlc(f"call_{i}"),
                turn_id="t1",
                correlation_id="c1",
            )
        events = await _drain(sub, 3)
    assert [e.payload["tool_call_id"] for e in events] == [
        "call_0",
        "call_1",
        "call_2",
    ]
    assert sub.is_live is True
    assert sub.last_event_timestamp is not None


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


async def test_event_types_filter(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    async with store.subscribe(event_types=["judge_verdict"]) as sub:
        await emitter.emit(
            "tool_lifecycle", _tlc(), turn_id="t1", correlation_id="c1"
        )
        await emitter.emit(
            "judge_verdict", _jv(), turn_id="t1", correlation_id="c1"
        )
        events = await _drain(sub, 1)
    assert len(events) == 1
    assert events[0].event_type == "judge_verdict"


async def test_correlation_id_filter(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    async with store.subscribe(correlation_id="c_target") as sub:
        await emitter.emit(
            "tool_lifecycle", _tlc("c0"), turn_id="t1", correlation_id="c_other"
        )
        await emitter.emit(
            "tool_lifecycle", _tlc("c1"), turn_id="t1", correlation_id="c_target"
        )
        events = await _drain(sub, 1)
    assert events[0].payload["tool_call_id"] == "c1"


async def test_session_id_filter(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    other_emitter = LedgerEmitter(store, session_id="sess_other")
    async with store.subscribe(session_id="sess_sub") as sub:
        await other_emitter.emit(
            "tool_lifecycle", _tlc("o"), turn_id="t1", correlation_id="c1"
        )
        await emitter.emit(
            "tool_lifecycle", _tlc("m"), turn_id="t1", correlation_id="c1"
        )
        events = await _drain(sub, 1)
    assert events[0].session_id == "sess_sub"
    assert events[0].payload["tool_call_id"] == "m"


async def test_branch_filter_default_main(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    """Default branch filter is 'main'; non-main events should not appear."""
    branch_emitter = LedgerEmitter(
        store, session_id="sess_sub", branch_id="alt_001"
    )
    async with store.subscribe() as sub:
        await branch_emitter.emit(
            "tool_lifecycle", _tlc("alt"), turn_id="t1", correlation_id="c1"
        )
        await emitter.emit(
            "tool_lifecycle", _tlc("main"), turn_id="t1", correlation_id="c1"
        )
        events = await _drain(sub, 1)
    assert events[0].payload["tool_call_id"] == "main"


async def test_branch_filter_none_lifts(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    branch_emitter = LedgerEmitter(
        store, session_id="sess_sub", branch_id="alt_001"
    )
    async with store.subscribe(branch_id=None) as sub:
        await branch_emitter.emit(
            "tool_lifecycle", _tlc("alt"), turn_id="t1", correlation_id="c1"
        )
        await emitter.emit(
            "tool_lifecycle", _tlc("main"), turn_id="t1", correlation_id="c1"
        )
        events = await _drain(sub, 2)
    assert {e.branch_id for e in events} == {"alt_001", "main"}


# ---------------------------------------------------------------------------
# Multi-subscriber fan-out
# ---------------------------------------------------------------------------


async def test_multi_subscriber_fanout(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    async with store.subscribe() as sub_a, store.subscribe() as sub_b:
        await emitter.emit(
            "tool_lifecycle", _tlc("c0"), turn_id="t1", correlation_id="c1"
        )
        a = await _drain(sub_a, 1)
        b = await _drain(sub_b, 1)
    assert a[0].event_id == b[0].event_id


# ---------------------------------------------------------------------------
# Bounded buffer + drop-oldest + is_live
# ---------------------------------------------------------------------------


async def test_drop_oldest_when_buffer_full(
    store: LedgerStore, emitter: LedgerEmitter, caplog
) -> None:
    """With buffer_size=3 and 5 emits before consumer reads, the
    subscriber sees only the LAST 3 events (oldest 2 dropped)."""
    import logging

    caplog.set_level(logging.WARNING, logger="loom.core.ledger.subscriber")

    async with store.subscribe(buffer_size=3) as sub:
        for i in range(5):
            await emitter.emit(
                "tool_lifecycle",
                _tlc(f"call_{i}"),
                turn_id="t1",
                correlation_id="c1",
            )
        assert sub.is_live is False  # drops occurred
        assert sub.dropped_total == 2
        events = await _drain(sub, 3)

    seen_ids = [e.payload["tool_call_id"] for e in events]
    assert seen_ids == ["call_2", "call_3", "call_4"]
    # Warning log emitted at least once
    assert any(
        "dropped event" in r.message for r in caplog.records
    )


async def test_is_live_starts_true(store: LedgerStore) -> None:
    async with store.subscribe() as sub:
        assert sub.is_live is True
        assert sub.lag_events == 0


async def test_is_live_stays_false_after_drop(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    """Once a drop occurs, is_live is False for the rest of this
    subscriber's lifetime — drops are facts of history."""
    async with store.subscribe(buffer_size=2) as sub:
        for i in range(4):
            await emitter.emit(
                "tool_lifecycle",
                _tlc(f"c{i}"),
                turn_id="t1",
                correlation_id="c1",
            )
        await _drain(sub, 2)  # drain everything
        assert sub.lag_events == 0
        assert sub.is_live is False  # but still False — drops are permanent


# ---------------------------------------------------------------------------
# replay_from — atomic handoff
# ---------------------------------------------------------------------------


async def test_replay_from_pulls_history_then_live(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    base = time.time()
    # Three historical events
    for i in range(3):
        await emitter.emit(
            "tool_lifecycle",
            _tlc(f"hist_{i}"),
            turn_id="t1",
            correlation_id="c1",
            timestamp=base + i * 0.1,
        )

    async with store.subscribe(replay_from=base) as sub:
        # Two more after subscribe
        for i in range(2):
            await emitter.emit(
                "tool_lifecycle",
                _tlc(f"live_{i}"),
                turn_id="t1",
                correlation_id="c1",
                timestamp=base + 1.0 + i * 0.1,
            )
        events = await _drain(sub, 5)

    call_ids = [e.payload["tool_call_id"] for e in events]
    assert call_ids == ["hist_0", "hist_1", "hist_2", "live_0", "live_1"]


async def test_replay_from_filtered_by_event_types(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    """Historical events must also pass through the subscriber filters."""
    base = time.time()
    await emitter.emit(
        "tool_lifecycle",
        _tlc("h"),
        turn_id="t1",
        correlation_id="c1",
        timestamp=base,
    )
    await emitter.emit(
        "judge_verdict",
        _jv(),
        turn_id="t1",
        correlation_id="c1",
        timestamp=base + 0.1,
    )

    async with store.subscribe(
        event_types=["judge_verdict"], replay_from=base
    ) as sub:
        events = await _drain(sub, 1)
    assert len(events) == 1
    assert events[0].event_type == "judge_verdict"


async def test_replay_from_no_duplicates_at_boundary(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    """Events emitted concurrently with subscribe must appear exactly once."""
    base = time.time()
    for i in range(3):
        await emitter.emit(
            "tool_lifecycle",
            _tlc(f"h{i}"),
            turn_id="t1",
            correlation_id="c1",
            timestamp=base + i * 0.1,
        )

    async with store.subscribe(replay_from=base) as sub:
        await emitter.emit(
            "tool_lifecycle",
            _tlc("live_after"),
            turn_id="t1",
            correlation_id="c1",
        )
        events = await _drain(sub, 4)

    ids = [e.event_id for e in events]
    assert len(ids) == len(set(ids))  # no duplicates


# ---------------------------------------------------------------------------
# Cleanup — closing the subscriber unregisters from store
# ---------------------------------------------------------------------------


async def test_close_unregisters_subscriber(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    assert store._subscribers == []
    async with store.subscribe() as sub:
        assert sub in store._subscribers
    assert sub not in store._subscribers


async def test_emit_to_no_subscribers_is_silent(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    """No subscribers → emit just writes to DB and returns."""
    await emitter.emit(
        "tool_lifecycle", _tlc(), turn_id="t1", correlation_id="c1"
    )
    # No exception, event is in store
    rows = await store.fetch_by_turn("t1")
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Read-only boundary (§6.5)
# ---------------------------------------------------------------------------


async def test_subscriber_has_no_mutator_api(store: LedgerStore) -> None:
    async with store.subscribe() as sub:
        # Spec: no emit / write / cancel / mutate methods.
        for forbidden in ("emit", "write", "cancel", "modify", "abort"):
            assert not hasattr(sub, forbidden), (
                f"LedgerSubscriber must not expose mutator: {forbidden}"
            )
