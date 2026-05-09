"""LedgerEnvelopeProjector — tool batch view from ledger (#325 commit 1).

Builds synthetic ledger entries and asserts the projector produces an
ExecutionEnvelopeView equivalent to what the legacy
``LoomSession._build_envelope_view`` would have produced from
``ExecutionEnvelope.records``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import pytest_asyncio

from loom.core.ledger import (
    CallMeta,
    LedgerEmitter,
    LedgerEnvelopeProjector,
    LedgerStore,
    ToolLifecyclePayload,
    TurnStartPayload,
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
    return LedgerEmitter(store, session_id="sess_proj")


@pytest_asyncio.fixture
def projector(store: LedgerStore) -> LedgerEnvelopeProjector:
    # No registry — capabilities/trust_level fall back to defaults.
    return LedgerEnvelopeProjector(store, registry=None)


async def _emit_lifecycle_pair(
    emitter: LedgerEmitter,
    *,
    call_id: str,
    tool_name: str,
    turn_id: str,
    timestamps: tuple[float, float],
    rolled_back: bool = False,
    error: str | None = None,
    result_summary: str | None = "ok",
) -> None:
    begin_ts, end_ts = timestamps
    await emitter.emit(
        "tool_lifecycle",
        ToolLifecyclePayload(
            phase="BEGIN",
            tool_name=tool_name,
            tool_call_id=call_id,
            args_digest=f"sha256:{call_id}",
        ),
        turn_id=turn_id,
        correlation_id="c1",
        timestamp=begin_ts,
    )
    state_transitions = (
        [
            {"from": "declared", "to": "authorized", "ts": "x", "reason": None},
            {"from": "authorized", "to": "executing", "ts": "x", "reason": None},
            {"from": "executing", "to": "observed", "ts": "x", "reason": None},
            {"from": "observed", "to": "validated", "ts": "x", "reason": None},
        ]
        + (
            [
                {"from": "validated", "to": "reverting", "ts": "x", "reason": None},
                {"from": "reverting", "to": "reverted", "ts": "x", "reason": None},
                {"from": "reverted", "to": "memorialized", "ts": "x", "reason": None},
            ]
            if rolled_back
            else [
                {"from": "validated", "to": "committed", "ts": "x", "reason": None},
                {"from": "committed", "to": "memorialized", "ts": "x", "reason": None},
            ]
        )
    )
    await emitter.emit(
        "tool_lifecycle",
        ToolLifecyclePayload(
            phase="END",
            tool_name=tool_name,
            tool_call_id=call_id,
            args_digest=f"sha256:{call_id}",
            result_summary=result_summary,
            state_history=state_transitions,
            rolled_back=rolled_back,
            error=error,
        ),
        turn_id=turn_id,
        correlation_id="c1",
        timestamp=end_ts,
    )


# ---------------------------------------------------------------------------
# Single-call batch
# ---------------------------------------------------------------------------


async def test_completed_batch_single_call(
    store: LedgerStore, emitter: LedgerEmitter, projector
) -> None:
    base = time.time()
    await emitter.emit(
        "turn_start",
        TurnStartPayload(
            prompt_stack_hash="sha256:p",
            prompt_stack_components={"persona": "x", "tool_catalog_size": 1},
        ),
        turn_id="turn_p",
        correlation_id="c1",
        timestamp=base,
    )
    await _emit_lifecycle_pair(
        emitter,
        call_id="call_1",
        tool_name="run_bash",
        turn_id="turn_p",
        timestamps=(base + 0.1, base + 0.5),
    )

    view = await projector.build_view(
        envelope_id="e1",
        session_id="sess_proj",
        turn_id="turn_p",
        turn_index=0,
        call_ids=["call_1"],
        call_meta={
            "call_1": CallMeta(
                call_id="call_1",
                tool_name="run_bash",
                full_args={"cmd": "ls"},
                args_preview="ls",
                auth_decision="auto",
            )
        },
    )

    assert view.envelope_id == "e1"
    assert view.status == "completed"
    assert view.node_count == 1
    n = view.nodes[0]
    assert n.tool_name == "run_bash"
    assert n.state == "memorialized"
    assert n.args_preview == "ls"
    assert n.full_args == {"cmd": "ls"}
    assert n.auth_decision == "auto"
    assert n.duration_ms == pytest.approx(400, abs=5)
    assert n.output_preview == "ok"
    # state_history is the bundled lifecycle transitions
    assert any(t["to"] == "memorialized" for t in n.state_history)


# ---------------------------------------------------------------------------
# Failed call (rolled back) → status="failed"
# ---------------------------------------------------------------------------


async def test_failed_batch_status(
    store: LedgerStore, emitter: LedgerEmitter, projector
) -> None:
    base = time.time()
    await _emit_lifecycle_pair(
        emitter,
        call_id="call_x",
        tool_name="run_bash",
        turn_id="turn_p",
        timestamps=(base, base + 0.3),
        rolled_back=True,
        error="rejected by post-validator",
    )

    view = await projector.build_view(
        envelope_id="e2",
        session_id="sess_proj",
        turn_id="turn_p",
        turn_index=0,
        call_ids=["call_x"],
        call_meta={
            "call_x": CallMeta(call_id="call_x", tool_name="run_bash"),
        },
    )

    assert view.status == "failed"
    n = view.nodes[0]
    assert n.state == "memorialized"
    # error_snippet truncates at 80
    assert "rejected" in n.error_snippet


# ---------------------------------------------------------------------------
# Parallel batch — mid-execution → status="running"
# ---------------------------------------------------------------------------


async def test_parallel_batch_one_running_one_done(
    store: LedgerStore, emitter: LedgerEmitter, projector
) -> None:
    base = time.time()
    # Call A finishes
    await _emit_lifecycle_pair(
        emitter,
        call_id="call_a",
        tool_name="read_file",
        turn_id="turn_p",
        timestamps=(base, base + 0.2),
    )
    # Call B only has BEGIN
    await emitter.emit(
        "tool_lifecycle",
        ToolLifecyclePayload(
            phase="BEGIN",
            tool_name="run_bash",
            tool_call_id="call_b",
            args_digest="sha256:b",
        ),
        turn_id="turn_p",
        correlation_id="c1",
        timestamp=base + 0.1,
    )

    view = await projector.build_view(
        envelope_id="e3",
        session_id="sess_proj",
        turn_id="turn_p",
        turn_index=0,
        call_ids=["call_a", "call_b"],
        call_meta={
            "call_a": CallMeta(call_id="call_a", tool_name="read_file"),
            "call_b": CallMeta(call_id="call_b", tool_name="run_bash"),
        },
    )

    assert view.status == "running"
    states = {n.tool_name: n.state for n in view.nodes}
    assert states["read_file"] == "memorialized"
    # Call B has only BEGIN → "executing" derivation
    assert states["run_bash"] == "executing"


# ---------------------------------------------------------------------------
# In-flight state coarsening (#337 — replaced live_record_lookup)
# ---------------------------------------------------------------------------


async def test_in_flight_call_reports_executing_state(
    store: LedgerStore, emitter: LedgerEmitter, projector
) -> None:
    """#337 — BEGIN seen, no END yet → state coarsens to 'executing'.

    Replaces the prior live_record_lookup-based test. The bridge that
    surfaced sub-states (PENDING / AUTHORIZED / awaiting_confirm /
    EXECUTING) is gone; the projector reads BEGIN / END only and
    falls back to 'executing' for in-flight calls. Sub-state
    granularity is not user-visible — see envelope_view.py docstring.
    """
    base = time.time()
    await emitter.emit(
        "tool_lifecycle",
        ToolLifecyclePayload(
            phase="BEGIN",
            tool_name="run_bash",
            tool_call_id="call_live",
            args_digest="sha256:l",
        ),
        turn_id="turn_p",
        correlation_id="c1",
        timestamp=base,
    )

    view = await projector.build_view(
        envelope_id="e4",
        session_id="sess_proj",
        turn_id="turn_p",
        turn_index=0,
        call_ids=["call_live"],
        call_meta={
            "call_live": CallMeta(call_id="call_live", tool_name="run_bash")
        },
    )

    assert view.nodes[0].state == "executing"
    assert view.status == "running"


# ---------------------------------------------------------------------------
# auth_expires_lookup
# ---------------------------------------------------------------------------


async def test_auth_expires_threaded_through_callable(
    store: LedgerStore, emitter: LedgerEmitter, projector
) -> None:
    base = time.time()
    await _emit_lifecycle_pair(
        emitter,
        call_id="call_e",
        tool_name="write_file",
        turn_id="turn_p",
        timestamps=(base, base + 0.1),
    )

    expiry = base + 600
    view = await projector.build_view(
        envelope_id="e5",
        session_id="sess_proj",
        turn_id="turn_p",
        turn_index=0,
        call_ids=["call_e"],
        call_meta={"call_e": CallMeta(call_id="call_e", tool_name="write_file")},
        auth_expires_lookup=lambda cid: expiry if cid == "call_e" else 0.0,
    )
    assert view.nodes[0].auth_expires == expiry


# ---------------------------------------------------------------------------
# Placeholder for registered-but-not-emitted call
# ---------------------------------------------------------------------------


async def test_call_id_with_no_lifecycle_event_yet(
    store: LedgerStore, emitter: LedgerEmitter, projector
) -> None:
    """Caller registers a call_id but no BEGIN has landed (race window)."""
    view = await projector.build_view(
        envelope_id="e6",
        session_id="sess_proj",
        turn_id="turn_empty",
        turn_index=0,
        call_ids=["call_pending"],
        call_meta={
            "call_pending": CallMeta(
                call_id="call_pending",
                tool_name="run_bash",
                args_preview="ls",
            )
        },
    )
    assert view.node_count == 1
    n = view.nodes[0]
    assert n.state == "declared"
    assert n.tool_name == "run_bash"
    assert n.args_preview == "ls"


# ---------------------------------------------------------------------------
# Ordering — calls appear in dispatch order (first-seen ts)
# ---------------------------------------------------------------------------


async def test_calls_ordered_by_first_seen(
    store: LedgerStore, emitter: LedgerEmitter, projector
) -> None:
    base = time.time()
    await _emit_lifecycle_pair(
        emitter,
        call_id="call_2nd",
        tool_name="t",
        turn_id="turn_p",
        timestamps=(base, base + 0.5),
    )
    await _emit_lifecycle_pair(
        emitter,
        call_id="call_1st",
        tool_name="t",
        turn_id="turn_p",
        timestamps=(base + 0.1, base + 0.6),
    )

    view = await projector.build_view(
        envelope_id="e7",
        session_id="sess_proj",
        turn_id="turn_p",
        turn_index=0,
        call_ids=["call_2nd", "call_1st"],
        call_meta={
            "call_2nd": CallMeta(call_id="call_2nd", tool_name="t"),
            "call_1st": CallMeta(call_id="call_1st", tool_name="t"),
        },
    )
    assert [n.call_id for n in view.nodes] == ["call_2nd", "call_1st"]
    assert view.levels == [["call_2nd", "call_1st"]]


# ---------------------------------------------------------------------------
# Empty envelope (no call_ids registered yet)
# ---------------------------------------------------------------------------


async def test_empty_envelope_running_status(
    store: LedgerStore, projector
) -> None:
    view = await projector.build_view(
        envelope_id="e_empty",
        session_id="sess_proj",
        turn_id="turn_unknown",
        turn_index=0,
        call_ids=[],
        call_meta={},
    )
    assert view.status == "running"
    assert view.node_count == 0
    assert view.nodes == []
    assert view.levels == []


# ---------------------------------------------------------------------------
# elapsed_ms uses batch_t0 from monotonic clock
# ---------------------------------------------------------------------------


async def test_elapsed_ms_from_batch_t0(
    store: LedgerStore, emitter: LedgerEmitter, projector
) -> None:
    base = time.time()
    await _emit_lifecycle_pair(
        emitter,
        call_id="c",
        tool_name="t",
        turn_id="turn_p",
        timestamps=(base, base + 0.05),
    )
    batch_t0 = time.monotonic() - 1.0  # pretend batch started 1s ago
    view = await projector.build_view(
        envelope_id="e8",
        session_id="sess_proj",
        turn_id="turn_p",
        turn_index=0,
        call_ids=["c"],
        call_meta={"c": CallMeta(call_id="c", tool_name="t")},
        batch_t0=batch_t0,
    )
    assert view.elapsed_ms >= 1000  # at least 1s
