from __future__ import annotations

from loom.platform.interaction_language import (
    ANIMATION_FAMILIES,
    ActionFamily,
    Engagement,
    HeartbeatState,
    LivenessSensor,
    _LABELS_ZH_TW,
    advance_engagement,
    derive_envelope_outcome,
    family_fps,
    family_variants,
    format_elapsed,
    format_parallel_reason,
    resolve_tool_action,
    synthesize_envelope_intent,
    stale_threshold_for_tool,
)


class _FakeClock:
    """Manually-advanced monotonic clock for deterministic sensor tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


def test_format_elapsed_under_one_minute_uses_seconds() -> None:
    assert format_elapsed(0.0) == "0s"
    assert format_elapsed(7.2) == "7s"
    assert format_elapsed(59.9) == "59s"


def test_format_elapsed_over_one_minute_uses_mmss() -> None:
    assert format_elapsed(60.0) == "1:00"
    assert format_elapsed(125.4) == "2:05"


def test_resolve_tool_action_for_run_bash_uses_command_root() -> None:
    action = resolve_tool_action("run_bash", {"command": "pytest tests/test_app.py -q"})
    assert action.label == "執行指令"
    assert action.subject == "pytest"


def test_resolve_tool_action_for_run_bash_skips_leading_env_assignments() -> None:
    # Heartbeat label must reflect the actual command, not the env var.
    # Mirrors sandbox_runtime.extract_command_root semantics (which we reuse).
    action = resolve_tool_action(
        "run_bash", {"command": "GH_HOST=github.com gh pr view 425"}
    )
    assert action.subject == "gh"

    action = resolve_tool_action(
        "run_bash", {"command": "_DEBUG=1 gh pr view 425"}
    )
    assert action.subject == "gh"


def test_resolve_tool_action_for_list_dir_uses_path_subject() -> None:
    action = resolve_tool_action("list_dir", {"path": "loom/platform"})
    assert action.label == "列出目錄"
    assert action.subject == "loom/platform"


def test_resolve_tool_action_for_read_file_uses_path_subject() -> None:
    action = resolve_tool_action("read_file", {"path": "loom/platform/cli/app.py"})
    assert action.label == "查詢檔案"
    assert action.subject == "loom/platform/cli/app.py"
    # Reading is a read-only probe — the scanning family.
    assert action.family == ActionFamily.PROBE.value


def test_resolve_tool_action_families_by_category() -> None:
    # Each action category maps to a distinct animation family so the
    # footer reads differently for writing vs searching vs executing.
    assert resolve_tool_action("write_file", {"path": "a.py"}).family == ActionFamily.WRITE.value
    assert resolve_tool_action("edit_file", {"path": "a.py"}).family == ActionFamily.WRITE.value
    assert resolve_tool_action("edit", {"path": "a.py"}).family == ActionFamily.WRITE.value
    assert resolve_tool_action("memorize", {"type": "x"}).family == ActionFamily.WRITE.value
    assert resolve_tool_action("task_write", {}).family == ActionFamily.WRITE.value
    assert resolve_tool_action("grep", {"pattern": "x"}).family == ActionFamily.PROBE.value
    assert resolve_tool_action("glob", {"pattern": "x"}).family == ActionFamily.PROBE.value
    assert resolve_tool_action("list_dir", {"path": "."}).family == ActionFamily.PROBE.value
    assert resolve_tool_action("gitnexus_query", {}).family == ActionFamily.PROBE.value
    assert resolve_tool_action("impact_analysis", {"target": "x"}).family == ActionFamily.PROBE.value
    assert resolve_tool_action("run_bash", {"command": "ls"}).family == ActionFamily.EXECUTE.value
    assert resolve_tool_action("compact", {}).family == ActionFamily.TIDY.value


def test_resolve_tool_action_unknown_falls_back_to_tool_family() -> None:
    assert resolve_tool_action("new_tool", {"v": "1"}).family == ActionFamily.TOOL.value
    assert resolve_tool_action("server__mcp_thing", {"v": "1"}).family == ActionFamily.TOOL.value


def test_animation_families_pools_are_registered() -> None:
    from loom.platform.cli.ui import _ANIMATION_FRAMES

    # Every family resolves to a non-empty pool of real animation names.
    for family, pool in ANIMATION_FAMILIES.items():
        assert pool, f"family {family} has an empty pool"
        for name in pool:
            assert name in _ANIMATION_FRAMES, f"{name} missing from _ANIMATION_FRAMES"


def test_family_variants_unknown_falls_back_to_tool_pool() -> None:
    assert family_variants("not-a-family") == ANIMATION_FAMILIES[ActionFamily.TOOL.value]
    assert family_variants(ActionFamily.PROBE.value) == ANIMATION_FAMILIES[ActionFamily.PROBE.value]


def test_family_fps_pulse_is_gentle_rest_snappy() -> None:
    # Cadence travels with the family: THINK pulses gently, everything
    # else (and unknown families) gets the snappy default.
    assert family_fps(ActionFamily.THINK.value) == 6.0
    assert family_fps(ActionFamily.PROBE.value) == 10.0
    assert family_fps(ActionFamily.WRITE.value) == 10.0
    assert family_fps("not-a-family") == 10.0


def test_labels_use_locale_registry_shape() -> None:
    assert _LABELS_ZH_TW["read_file"][0] == "查詢檔案"
    assert _LABELS_ZH_TW["run_bash"][0] == "執行指令"


def test_resolve_tool_action_unknown_tool_falls_back_to_name() -> None:
    action = resolve_tool_action("new_tool", {"value": "abc"})
    assert action.label == "new_tool"
    assert action.subject == ""


def test_stale_threshold_defaults_and_long_runner_override() -> None:
    assert stale_threshold_for_tool("read_file") == 30.0
    assert stale_threshold_for_tool("run_bash") == 90.0
    assert stale_threshold_for_tool("gitnexus_query") == 90.0


def test_action_family_names_are_stable() -> None:
    # These are contract values the CLI footer + tests key off.
    assert ActionFamily.THINK.value == "think"
    assert ActionFamily.PROBE.value == "probe"
    assert ActionFamily.WRITE.value == "write"
    assert ActionFamily.EXECUTE.value == "execute"
    assert ActionFamily.TIDY.value == "tidy"
    assert ActionFamily.TOOL.value == "tool"


def test_long_tooling_state_removed() -> None:
    # #521 Gap A: LONG_TOOLING was never assigned anywhere (dead branch);
    # rising_columns moved to the WRITE family in #520. Removed.
    assert not hasattr(HeartbeatState, "LONG_TOOLING")


def test_advance_engagement_increments_same_family() -> None:
    e = Engagement()
    e = advance_engagement(e, ActionFamily.WRITE.value)
    assert e == Engagement(ActionFamily.WRITE.value, 1)
    e = advance_engagement(e, ActionFamily.WRITE.value)
    e = advance_engagement(e, ActionFamily.WRITE.value)
    assert e == Engagement(ActionFamily.WRITE.value, 3)


def test_advance_engagement_resets_run_on_family_change() -> None:
    prior = Engagement(ActionFamily.WRITE.value, 5)
    assert advance_engagement(prior, ActionFamily.PROBE.value) == Engagement(ActionFamily.PROBE.value, 1)


def test_advance_engagement_empty_family_resets_to_default() -> None:
    assert advance_engagement(Engagement(ActionFamily.WRITE.value, 5), "") == Engagement()


def test_advance_engagement_empty_then_real_starts_fresh() -> None:
    # An empty family fully resets, so the next real family starts a new
    # run at 1 (not a continuation of the pre-empty run).
    e = advance_engagement(Engagement(ActionFamily.WRITE.value, 5), "")
    e = advance_engagement(e, ActionFamily.WRITE.value)
    assert e == Engagement(ActionFamily.WRITE.value, 1)


def test_heartbeat_state_names_are_stable() -> None:
    assert HeartbeatState.THINKING.value == "thinking"
    assert HeartbeatState.STALLED.value == "stalled"


def test_parallel_reason_formats_as_human_label() -> None:
    assert format_parallel_reason("fan_out_replicas") == "多次驗證"
    assert format_parallel_reason("competing_strategies") == "多策略對比"


def test_synthesize_envelope_intent_uses_parallel_reason_and_dominant_tool() -> None:
    intent = synthesize_envelope_intent(
        parallel_reason="fan_out_replicas",
        tool_names=["pytest", "pytest", "pytest"],
    )
    assert intent == "重複跑同一組 pytest"


# ----------------------------------------------------------------------
# Mechanical envelope outcome derivation (#421)
# ----------------------------------------------------------------------


def test_derive_envelope_outcome_fulfilled_when_all_success_states() -> None:
    assert derive_envelope_outcome(["memorialized", "committed"]) == "fulfilled"


def test_derive_envelope_outcome_partial_when_mixed_success_and_failure() -> None:
    assert derive_envelope_outcome(["memorialized", "timed_out"]) == "partial"


def test_derive_envelope_outcome_unfulfilled_when_all_failed() -> None:
    assert derive_envelope_outcome(["denied", "timed_out"]) == "unfulfilled"


def test_derive_envelope_outcome_aborted_when_abort_alone() -> None:
    # Aborted is its own category — distinct from generic failure so
    # the renderer can show 🛑 instead of the generic ⚠ glyph.
    assert derive_envelope_outcome(["aborted"]) == "aborted"


def test_derive_envelope_outcome_aborted_with_failure_degrades_to_unfulfilled() -> None:
    # When aborted mixes with a real failure the helper falls back to
    # unfulfilled — user mostly cares that the batch didn't finish
    # cleanly, not the exact flavour of "didn't finish".
    assert derive_envelope_outcome(["aborted", "timed_out"]) == "unfulfilled"


def test_derive_envelope_outcome_never_mechanically_pivots() -> None:
    # Pivoted requires LLM-authored judgement; mechanical derivation
    # must never return it regardless of state combination.
    for states in (
        ["memorialized"],
        ["memorialized", "timed_out"],
        ["aborted", "memorialized"],
        ["denied", "aborted"],
    ):
        assert derive_envelope_outcome(states) != "pivoted"


def test_derive_envelope_outcome_handles_lowercase_normalization() -> None:
    # Be tolerant of upper-case ActionState.value strings — the ledger
    # path normalises but legacy producers may still emit raw names.
    assert derive_envelope_outcome(["MEMORIALIZED", "TIMED_OUT"]) == "partial"


def test_derive_envelope_outcome_empty_list_returns_fulfilled_for_callers_to_gate() -> None:
    # Documents the contract: the helper does not know whether an
    # envelope is terminal. Callers must only invoke it after the
    # batch has all-terminal states; in-flight inputs would otherwise
    # be misread as fulfilled. ``LedgerEnvelopeProjector`` gates on
    # ``status in ("completed", "failed")``.
    assert derive_envelope_outcome([]) == "fulfilled"


# ----------------------------------------------------------------------
# LivenessSensor — shared runtime-stall logic for CLI + Discord
# (convergence refactor; was split between bot.py and cli/app.py)
# ----------------------------------------------------------------------


class TestLivenessSensor:
    def test_injected_clock_is_used_for_defaults(self) -> None:
        clock = _FakeClock(100.0)
        sensor = LivenessSensor(clock=clock)
        # quiet_for with default now reads the injected clock.
        clock.t = 105.0
        assert sensor.quiet_for() == 5.0

    def test_observe_progress_resets_clock_and_clears_latch(self) -> None:
        clock = _FakeClock(0.0)
        sensor = LivenessSensor(default_threshold_s=30.0, clock=clock)
        clock.t = 40.0
        # Latch it.
        assert sensor.should_emit_stalled() is True
        # A genuine event resets the clock and clears the latch.
        assert sensor.observe() is True
        assert sensor.quiet_for() == 0.0
        # Latch cleared → not immediately re-stalled.
        assert sensor.is_stalled() is False

    def test_observe_is_progress_false_never_resets(self) -> None:
        # ActionStateChange analog: silent-by-design, never resets.
        clock = _FakeClock(0.0)
        sensor = LivenessSensor(clock=clock)
        clock.t = 10.0
        assert sensor.observe(is_progress=False) is False
        assert sensor.quiet_for() == 10.0

    def test_observe_same_fingerprint_does_not_reset(self) -> None:
        # Synthetic ~1Hz no-op EnvelopeUpdated analog: unchanged
        # fingerprint must not move the clock.
        clock = _FakeClock(0.0)
        sensor = LivenessSensor(clock=clock)
        assert sensor.observe(fingerprint="A") is True
        clock.t = 10.0
        assert sensor.observe(fingerprint="A") is False
        assert sensor.quiet_for() == 10.0

    def test_observe_changed_fingerprint_resets(self) -> None:
        clock = _FakeClock(0.0)
        sensor = LivenessSensor(clock=clock)
        sensor.observe(fingerprint="A")
        clock.t = 10.0
        assert sensor.observe(fingerprint="B") is True
        assert sensor.quiet_for() == 0.0

    def test_observe_none_fingerprint_always_resets(self) -> None:
        clock = _FakeClock(0.0)
        sensor = LivenessSensor(clock=clock)
        sensor.observe(fingerprint="A")
        clock.t = 5.0
        # No fingerprint supplied → treated as new progress.
        assert sensor.observe() is True
        assert sensor.quiet_for() == 0.0

    def test_should_emit_stalled_emits_once_then_latches(self) -> None:
        clock = _FakeClock(0.0)
        sensor = LivenessSensor(default_threshold_s=30.0, clock=clock)
        clock.t = 29.0
        assert sensor.should_emit_stalled() is False  # below threshold
        clock.t = 30.0
        assert sensor.should_emit_stalled() is True   # at threshold
        # Latched: a second tick at the same quiet stretch does not re-emit.
        assert sensor.should_emit_stalled() is False
        clock.t = 60.0
        assert sensor.should_emit_stalled() is False
        # …until the next observable event clears the latch.
        sensor.observe()
        clock.t = 95.0
        assert sensor.should_emit_stalled() is True

    def test_is_stalled_is_continuous_no_latch(self) -> None:
        clock = _FakeClock(0.0)
        sensor = LivenessSensor(default_threshold_s=30.0, clock=clock)
        clock.t = 30.0
        assert sensor.is_stalled() is True
        # Re-reading keeps returning True (no latch) while still quiet.
        clock.t = 31.0
        assert sensor.is_stalled() is True
        # is_stalled never sets the emit latch, so should_emit_stalled
        # can still fire.
        assert sensor.should_emit_stalled() is True

    def test_quiet_for_clamps_to_non_negative(self) -> None:
        clock = _FakeClock(10.0)
        sensor = LivenessSensor(clock=clock)  # last_observed = 10.0
        clock.t = 5.0  # clock went backwards (shouldn't happen, but clamp)
        assert sensor.quiet_for() == 0.0

    def test_suppress_blocks_emission_and_resume_resets_clock(self) -> None:
        clock = _FakeClock(0.0)
        sensor = LivenessSensor(default_threshold_s=30.0, clock=clock)
        clock.t = 100.0
        sensor.suppress()
        assert sensor.should_emit_stalled() is False
        assert sensor.is_stalled() is False
        # Resume clears suppression AND resets the clock so the
        # post-resume window does not inherit the pre-pause quiet time.
        sensor.resume()
        assert sensor.quiet_for() == 0.0
        assert sensor.should_emit_stalled() is False
        clock.t = 130.0
        assert sensor.should_emit_stalled() is True

    def test_set_threshold_per_tool_drives_decision_boundary(self) -> None:
        clock = _FakeClock(0.0)
        sensor = LivenessSensor(default_threshold_s=30.0, clock=clock)
        # Default 30s tool stalls at 30.
        clock.t = 30.0
        assert sensor.is_stalled() is True
        # Switch to a long-runner: 90s patience.
        sensor.set_threshold(tool_name="run_bash")
        assert stale_threshold_for_tool("run_bash") == 90.0
        clock.t = 60.0
        assert sensor.is_stalled() is False
        clock.t = 90.0
        assert sensor.is_stalled() is True

    def test_set_threshold_explicit_seconds_and_reset_to_default(self) -> None:
        clock = _FakeClock(0.0)
        sensor = LivenessSensor(default_threshold_s=30.0, clock=clock)
        sensor.set_threshold(seconds=10.0)
        clock.t = 10.0
        assert sensor.is_stalled() is True
        # No args → back to default.
        sensor.set_threshold()
        clock.t = 20.0
        assert sensor.is_stalled() is False
        clock.t = 30.0
        assert sensor.is_stalled() is True

    def test_reset_starts_fresh_quiet_stretch(self) -> None:
        clock = _FakeClock(0.0)
        sensor = LivenessSensor(default_threshold_s=30.0, clock=clock)
        sensor.set_threshold(tool_name="run_bash")  # 90s
        sensor.suppress()
        clock.t = 50.0
        sensor.should_emit_stalled()  # would latch if not suppressed
        sensor.reset()
        # Threshold back to default, suppression cleared, clock fresh.
        assert sensor.quiet_for() == 0.0
        clock.t = 80.0
        assert sensor.is_stalled() is True  # 80 >= default 30

    def test_resume_clears_suppression_even_when_pause_branch_raises(self) -> None:
        # Mirrors the Discord try/finally contract: even if the paused
        # branch raises, ``resume()`` in the finally must clear
        # suppression so it can't stick into the next state.
        clock = _FakeClock(0.0)
        sensor = LivenessSensor(default_threshold_s=30.0, clock=clock)
        sensor.suppress()
        try:
            raise RuntimeError("pause branch blew up")
        except RuntimeError:
            pass
        finally:
            sensor.resume()
        clock.t = 30.0
        assert sensor.should_emit_stalled() is True

    def test_stalled_emitted_property_tracks_latch(self) -> None:
        clock = _FakeClock(0.0)
        sensor = LivenessSensor(default_threshold_s=30.0, clock=clock)
        assert sensor.stalled_emitted is False
        clock.t = 30.0
        sensor.should_emit_stalled()
        assert sensor.stalled_emitted is True
        sensor.observe()
        assert sensor.stalled_emitted is False
