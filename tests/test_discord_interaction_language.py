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


# ----------------------------------------------------------------------
# Stall-clock heartbeat gating (#422 codex review)
# ----------------------------------------------------------------------


from loom.core.events import (
    ActionStateChange,
    EnvelopeStarted,
    EnvelopeUpdated,
    TextChunk,
)
from loom.platform.discord.bot import (
    _envelope_edit_would_clobber_overlay,
    _event_resets_stall_clock,
)


def _running_envelope(envelope_id: str, tool_name: str) -> ExecutionEnvelopeView:
    """Single-node running envelope — what a long sequential ``run_bash``
    looks like to the Discord consumer between ``EnvelopeStarted`` and
    ``EnvelopeCompleted``."""
    return ExecutionEnvelopeView(
        envelope_id=envelope_id,
        session_id="s1",
        turn_index=1,
        status="running",
        node_count=1,
        parallel_groups=1,
        levels=[["n1"]],
        nodes=[_node("n1", tool_name)],
    )


def test_envelope_started_always_resets_stall_clock() -> None:
    event = EnvelopeStarted(envelope=_running_envelope("e1", "run_bash"))
    assert _event_resets_stall_clock(event, last_envelope_render="") is True


def test_envelope_updated_with_no_render_change_does_not_reset_clock() -> None:
    # The pathological case codex caught: ``LoomSession.stream_turn``
    # emits a synthetic ``EnvelopeUpdated`` every ~1s while a tool is
    # running. If those reset the clock, a long sequential ``run_bash``
    # never reaches the 90s threshold.
    envelope = _running_envelope("e1", "run_bash")
    rendered = _format_envelope_status(envelope)
    event = EnvelopeUpdated(envelope=envelope)
    assert _event_resets_stall_clock(event, last_envelope_render=rendered) is False


def test_envelope_updated_with_state_change_resets_clock() -> None:
    # When a node transitions (e.g., one of N parallel tools just
    # finished), the rendered status differs from the previous view —
    # that's real progress and must reset the clock.
    previous = _format_envelope_status(_running_envelope("e1", "run_bash"))
    new_view = ExecutionEnvelopeView(
        envelope_id="e1",
        session_id="s1",
        turn_index=1,
        status="running",
        node_count=2,
        parallel_groups=1,
        levels=[["n1", "n2"]],
        nodes=[_node("n1", "run_bash"), _node("n2", "pytest")],
    )
    event = EnvelopeUpdated(envelope=new_view)
    assert _event_resets_stall_clock(event, last_envelope_render=previous) is True


def test_action_state_change_never_resets_clock() -> None:
    # Silent in Discord display by design (#422). User sees no edit,
    # so 90s of state-machine churn still warrants a stalled message.
    event = ActionStateChange(
        action_id="n1",
        tool_name="run_bash",
        call_id="n1",
        old_state="executing",
        new_state="observed",
    )
    assert _event_resets_stall_clock(event, last_envelope_render="anything") is False


def test_text_chunk_always_resets_clock() -> None:
    event = TextChunk(text="hello")
    assert _event_resets_stall_clock(event, last_envelope_render="") is True


def test_long_sequential_dispatch_eventually_triggers_stalled() -> None:
    """End-to-end shape: simulate the codex-described scenario.

    A single long ``run_bash`` produces ``EnvelopeStarted`` followed by
    ~90 fallback ``EnvelopeUpdated`` ticks at 1s intervals before the
    user-visible state changes. With the unconditional heartbeat (pre
    codex fix) the clock would reset every tick and the watchdog would
    never fire. With the render-gated heartbeat, after 90s of identical
    fallback ticks the threshold is crossed and a single stalled emit
    must be allowed.
    """
    envelope = _running_envelope("e1", "run_bash")
    rendered = _format_envelope_status(envelope)

    started = EnvelopeStarted(envelope=envelope)
    last_event_at = 0.0
    if _event_resets_stall_clock(started, last_envelope_render=""):
        last_event_at = 0.0  # turn start
    last_envelope_render = rendered

    # 90 fallback updates at 1s intervals — none change the rendered
    # status, so none reset the clock.
    for tick in range(1, 91):
        now = float(tick)
        ev = EnvelopeUpdated(envelope=envelope)
        if _event_resets_stall_clock(ev, last_envelope_render):
            last_event_at = now
        # Outer loop would also keep ``last_envelope_render`` fresh; it
        # never actually changes here so the assignment is a no-op.
        last_envelope_render = _format_envelope_status(ev.envelope)

    now = 90.5  # half a watchdog poll-tick past the threshold boundary
    assert _should_emit_stalled_status(
        now=now,
        last_event_at=last_event_at,
        threshold_s=90.0,
        already_emitted=False,
    ), "watchdog must fire after 90s of no-op envelope ticks"


# ----------------------------------------------------------------------
# Overlay-aware envelope edit gating (#422 codex review v3)
# ----------------------------------------------------------------------
# v1 fixed the stall-clock starvation by gating clock reset on observed
# render. v2 then tried to unify clock-gate and branch-skip into a
# single ``continue`` at the loop top — but that conflated observed
# state with displayed state and broke debounce-suppressed catch-up.
# v3 splits them back apart:
#
# - ``_event_resets_stall_clock`` (clock) compares OBSERVED render.
# - ``_envelope_edit_would_clobber_overlay`` (edit) compares DISPLAYED
#   render. The displayed timeline lags the observed one whenever an
#   earlier update lost its edit to debounce, which is the exact case
#   v2 broke.
#
# Tests below pin both contracts so future refactors can't re-merge.


def test_edit_skip_preserves_overlay_on_identical_displayed_render() -> None:
    # The v2-class case: watchdog overlay is up, display matches
    # observation, and a no-op fallback arrives. Skipping the edit
    # preserves the ``still waiting`` overlay.
    rendered = _format_envelope_status(_running_envelope("e1", "run_bash"))
    assert _envelope_edit_would_clobber_overlay(
        new_render=rendered,
        displayed_render=rendered,
        stalled_emitted=True,
    ) is True


def test_edit_runs_when_no_overlay_to_preserve() -> None:
    # Without an active overlay there's nothing to clobber, so even
    # identical renders should re-edit. This is the load-bearing case
    # for v3: a debounce-suppressed real-progress update means the
    # observed render moved on but the displayed render didn't. The
    # next identical fallback finds ``new_render != tool_buf`` (because
    # tool_buf is the stale pre-progress render) — but the helper
    # also has to allow re-edit when ``new_render == tool_buf`` if
    # stalled isn't set, in case a later refactor relies on idempotent
    # re-edits to refresh the display.
    rendered = _format_envelope_status(_running_envelope("e1", "run_bash"))
    assert _envelope_edit_would_clobber_overlay(
        new_render=rendered,
        displayed_render=rendered,
        stalled_emitted=False,
    ) is False


def test_edit_runs_when_render_differs_even_with_overlay() -> None:
    # Real progress overrides the overlay: the new render reflects new
    # state, the user is no longer stalled, the overlay shouldn't
    # survive. ``new_render != tool_buf`` is the catch-up case where
    # the displayed render was suppressed by debounce earlier.
    old = _format_envelope_status(_running_envelope("e1", "run_bash"))
    new = _format_envelope_status(
        ExecutionEnvelopeView(
            envelope_id="e1",
            session_id="s1",
            turn_index=1,
            status="completed",
            node_count=1,
            parallel_groups=1,
            levels=[["n1"]],
            nodes=[_node("n1", "run_bash", state="memorialized")],
        )
    )
    assert _envelope_edit_would_clobber_overlay(
        new_render=new,
        displayed_render=old,
        stalled_emitted=True,
    ) is False


def test_debounce_suppressed_progress_catches_up_via_later_fallback() -> None:
    """Regression for the codex P2 v3 finding (PR #429 third review).

    Sequence:
    1. T=0   ``EnvelopeStarted`` render A → ``tool_buf = A``,
       displayed = A, ``_last_envelope_edit = 0``.
    2. T=0.3 ``EnvelopeUpdated`` render B (real progress). Top
       guard: B != A, clock resets, ``_last_envelope_render = B``.
       Branch: debounce blocks (0.3 < 0.5s), ``tool_buf`` stays A.
       Observation moved on, display did not.
    3. T=1.3 fallback ``EnvelopeUpdated`` render B (no further
       state change). Top guard: B == B, clock unchanged. Branch:
       debounce passes (1.0 > 0.5s).

    The wiring guarantee at step 3:

    - v2 (bug): the loop's top ``continue`` skipped the branch
      entirely because observed == new. Display stayed at A forever.
    - v3 (fix): the loop runs the branch. The branch's
      overlay-aware skip compares against DISPLAYED, finds
      ``new_render (B) != tool_buf (A)``, runs the edit. Display
      catches up to B.

    This test exercises the helper at step 3 with the exact arguments
    the production branch would supply.
    """
    rendered_a = _format_envelope_status(_running_envelope("e1", "run_bash"))
    progress_view = ExecutionEnvelopeView(
        envelope_id="e1",
        session_id="s1",
        turn_index=1,
        status="completed",
        node_count=1,
        parallel_groups=1,
        levels=[["n1"]],
        nodes=[_node("n1", "run_bash", state="memorialized")],
    )
    rendered_b = _format_envelope_status(progress_view)

    # tool_buf stale at A; new render is B; no overlay (real progress
    # at T=0.3 reset _stalled_emitted on the clock gate).
    assert _envelope_edit_would_clobber_overlay(
        new_render=rendered_b,
        displayed_render=rendered_a,
        stalled_emitted=False,
    ) is False  # → branch will run the edit → display catches up

    # Also confirm the clock-gate side: observed (B) == observed (B)
    # so a redundant fallback after we already observed B does NOT
    # reset the clock. Two timelines, two gates.
    fallback = EnvelopeUpdated(envelope=progress_view)
    assert _event_resets_stall_clock(fallback, rendered_b) is False


def test_no_op_envelope_update_with_stalled_overlay_preserves_message() -> None:
    """v2 case restated against the v3 mechanism.

    After the watchdog has fired and ``_stalled_emitted = True``, a
    fallback envelope update with identical displayed render must skip
    the edit. The v3 mechanism still pins this — the helper returns
    True for ``displayed == new AND stalled`` and the branch responds
    with ``pass``.
    """
    rendered = _format_envelope_status(_running_envelope("e1", "run_bash"))
    assert _envelope_edit_would_clobber_overlay(
        new_render=rendered,
        displayed_render=rendered,
        stalled_emitted=True,
    ) is True
    # The clock-gate side also still returns False for this case so
    # the latch doesn't accidentally reset.
    no_op = EnvelopeUpdated(envelope=_running_envelope("e1", "run_bash"))
    assert _event_resets_stall_clock(no_op, rendered) is False
