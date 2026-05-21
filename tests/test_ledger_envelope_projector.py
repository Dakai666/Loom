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
    failure_state: str | None = None,
) -> None:
    """Emit a BEGIN/END pair for a tool call.

    ``failure_state`` mirrors the Loom lifecycle for non-rollback
    failure paths (denied / timed_out / aborted): the call transitions
    ``executing → <failure_state> → memorialized``. Final terminal
    state stays ``memorialized`` because the lifecycle memorialises
    every call regardless of how it ended — the failure signal lives
    in the middle of the history. Pass either ``rolled_back=True`` for
    the post-validator-reverted path or ``failure_state="..."`` for
    the lifecycle-failure paths, but not both (the projector treats
    rollback as the more specific signal).
    """
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
    if rolled_back:
        tail = [
            {"from": "validated", "to": "reverting", "ts": "x", "reason": None},
            {"from": "reverting", "to": "reverted", "ts": "x", "reason": None},
            {"from": "reverted", "to": "memorialized", "ts": "x", "reason": None},
        ]
        head = [
            {"from": "declared", "to": "authorized", "ts": "x", "reason": None},
            {"from": "authorized", "to": "executing", "ts": "x", "reason": None},
            {"from": "executing", "to": "observed", "ts": "x", "reason": None},
            {"from": "observed", "to": "validated", "ts": "x", "reason": None},
        ]
    elif failure_state is not None:
        # denied / aborted / timed_out path: executing → failure_state →
        # memorialized. No validate/commit step because the call never
        # produced an observation worth validating.
        head = [
            {"from": "declared", "to": "authorized", "ts": "x", "reason": None},
            {"from": "authorized", "to": "executing", "ts": "x", "reason": None},
        ]
        tail = [
            {"from": "executing", "to": failure_state, "ts": "x", "reason": None},
            {"from": failure_state, "to": "memorialized", "ts": "x", "reason": None},
        ]
    else:
        head = [
            {"from": "declared", "to": "authorized", "ts": "x", "reason": None},
            {"from": "authorized", "to": "executing", "ts": "x", "reason": None},
            {"from": "executing", "to": "observed", "ts": "x", "reason": None},
            {"from": "observed", "to": "validated", "ts": "x", "reason": None},
        ]
        tail = [
            {"from": "validated", "to": "committed", "ts": "x", "reason": None},
            {"from": "committed", "to": "memorialized", "ts": "x", "reason": None},
        ]
    state_transitions = head + tail
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
    # #421: producer derives outcome from node states once terminal.
    # All-memorialized → fulfilled.
    assert view.outcome == "fulfilled"
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
    # #421: a rolled-back call's node state stays "memorialized" at the
    # node level (rolled_back is recorded separately, see replay.py).
    # The projector remaps rolled_back → "reverted" when feeding the
    # outcome helper so a status=failed envelope never reports a
    # fulfilled outcome.
    assert view.outcome == "unfulfilled"
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
    # #421: outcome stays empty while envelope is still running so the
    # renderer never paints a passing glyph on something in flight.
    assert view.outcome == ""
    states = {n.tool_name: n.state for n in view.nodes}
    assert states["read_file"] == "memorialized"
    # Call B has only BEGIN → "executing" derivation
    assert states["run_bash"] == "executing"


# ---------------------------------------------------------------------------
# Hidden-failure paths — denied / timed_out / aborted all terminate in
# ``memorialized``, so ``state in _FAILURE_STATES`` checks against the
# last transition would miss them. The projector scans the full history
# (#421 codex-review fix) so envelope status and outcome stay honest.
# ---------------------------------------------------------------------------


async def test_denied_path_marks_envelope_failed_and_unfulfilled(
    store: LedgerStore, emitter: LedgerEmitter, projector
) -> None:
    base = time.time()
    await _emit_lifecycle_pair(
        emitter,
        call_id="d",
        tool_name="run_bash",
        turn_id="turn_p",
        timestamps=(base, base + 0.1),
        failure_state="denied",
        result_summary=None,
        error="permission denied",
    )

    view = await projector.build_view(
        envelope_id="e_denied",
        session_id="sess_proj",
        turn_id="turn_p",
        turn_index=0,
        call_ids=["d"],
        call_meta={"d": CallMeta(call_id="d", tool_name="run_bash")},
    )

    # Node still terminates in memorialized (Loom lifecycle invariant) —
    # the failure signal lives mid-history.
    assert view.nodes[0].state == "memorialized"
    assert view.status == "failed"
    assert view.outcome == "unfulfilled"


async def test_timed_out_path_marks_envelope_failed_and_unfulfilled(
    store: LedgerStore, emitter: LedgerEmitter, projector
) -> None:
    base = time.time()
    await _emit_lifecycle_pair(
        emitter,
        call_id="t",
        tool_name="run_bash",
        turn_id="turn_p",
        timestamps=(base, base + 0.2),
        failure_state="timed_out",
        result_summary=None,
        error="exceeded 30s",
    )

    view = await projector.build_view(
        envelope_id="e_timeout",
        session_id="sess_proj",
        turn_id="turn_p",
        turn_index=0,
        call_ids=["t"],
        call_meta={"t": CallMeta(call_id="t", tool_name="run_bash")},
    )

    assert view.nodes[0].state == "memorialized"
    assert view.status == "failed"
    assert view.outcome == "unfulfilled"


async def test_aborted_path_keeps_aborted_outcome_glyph_distinct(
    store: LedgerStore, emitter: LedgerEmitter, projector
) -> None:
    # Aborted gets its own outcome category (🛑) instead of the generic
    # ⚠ that denied/timed_out land on — the user explicitly bailed,
    # which is meaningfully different from "the tool failed". The
    # projector preserves this distinction by surfacing the mid-history
    # ``aborted`` transition to the outcome helper rather than lumping
    # all failures under ``unfulfilled``.
    base = time.time()
    await _emit_lifecycle_pair(
        emitter,
        call_id="a",
        tool_name="run_bash",
        turn_id="turn_p",
        timestamps=(base, base + 0.1),
        failure_state="aborted",
        result_summary=None,
        error="cancelled by user",
    )

    view = await projector.build_view(
        envelope_id="e_aborted",
        session_id="sess_proj",
        turn_id="turn_p",
        turn_index=0,
        call_ids=["a"],
        call_meta={"a": CallMeta(call_id="a", tool_name="run_bash")},
    )

    assert view.nodes[0].state == "memorialized"
    assert view.status == "failed"
    assert view.outcome == "aborted"


async def test_mixed_success_and_hidden_failure_yields_partial(
    store: LedgerStore, emitter: LedgerEmitter, projector
) -> None:
    # Success node + a timed_out-then-memorialized node should derive
    # "partial" — the helper sees ``[memorialized, timed_out]`` after
    # the projector's history scan, where it would have seen
    # ``[memorialized, memorialized]`` before the fix.
    base = time.time()
    await _emit_lifecycle_pair(
        emitter,
        call_id="ok",
        tool_name="read_file",
        turn_id="turn_p",
        timestamps=(base, base + 0.1),
    )
    await _emit_lifecycle_pair(
        emitter,
        call_id="slow",
        tool_name="run_bash",
        turn_id="turn_p",
        timestamps=(base, base + 0.3),
        failure_state="timed_out",
        result_summary=None,
        error="exceeded 30s",
    )

    view = await projector.build_view(
        envelope_id="e_mixed_hidden",
        session_id="sess_proj",
        turn_id="turn_p",
        turn_index=0,
        call_ids=["ok", "slow"],
        call_meta={
            "ok": CallMeta(call_id="ok", tool_name="read_file"),
            "slow": CallMeta(call_id="slow", tool_name="run_bash"),
        },
    )

    assert view.status == "failed"
    assert view.outcome == "partial"


# ---------------------------------------------------------------------------
# Mixed terminal batch — one success, one rollback → status="failed",
# outcome="partial" (#421)
# ---------------------------------------------------------------------------


async def test_partial_outcome_when_one_success_one_rollback(
    store: LedgerStore, emitter: LedgerEmitter, projector
) -> None:
    base = time.time()
    await _emit_lifecycle_pair(
        emitter,
        call_id="ok",
        tool_name="read_file",
        turn_id="turn_p",
        timestamps=(base, base + 0.1),
    )
    await _emit_lifecycle_pair(
        emitter,
        call_id="bad",
        tool_name="run_bash",
        turn_id="turn_p",
        timestamps=(base, base + 0.3),
        rolled_back=True,
        error="post-validator rejected",
    )

    view = await projector.build_view(
        envelope_id="e_mixed",
        session_id="sess_proj",
        turn_id="turn_p",
        turn_index=0,
        call_ids=["ok", "bad"],
        call_meta={
            "ok": CallMeta(call_id="ok", tool_name="read_file"),
            "bad": CallMeta(call_id="bad", tool_name="run_bash"),
        },
    )

    assert view.status == "failed"
    # Success + rolled-back → partial: one node fully succeeded, one
    # was reverted. The mechanical helper sees ``[memorialized,
    # reverted]`` after the projector's rolled-back remap.
    assert view.outcome == "partial"


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
    # #421: outcome is gated on terminal status. Running → empty string.
    assert view.outcome == ""


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


# ---------------------------------------------------------------------------
# Interaction-language metadata fields (#417)
# ---------------------------------------------------------------------------


def test_execution_envelope_view_defaults_interaction_metadata() -> None:
    """Empty outcome must mean unknown, never silently fulfilled.

    The renderer is responsible for inferring from ``status`` when the producer
    leaves ``outcome`` empty. This default is the contract that protects
    against accidentally claiming success on a failed envelope.
    """
    from loom.core.events import ExecutionEnvelopeView
    from loom.platform.interaction_language import ParallelReason

    view = ExecutionEnvelopeView(
        envelope_id="e1",
        session_id="s1",
        turn_index=1,
        status="running",
        node_count=1,
        parallel_groups=1,
    )

    assert view.intent == ""
    assert view.parallel_reason == ParallelReason.UNSPECIFIED.value
    assert view.outcome == ""
    assert view.outcome_summary == ""
