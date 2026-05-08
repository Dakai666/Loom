"""LedgerReplay — Layer 1 + Layer 2 (#323 / doc/53 §6.3, §8).

Builds a synthetic ledger by emit()ing events directly (so the replay
layer is tested in isolation from the Step 2 emitters), then verifies:

- Layer 1 raw event sequences for turn / correlation / session
- Layer 2 TurnSnapshot reconstruction:
  - prompt_stack_snapshot from turn_start
  - tool_calls grouped by tool_call_id (BEGIN+END pair)
  - state_history phase sequence + state_transitions bundle
  - rolled_back surfaces correctly
  - memory_ops kept as a flat list (batch_read not expanded — §8.2 medium)
  - permission_decisions, judge_verdict, artifacts pass through
  - outcome / duration from turn_end
  - <incomplete> turns get a graceful partial snapshot
- correlation_snapshots covers multi-turn chains
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import pytest_asyncio

from loom.core.ledger import (
    ArtifactEmitPayload,
    JudgeVerdictPayload,
    LedgerEmitter,
    LedgerEvent,
    LedgerStore,
    MemoryOpPayload,
    PermissionDecisionPayload,
    ToolLifecyclePayload,
    TurnEndPayload,
    TurnSnapshot,
    TurnStartPayload,
    reconstruct_tool_calls,
)


@pytest_asyncio.fixture
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


@pytest_asyncio.fixture
def emitter(store: LedgerStore) -> LedgerEmitter:
    return LedgerEmitter(store, session_id="sess_replay")


# ---------------------------------------------------------------------------
# Helpers — build a "rich turn" with one tool call, one memory_op,
# one permission, one judge, one artifact.
# ---------------------------------------------------------------------------


async def _seed_rich_turn(
    store: LedgerStore,
    emitter: LedgerEmitter,
    *,
    turn_id: str = "turn_a",
    correlation_id: str = "corr_a",
    rolled_back: bool = False,
) -> None:
    base = time.time()

    async def _emit(et: str, payload, *, ts: float, **kw):
        return await emitter.emit(
            et,
            payload,
            turn_id=turn_id,
            correlation_id=correlation_id,
            timestamp=ts,
            **kw,
        )

    await _emit(
        "turn_start",
        TurnStartPayload(
            prompt_stack_hash="sha256:abc",
            prompt_stack_components={"persona": "neutral", "tool_catalog_size": 5},
        ),
        ts=base + 0.0,
    )
    await _emit(
        "tool_lifecycle",
        ToolLifecyclePayload(
            phase="BEGIN",
            tool_name="run_bash",
            tool_call_id="call_1",
            args_digest="sha256:args_1",
        ),
        ts=base + 0.1,
    )
    await _emit(
        "permission_decision",
        PermissionDecisionPayload(
            decision="grant",
            tool_call_id="call_1",
            trust_level="safe",
            reason="pre-authorized",
        ),
        ts=base + 0.15,
    )
    await _emit(
        "memory_op",
        MemoryOpPayload(
            operation="read",
            type_summary="search:all",
            trigger="agent_search",
        ),
        ts=base + 0.2,
    )
    await _emit(
        "tool_lifecycle",
        ToolLifecyclePayload(
            phase="END",
            tool_name="run_bash",
            tool_call_id="call_1",
            args_digest="sha256:args_1",
            result_digest="sha256:r_1",
            result_summary="ok",
            state_history=[
                {"from": "declared", "to": "authorized", "ts": "x", "reason": None},
                {"from": "authorized", "to": "prepared", "ts": "x", "reason": None},
                {"from": "prepared", "to": "executing", "ts": "x", "reason": None},
                {"from": "executing", "to": "observed", "ts": "x", "reason": None},
                {"from": "observed", "to": "validated", "ts": "x", "reason": None},
                *(
                    [
                        {"from": "validated", "to": "reverting", "ts": "x", "reason": "x"},
                        {"from": "reverting", "to": "reverted", "ts": "x", "reason": "x"},
                    ]
                    if rolled_back
                    else [
                        {"from": "validated", "to": "committed", "ts": "x", "reason": None},
                    ]
                ),
                {"from": "committed" if not rolled_back else "reverted",
                 "to": "memorialized", "ts": "x", "reason": None},
            ],
            rolled_back=rolled_back,
            error="rejected" if rolled_back else None,
        ),
        ts=base + 0.3,
    )
    await _emit(
        "judge_verdict",
        JudgeVerdictPayload(
            verdict="PASS" if not rolled_back else "FAIL",
            confidence=0.9,
            reason="looks ok",
            judged_subject="turn",
        ),
        ts=base + 0.35,
    )
    await _emit(
        "artifact_emit",
        ArtifactEmitPayload(
            artifact_type="code",
            size_bytes=1024,
            digest="sha256:af",
            location="report.md",
        ),
        ts=base + 0.4,
    )
    await _emit(
        "turn_end",
        TurnEndPayload(
            outcome="clean" if not rolled_back else "error",
            duration_ms=400,
            token_usage={"prompt": 100, "completion": 50},
        ),
        ts=base + 0.5,
    )


# ---------------------------------------------------------------------------
# Layer 1
# ---------------------------------------------------------------------------


async def test_events_for_turn_returns_chronological(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    await _seed_rich_turn(store, emitter)
    events = await store.replay.events_for_turn("turn_a")
    types = [e.event_type for e in events]
    assert types[0] == "turn_start"
    assert types[-1] == "turn_end"
    # Timestamps strictly non-decreasing
    ts = [e.timestamp for e in events]
    assert ts == sorted(ts)


async def test_events_for_correlation_crosses_turns(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    await _seed_rich_turn(
        store, emitter, turn_id="turn_a", correlation_id="shared_corr"
    )
    await _seed_rich_turn(
        store, emitter, turn_id="turn_b", correlation_id="shared_corr"
    )
    # A noise turn with a different correlation should not appear
    await _seed_rich_turn(
        store, emitter, turn_id="turn_c", correlation_id="other_corr"
    )

    events = await store.replay.events_for_correlation("shared_corr")
    distinct_turns = {e.turn_id for e in events}
    assert distinct_turns == {"turn_a", "turn_b"}


async def test_events_for_session_pulls_all(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    await _seed_rich_turn(store, emitter, turn_id="turn_a")
    await _seed_rich_turn(store, emitter, turn_id="turn_b")
    events = await store.replay.events_for_session("sess_replay")
    assert {e.turn_id for e in events} == {"turn_a", "turn_b"}


# ---------------------------------------------------------------------------
# Layer 2 — turn_snapshot
# ---------------------------------------------------------------------------


async def test_turn_snapshot_full_field_population(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    await _seed_rich_turn(store, emitter)
    snap: TurnSnapshot = await store.replay.turn_snapshot("turn_a")

    assert snap.turn_id == "turn_a"
    assert snap.branch_id == "main"
    assert "corr_a" in snap.correlation_ids
    assert snap.outcome == "clean"
    assert snap.duration_ms == 400

    # PromptStack
    assert snap.prompt_stack_snapshot is not None
    assert snap.prompt_stack_snapshot.prompt_stack_hash == "sha256:abc"
    assert snap.prompt_stack_snapshot.prompt_stack_components["persona"] == "neutral"

    # tool_calls (medium reconstruction)
    assert len(snap.tool_calls) == 1
    tc = snap.tool_calls[0]
    assert tc.tool_call_id == "call_1"
    assert tc.tool_name == "run_bash"
    assert tc.state_history == ["BEGIN", "END"]  # phase sequence per §8.1
    assert tc.result_digest == "sha256:r_1"
    assert tc.result_summary == "ok"
    assert tc.rolled_back is False
    assert tc.error is None
    assert len(tc.state_transitions) >= 5  # bundled lifecycle history

    # memory_ops kept flat (no expansion of batch_read; that's §3.5)
    assert len(snap.memory_ops) == 1
    assert snap.memory_ops[0].trigger == "agent_search"

    # permission_decisions
    assert len(snap.permission_decisions) == 1
    assert snap.permission_decisions[0].decision == "grant"
    assert snap.permission_decisions[0].tool_call_id == "call_1"

    # judge_verdict
    assert snap.judge_verdict is not None
    assert snap.judge_verdict["verdict"] == "PASS"

    # artifacts
    assert len(snap.artifacts) == 1
    assert snap.artifacts[0].location == "report.md"
    assert snap.artifacts[0].size_bytes == 1024


async def test_rolled_back_tool_call_propagates(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    await _seed_rich_turn(store, emitter, turn_id="turn_x", rolled_back=True)
    snap = await store.replay.turn_snapshot("turn_x")

    assert snap.outcome == "error"
    tc = snap.tool_calls[0]
    assert tc.rolled_back is True
    assert tc.error == "rejected"
    # The bundled state_transitions list contains REVERTED
    assert any(t["to"] == "reverted" for t in tc.state_transitions)


async def test_incomplete_turn_returns_partial_snapshot(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    """A turn with no turn_end yet must still snapshot cleanly."""
    base = time.time()
    await emitter.emit(
        "turn_start",
        TurnStartPayload(
            prompt_stack_hash="sha256:p",
            prompt_stack_components={"persona": "x"},
        ),
        turn_id="turn_pending",
        correlation_id="c1",
        timestamp=base,
    )
    await emitter.emit(
        "tool_lifecycle",
        ToolLifecyclePayload(
            phase="BEGIN",
            tool_name="t",
            tool_call_id="call_q",
            args_digest="sha256:q",
        ),
        turn_id="turn_pending",
        correlation_id="c1",
        timestamp=base + 0.1,
    )

    snap = await store.replay.turn_snapshot("turn_pending")
    assert snap.outcome == "<incomplete>"
    assert snap.duration_ms >= 0  # fallback span
    # Tool call has only a BEGIN — phase sequence reflects that
    assert snap.tool_calls[0].state_history == ["BEGIN"]
    assert snap.tool_calls[0].result_digest is None


async def test_unknown_turn_returns_empty_snapshot(store: LedgerStore) -> None:
    snap = await store.replay.turn_snapshot("turn_does_not_exist")
    assert snap.turn_id == "turn_does_not_exist"
    assert snap.tool_calls == []
    assert snap.outcome == "<incomplete>"
    assert snap.prompt_stack_snapshot is None


# ---------------------------------------------------------------------------
# correlation_snapshots — multi-turn chain
# ---------------------------------------------------------------------------


async def test_correlation_snapshots_covers_each_turn(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    await _seed_rich_turn(
        store, emitter, turn_id="turn_a", correlation_id="chain_corr"
    )
    await _seed_rich_turn(
        store, emitter, turn_id="turn_b", correlation_id="chain_corr"
    )
    # Different correlation — must NOT appear
    await _seed_rich_turn(
        store, emitter, turn_id="turn_other", correlation_id="other"
    )

    snaps = await store.replay.correlation_snapshots("chain_corr")
    turn_ids = [s.turn_id for s in snaps]
    assert turn_ids == ["turn_a", "turn_b"]
    # Each is a fully-built snapshot, not a stub
    assert all(s.outcome == "clean" for s in snaps)
    assert all(len(s.tool_calls) == 1 for s in snaps)


# ---------------------------------------------------------------------------
# reconstruct_tool_calls free helper
# ---------------------------------------------------------------------------


async def test_reconstruct_tool_calls_orders_by_first_seen(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    """Calls are listed in the order their BEGIN events first appear."""
    base = time.time()

    async def _begin_end(call_id: str, ts: float):
        await emitter.emit(
            "tool_lifecycle",
            ToolLifecyclePayload(
                phase="BEGIN",
                tool_name="t",
                tool_call_id=call_id,
                args_digest=f"sha256:{call_id}",
            ),
            turn_id="turn_order",
            correlation_id="c",
            timestamp=ts,
        )
        await emitter.emit(
            "tool_lifecycle",
            ToolLifecyclePayload(
                phase="END",
                tool_name="t",
                tool_call_id=call_id,
                args_digest=f"sha256:{call_id}",
                result_summary="ok",
            ),
            turn_id="turn_order",
            correlation_id="c",
            timestamp=ts + 0.5,
        )

    # Interleave the BEGINs in a deliberate order
    await _begin_end("call_2nd", base + 0.0)
    await _begin_end("call_1st", base + 0.1)
    await _begin_end("call_3rd", base + 0.2)

    events = await store.replay.events_for_turn("turn_order")
    tool_calls = reconstruct_tool_calls(events)
    assert [tc.tool_call_id for tc in tool_calls] == [
        "call_2nd",
        "call_1st",
        "call_3rd",
    ]


async def test_reconstruct_tool_calls_skips_non_lifecycle_events(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    """memory_op / judge_verdict events must not contribute."""
    await _seed_rich_turn(store, emitter, turn_id="turn_mix")
    events = await store.replay.events_for_turn("turn_mix")
    tool_calls = reconstruct_tool_calls(events)
    assert len(tool_calls) == 1  # only the one tool_lifecycle group


# ---------------------------------------------------------------------------
# Layer 1 helpers can be passed back into reconstruct_tool_calls
# (a common consumer pattern: pull events once, re-project N ways)
# ---------------------------------------------------------------------------


async def test_layer1_then_helper_composition(
    store: LedgerStore, emitter: LedgerEmitter
) -> None:
    await _seed_rich_turn(store, emitter)
    events = await store.replay.events_for_turn("turn_a")
    summaries = reconstruct_tool_calls(events)
    assert summaries[0].tool_name == "run_bash"


# ---------------------------------------------------------------------------
# replay accessor is cached on the store
# ---------------------------------------------------------------------------


async def test_replay_accessor_is_cached(store: LedgerStore) -> None:
    a = store.replay
    b = store.replay
    assert a is b
