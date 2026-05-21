from __future__ import annotations

from loom.platform.interaction_language import (
    HeartbeatState,
    _LABELS_ZH_TW,
    derive_envelope_outcome,
    format_elapsed,
    format_parallel_reason,
    resolve_tool_action,
    synthesize_envelope_intent,
    stale_threshold_for_tool,
)


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
