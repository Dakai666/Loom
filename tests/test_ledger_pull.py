"""LedgerStore.events — Pull fluent API (#324 / doc/53 §6.2).

Asserts the chainable EventQuery covers the verbs spec'd in doc/53 §6.2:
where / where_payload / since / until / order_by / limit /
all / first / count / group_by(...).count_by()

And that the raw SQL escape (execute_sql) works for shapes the fluent
API does not cover.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import pytest_asyncio

from loom.core.ledger import (
    JudgeVerdictPayload,
    LedgerEmitter,
    LedgerEvent,
    LedgerStore,
    MemoryOpPayload,
    PermissionDecisionPayload,
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
    return LedgerEmitter(store, session_id="sess_pull")


async def _seed(store: LedgerStore, emitter: LedgerEmitter) -> float:
    """Seed a small mixed event set. Returns base timestamp."""
    base = time.time()
    # Two run_bash tool calls, both with PASS verdicts
    for i, tname in enumerate(["run_bash", "run_bash", "read_file"]):
        await emitter.emit(
            "tool_lifecycle",
            ToolLifecyclePayload(
                phase="BEGIN",
                tool_name=tname,
                tool_call_id=f"call_{i}",
                args_digest=f"sha256:a{i}",
            ),
            turn_id="turn_p",
            correlation_id="c1",
            timestamp=base + i * 0.1,
        )
    await emitter.emit(
        "judge_verdict",
        JudgeVerdictPayload(
            verdict="PASS",
            confidence=0.9,
            reason="ok",
            judged_subject="turn",
        ),
        turn_id="turn_p",
        correlation_id="c1",
        timestamp=base + 1.0,
    )
    await emitter.emit(
        "judge_verdict",
        JudgeVerdictPayload(
            verdict="FAIL",
            confidence=0.8,
            reason="bad",
            judged_subject="turn",
        ),
        turn_id="turn_q",
        correlation_id="c2",
        timestamp=base + 2.0,
    )
    await emitter.emit(
        "memory_op",
        MemoryOpPayload(
            operation="write",
            memory_id="m_a",
            type_summary="semantic_fact",
        ),
        turn_id="turn_p",
        correlation_id="c1",
        timestamp=base + 3.0,
    )
    return base


# ---------------------------------------------------------------------------
# .where() / .all()
# ---------------------------------------------------------------------------


async def test_where_event_type(store: LedgerStore, emitter: LedgerEmitter) -> None:
    await _seed(store, emitter)
    events = await store.events.where(event_type="tool_lifecycle").all()
    assert {e.event_type for e in events} == {"tool_lifecycle"}
    assert len(events) == 3


async def test_where_generated_column_tool_name(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    """tool_name is a generated column — index-eligible."""
    await _seed(store, emitter)
    bash_calls = await store.events.where(tool_name="run_bash").all()
    assert len(bash_calls) == 2
    assert all(e.payload["tool_name"] == "run_bash" for e in bash_calls)


async def test_where_generated_column_verdict(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    await _seed(store, emitter)
    fails = await store.events.where(verdict="FAIL").all()
    assert len(fails) == 1
    assert fails[0].turn_id == "turn_q"


async def test_where_correlation_id_filter(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    await _seed(store, emitter)
    c1_events = await store.events.where(correlation_id="c1").all()
    assert all(e.correlation_id == "c1" for e in c1_events)
    assert len(c1_events) == 5  # 3 tool + 1 judge + 1 memory_op


async def test_unknown_column_raises(store: LedgerStore) -> None:
    with pytest.raises(ValueError):
        store.events.where(not_a_column="x")


# ---------------------------------------------------------------------------
# .where_payload() — json_extract path
# ---------------------------------------------------------------------------


async def test_where_payload_arbitrary_key(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    await _seed(store, emitter)
    # type_summary is in payload but not a generated column
    rows = await store.events.where_payload(type_summary="semantic_fact").all()
    assert len(rows) == 1
    assert rows[0].event_type == "memory_op"


async def test_where_payload_invalid_key_raises(store: LedgerStore) -> None:
    with pytest.raises(ValueError):
        store.events.where_payload(**{"weird-key": "x"})  # hyphen not allowed


# ---------------------------------------------------------------------------
# .since() / .until() — time bounds
# ---------------------------------------------------------------------------


async def test_since_filters(store: LedgerStore, emitter: LedgerEmitter) -> None:
    base = await _seed(store, emitter)
    after = await store.events.since(base + 0.5).all()
    assert all(e.timestamp >= base + 0.5 for e in after)
    assert len(after) == 3  # 2 judge + 1 memory_op


async def test_until_excludes_endpoint(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    base = await _seed(store, emitter)
    before = await store.events.until(base + 1.0).all()
    assert all(e.timestamp < base + 1.0 for e in before)


async def test_since_accepts_datetime(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    from datetime import UTC, datetime

    base = await _seed(store, emitter)
    cutoff = datetime.fromtimestamp(base + 1.5, UTC)
    after = await store.events.since(cutoff).all()
    assert all(e.timestamp >= base + 1.5 for e in after)


# ---------------------------------------------------------------------------
# .order_by() / .limit() / .first()
# ---------------------------------------------------------------------------


async def test_order_by_timestamp_desc(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    await _seed(store, emitter)
    rows = await store.events.order_by("timestamp", desc=True).all()
    timestamps = [r.timestamp for r in rows]
    assert timestamps == sorted(timestamps, reverse=True)


async def test_limit(store: LedgerStore, emitter: LedgerEmitter) -> None:
    await _seed(store, emitter)
    rows = await store.events.limit(2).all()
    assert len(rows) == 2


async def test_first_returns_one_or_none(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    await _seed(store, emitter)
    e = await store.events.where(event_type="memory_op").first()
    assert e is not None
    assert e.event_type == "memory_op"

    none = await store.events.where(event_type="thought").first()
    assert none is None


async def test_order_by_unknown_column_raises(store: LedgerStore) -> None:
    with pytest.raises(ValueError):
        store.events.order_by("payload")  # not whitelisted


# ---------------------------------------------------------------------------
# .count() / group_by().count_by()
# ---------------------------------------------------------------------------


async def test_count(store: LedgerStore, emitter: LedgerEmitter) -> None:
    await _seed(store, emitter)
    total = await store.events.count()
    assert total == 6
    bash_count = await store.events.where(tool_name="run_bash").count()
    assert bash_count == 2


async def test_group_by_count_by(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    await _seed(store, emitter)
    by_type = await store.events.group_by("event_type").count_by()
    assert by_type == {"tool_lifecycle": 3, "judge_verdict": 2, "memory_op": 1}


async def test_group_by_with_filter(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    await _seed(store, emitter)
    by_verdict = await (
        store.events.where(event_type="judge_verdict")
        .group_by("verdict")
        .count_by()
    )
    assert by_verdict == {"PASS": 1, "FAIL": 1}


# ---------------------------------------------------------------------------
# Immutability — chaining returns new instances
# ---------------------------------------------------------------------------


async def test_query_is_immutable(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    await _seed(store, emitter)
    base = store.events.where(event_type="tool_lifecycle")
    bash_only = base.where(tool_name="run_bash")
    # base is unaffected by the chained where
    base_count = await base.count()
    bash_count = await bash_only.count()
    assert base_count == 3
    assert bash_count == 2


# ---------------------------------------------------------------------------
# branch filter — default 'main'; on_branch overrides; None lifts the filter
# ---------------------------------------------------------------------------


async def test_branch_filter_default_main(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    """Default emit goes to 'main' branch; default query filters to it."""
    await _seed(store, emitter)
    rows = await store.events.all()
    assert all(e.branch_id == "main" for e in rows)


async def test_on_branch_none_lifts_filter(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    """on_branch(None) returns events from any branch (cross-branch query)."""
    # Even with no non-main branches in the store, the query should still
    # work — just returns the same as the default in this case.
    await _seed(store, emitter)
    rows = await store.events.on_branch(None).all()
    assert len(rows) == 6


# ---------------------------------------------------------------------------
# Raw SQL escape hatch (§6.2)
# ---------------------------------------------------------------------------


async def test_execute_sql_raw_aggregate(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    """Use case from doc/53 §6.2 — 30-day deny-rate per tool."""
    base = await _seed(store, emitter)
    # Add some permission_decisions
    await emitter.emit(
        "permission_decision",
        PermissionDecisionPayload(
            decision="deny",
            tool_call_id="x",
            trust_level="guarded",
            reason="user denied",
        ),
        turn_id="turn_p",
        correlation_id="c1",
        timestamp=base + 4.0,
    )

    rows = await store.execute_sql(
        """
        SELECT json_extract(payload, '$.decision') as decision, COUNT(*) as n
        FROM events
        WHERE event_type='permission_decision'
        GROUP BY decision
        """
    )
    assert dict(rows) == {"deny": 1}


async def test_execute_sql_with_params(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    base = await _seed(store, emitter)
    rows = await store.execute_sql(
        "SELECT COUNT(*) FROM events WHERE timestamp>=?",
        (base + 1.5,),
    )
    assert rows[0][0] >= 1
