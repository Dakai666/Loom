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
    _envelope_dominant_threshold_s,
    _format_envelope_status,
)
from loom.platform.interaction_language import (
    EnvelopeOutcome,
    LivenessSensor,
    ParallelReason,
)


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
# Discord per-tool threshold (LivenessSensor convergence — divergence kill)
# ----------------------------------------------------------------------
# The stall DECISION/CLOCK logic that previously lived in bot.py
# (``_should_emit_stalled_status`` / ``_event_resets_stall_clock``) now lives
# in ``LivenessSensor``; its full behavior matrix is covered by
# ``tests/test_interaction_language.py``. Here we pin only the Discord-side
# integration: the bot now drives per-tool thresholds (run_bash takes 90s,
# default tools 30s) instead of the old flat 90.0, proving the CLI/Discord
# divergence is killed.


def test_envelope_dominant_threshold_takes_most_patient_tool() -> None:
    # Parallel nodes: the slowest-allowed tool governs (max threshold).
    view = ExecutionEnvelopeView(
        envelope_id="e1",
        session_id="s1",
        turn_index=1,
        status="running",
        node_count=2,
        parallel_groups=1,
        levels=[["n1", "n2"]],
        nodes=[_node("n1", "read_file"), _node("n2", "run_bash")],
    )
    assert _envelope_dominant_threshold_s(view) == 90.0


def test_parallel_envelope_threshold_not_clobbered_by_last_tool_begin() -> None:
    # Regression for PR #483 review P2: under parallel dispatch the envelope
    # emits EnvelopeStarted (dominant threshold = most patient = 90s for a
    # run_bash + read_file group), then every ToolBegin back-to-back before
    # any tool completes and before any fallback EnvelopeUpdated lands. A
    # per-ToolBegin set_threshold would let the LAST tool (read_file, 30s)
    # overwrite the patient window. The fix gates ToolBegin on
    # ``not _envelope_active``; this test mirrors that handler sequence.
    sensor = LivenessSensor(default_threshold_s=30.0, clock=lambda: 0.0)
    view = ExecutionEnvelopeView(
        envelope_id="e1",
        session_id="s1",
        turn_index=1,
        status="running",
        node_count=2,
        parallel_groups=1,
        levels=[["n1", "n2"]],
        nodes=[_node("n1", "read_file"), _node("n2", "run_bash")],
    )
    # EnvelopeStarted: envelope becomes active, dominant threshold applied.
    envelope_active = True
    sensor.set_threshold(seconds=_envelope_dominant_threshold_s(view))  # 90s
    # ToolBegin(run_bash) then ToolBegin(read_file): gated out by the fix.
    for tool_name in ("run_bash", "read_file"):
        if not envelope_active:  # the bot.py gate — both skipped here
            sensor.set_threshold(tool_name=tool_name)
    # The patient 90s window survives: no stall at 30s, stall only past 90s.
    assert sensor.should_emit_stalled(now=30.2) is False
    assert sensor.should_emit_stalled(now=90.2) is True


def test_envelope_dominant_threshold_default_for_fast_tools() -> None:
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
    assert _envelope_dominant_threshold_s(view) == 30.0


def test_discord_watchdog_run_bash_gets_90s_not_flat_30() -> None:
    # Drive the sensor the way the Discord ToolBegin branch does: a
    # run_bash tool sets the per-tool threshold to 90s. After 60s of
    # synthetic no-op envelope ticks (same fingerprint), the watchdog must
    # NOT have stalled — the old flat-90 would also pass here, but the
    # point is that the per-tool table now drives it.
    sensor = LivenessSensor(default_threshold_s=30.0, clock=lambda: 0.0)
    sensor.set_threshold(tool_name="run_bash")  # 90s
    rendered = _format_envelope_status(_running_envelope("e1", "run_bash"))
    sensor.observe(now=0.0, fingerprint=rendered)
    # 60 no-op ticks, fingerprint unchanged → never resets the clock.
    for tick in range(1, 61):
        sensor.observe(now=float(tick), fingerprint=rendered)
    assert sensor.should_emit_stalled(now=60.0) is False
    # But by 90s it does stall.
    assert sensor.should_emit_stalled(now=90.0) is True


def test_discord_watchdog_default_tool_stalls_at_30s() -> None:
    # A default-patience tool now stalls at 30s on Discord (it used to
    # tolerate 90s under the old flat threshold). This is the intended
    # user-visible behavior change that kills the divergence.
    sensor = LivenessSensor(default_threshold_s=30.0, clock=lambda: 0.0)
    sensor.set_threshold(tool_name="read_file")  # 30s
    rendered = _format_envelope_status(_running_envelope("e1", "read_file"))
    sensor.observe(now=0.0, fingerprint=rendered)
    for tick in range(1, 31):
        sensor.observe(now=float(tick), fingerprint=rendered)
    assert sensor.should_emit_stalled(now=29.0) is False
    assert sensor.should_emit_stalled(now=30.0) is True


# ----------------------------------------------------------------------
# Overlay-aware envelope edit gating (#422 codex review v3)
# ----------------------------------------------------------------------


from loom.platform.discord.bot import (
    _envelope_edit_would_clobber_overlay,
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


# ----------------------------------------------------------------------
# Overlay-aware envelope edit gating (#422 codex review v3)
# ----------------------------------------------------------------------
# v1 fixed the stall-clock starvation by gating clock reset on observed
# render. v2 then tried to unify clock-gate and branch-skip into a
# single ``continue`` at the loop top — but that conflated observed
# state with displayed state and broke debounce-suppressed catch-up.
# v3 splits them back apart:
#
# - The LivenessSensor clock compares the OBSERVED fingerprint.
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

    # Also confirm the clock-gate side: once the sensor has observed B,
    # a redundant fallback carrying the same fingerprint B does NOT reset
    # the clock. Two timelines, two gates.
    sensor = LivenessSensor()
    sensor.observe(fingerprint=rendered_b)
    assert sensor.observe(fingerprint=rendered_b) is False


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
    sensor = LivenessSensor()
    sensor.observe(fingerprint=rendered)
    assert sensor.observe(fingerprint=rendered) is False
