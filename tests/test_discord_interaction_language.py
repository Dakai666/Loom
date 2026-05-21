"""Discord rendering of the shared interaction-language envelope view (#420).

Pure-function coverage around ``_format_envelope_status``. Verifies:

- Single-node envelopes keep the compact ``Envelope <id>`` header.
- Multi-node envelopes render an ``▸ <intent>`` header with the human
  ``parallel_reason`` label, never leaking the raw enum value.
- When the agent omits the intent on a multi-node envelope, the renderer
  falls back to ``synthesize_envelope_intent`` so the user is never shown
  a bare ``▸ `` row.
- Outcomes other than ``FULFILLED`` add a trailing summary line with the
  matching glyph (``⚠`` / ``◐`` / ``↪`` / ``🛑``).
- An empty ``outcome`` does NOT silently render as success — when status
  is ``failed`` the renderer still surfaces a warning so the user can
  tell the batch did not fulfill its intent.
"""
from __future__ import annotations

from loom.core.events import ExecutionEnvelopeView, ExecutionNodeView
from loom.platform.discord.bot import (
    _format_envelope_status,
    _should_emit_stalled_status,
)
from loom.platform.interaction_language import EnvelopeOutcome, ParallelReason


def _node(node_id: str, tool_name: str, state: str = "executing") -> ExecutionNodeView:
    return ExecutionNodeView(
        node_id=node_id,
        call_id=f"call-{node_id}",
        action_id=node_id,
        tool_name=tool_name,
        level=0,
        state=state,
        trust_level="SAFE",
    )


def test_single_node_envelope_keeps_compact_header() -> None:
    view = ExecutionEnvelopeView(
        envelope_id="e1",
        session_id="s1",
        turn_index=1,
        status="running",
        node_count=1,
        parallel_groups=1,
        levels=[["n1"]],
        nodes=[_node("n1", "read_file")],
    )

    text = _format_envelope_status(view)

    assert "Envelope e1" in text
    assert "read_file" in text
    # Single-node batches do not get the intent header glyph
    assert "▸" not in text


def test_multi_node_envelope_renders_intent_and_parallel_reason() -> None:
    view = ExecutionEnvelopeView(
        envelope_id="e2",
        session_id="s1",
        turn_index=1,
        status="running",
        node_count=2,
        parallel_groups=1,
        levels=[["n1", "n2"]],
        nodes=[_node("n1", "pytest"), _node("n2", "pytest")],
        intent="重複跑測試確認穩定性",
        parallel_reason=ParallelReason.FAN_OUT_REPLICAS.value,
    )

    text = _format_envelope_status(view)

    assert "▸ 重複跑測試確認穩定性" in text
    assert "多次驗證" in text
    # Never leak the raw enum value to the user
    assert "fan_out_replicas" not in text


def test_multi_node_envelope_synthesizes_intent_when_agent_omits_it() -> None:
    view = ExecutionEnvelopeView(
        envelope_id="e4",
        session_id="s1",
        turn_index=1,
        status="running",
        node_count=3,
        parallel_groups=1,
        levels=[["n1", "n2", "n3"]],
        nodes=[
            _node("n1", "pytest"),
            _node("n2", "pytest"),
            _node("n3", "pytest"),
        ],
        parallel_reason=ParallelReason.FAN_OUT_REPLICAS.value,
    )

    text = _format_envelope_status(view)

    assert "▸ 重複跑同一組 pytest" in text
    assert "多次驗證" in text


def test_unfulfilled_envelope_renders_outcome_summary() -> None:
    view = ExecutionEnvelopeView(
        envelope_id="e3",
        session_id="s1",
        turn_index=1,
        status="failed",
        node_count=2,
        parallel_groups=1,
        levels=[["n1", "n2"]],
        nodes=[
            _node("n1", "pytest", "observed"),
            _node("n2", "pytest", "timed_out"),
        ],
        intent="驗證測試狀態",
        outcome=EnvelopeOutcome.UNFULFILLED.value,
        outcome_summary="其中一組測試逾時，需要重跑",
    )

    text = _format_envelope_status(view)

    assert "⚠ 其中一組測試逾時，需要重跑" in text


def test_empty_outcome_infers_from_failed_status_without_claiming_success() -> None:
    # Plan invariant: empty ``outcome`` means unknown, never fulfilled.
    # When ``status == "failed"`` the renderer must still surface a
    # warning so the user can tell something went wrong even if no
    # producer wrote an outcome string.
    view = ExecutionEnvelopeView(
        envelope_id="e5",
        session_id="s1",
        turn_index=1,
        status="failed",
        node_count=1,
        parallel_groups=1,
        levels=[["n1"]],
        nodes=[_node("n1", "pytest", "timed_out")],
        outcome="",
    )

    text = _format_envelope_status(view)

    assert "failed" in text or "⚠" in text


def test_partial_outcome_renders_half_circle_glyph() -> None:
    view = ExecutionEnvelopeView(
        envelope_id="e6",
        session_id="s1",
        turn_index=1,
        status="completed",
        node_count=2,
        parallel_groups=1,
        levels=[["n1", "n2"]],
        nodes=[
            _node("n1", "pytest", "observed"),
            _node("n2", "pytest", "timed_out"),
        ],
        intent="跑多組測試",
        outcome=EnvelopeOutcome.PARTIAL.value,
        outcome_summary="一組成功，一組逾時",
    )

    text = _format_envelope_status(view)

    assert "◐ 一組成功，一組逾時" in text


def test_fulfilled_outcome_does_not_add_summary_line() -> None:
    # Plain success should not add visual noise — the per-node ✓ icons
    # already tell the story.
    view = ExecutionEnvelopeView(
        envelope_id="e7",
        session_id="s1",
        turn_index=1,
        status="completed",
        node_count=2,
        parallel_groups=1,
        levels=[["n1", "n2"]],
        nodes=[
            _node("n1", "pytest", "observed"),
            _node("n2", "pytest", "observed"),
        ],
        outcome=EnvelopeOutcome.FULFILLED.value,
    )

    text = _format_envelope_status(view)

    # No glyph + summary line stacked after the nodes
    assert "◐" not in text
    assert "⚠" not in text
    assert "↪" not in text


# ----------------------------------------------------------------------
# Stalled-status proxy (#422)
# ----------------------------------------------------------------------


def test_stalled_status_emits_once_after_threshold() -> None:
    # First check past the threshold returns True; once the caller has
    # recorded the emit, subsequent ticks must not retrigger until the
    # next observable event resets ``already_emitted``.
    assert _should_emit_stalled_status(
        now=100.0,
        last_event_at=9.0,
        threshold_s=90.0,
        already_emitted=False,
    )
    assert not _should_emit_stalled_status(
        now=100.0,
        last_event_at=9.0,
        threshold_s=90.0,
        already_emitted=True,
    )


def test_stalled_status_does_not_emit_before_threshold() -> None:
    assert not _should_emit_stalled_status(
        now=100.0,
        last_event_at=20.0,  # only 80s of quiet, threshold is 90s
        threshold_s=90.0,
        already_emitted=False,
    )


def test_stalled_status_is_suppressed_while_waiting_for_user() -> None:
    # During a TurnPaused window, the runtime is quiet on the agent
    # side because we're waiting on a human. The watchdog must not
    # call this "still waiting" — same invariant as the CLI footer's
    # PAUSED_BLOCKING heartbeat state (#419).
    assert not _should_emit_stalled_status(
        now=200.0,
        last_event_at=0.0,
        threshold_s=90.0,
        already_emitted=False,
        suppressed=True,
    )


def test_stalled_status_suppression_takes_precedence_over_already_emitted() -> None:
    # Even if the watchdog had already fired (and would normally just
    # stay quiet), the suppression flag is the dominant signal. This
    # protects against a race where a pause arrives right after a
    # stalled emit — we want the pause window to govern, not the
    # stalled latch.
    assert not _should_emit_stalled_status(
        now=200.0,
        last_event_at=0.0,
        threshold_s=90.0,
        already_emitted=True,
        suppressed=True,
    )
