"""Tests for loom.core.ledger.recall — pure renderer functions.

Issue #385 Phase 1: ledger_recall narrative / summary / raw rendering.

The renderers operate on plain LedgerEvent + dict inputs, so these tests
construct events directly without spinning up a LedgerStore. The tool-
layer cross-database join is covered separately by the tool wiring tests.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from loom.core.ledger.recall import (
    TurnSlice,
    group_events_by_turn,
    infer_output_format,
    render_narrative,
    render_raw,
    render_summary,
)
from loom.core.ledger.schema import LedgerEvent


# ── Fixture helpers ───────────────────────────────────────────────────


_BASE_TS = datetime(2026, 5, 18, 14, 30, 0, tzinfo=timezone.utc).timestamp()


def _ev(
    *,
    et: str,
    ts_offset: float,
    turn_id: str = "turn_a",
    session_id: str = "sess_1",
    payload: dict | None = None,
    event_id: str | None = None,
) -> LedgerEvent:
    """Build a LedgerEvent at base + offset seconds. Payload defaults to {}."""
    return LedgerEvent(
        event_id=event_id or f"evt_{et}_{ts_offset:.3f}",
        session_id=session_id,
        turn_id=turn_id,
        correlation_id="corr_x",
        event_type=et,
        timestamp=_BASE_TS + ts_offset,
        payload=payload or {},
    )


def _tool_begin(call_id: str, tool: str, *, turn_id="turn_a", ts_offset=0.0) -> LedgerEvent:
    return _ev(
        et="tool_lifecycle", ts_offset=ts_offset, turn_id=turn_id,
        payload={
            "phase": "BEGIN",
            "tool_name": tool,
            "tool_call_id": call_id,
            "args_digest": "sha256:x",
        },
    )


def _tool_end(
    call_id: str, tool: str, *,
    turn_id="turn_a", ts_offset=1.0,
    error: str | None = None, rolled_back: bool = False,
) -> LedgerEvent:
    payload = {
        "phase": "END",
        "tool_name": tool,
        "tool_call_id": call_id,
        "args_digest": "sha256:x",
    }
    if error:
        payload["error"] = error
    if rolled_back:
        payload["rolled_back"] = True
    return _ev(
        et="tool_lifecycle", ts_offset=ts_offset, turn_id=turn_id,
        payload=payload,
    )


# ── group_events_by_turn ──────────────────────────────────────────────


def test_group_empty_returns_empty():
    assert group_events_by_turn([]) == []


def test_group_single_turn_with_bookends():
    events = [
        _ev(et="turn_start", ts_offset=0.0, payload={
            "prompt_stack_hash": "x", "prompt_stack_components": {},
        }),
        _tool_begin("c1", "run_bash", ts_offset=0.5),
        _tool_end("c1", "run_bash", ts_offset=1.2),
        _ev(et="turn_end", ts_offset=2.0, payload={
            "outcome": "clean", "duration_ms": 2000, "token_usage": {},
        }),
    ]
    slices = group_events_by_turn(events)
    assert len(slices) == 1
    sl = slices[0]
    assert sl.turn_id == "turn_a"
    assert sl.started_at == _BASE_TS
    assert sl.ended_at == _BASE_TS + 2.0
    assert sl.outcome == "clean"
    assert len(sl.events) == 4


def test_group_without_turn_start_falls_back_to_first_event_ts():
    events = [
        _tool_begin("c1", "tool_x", ts_offset=5.0),
        _tool_end("c1", "tool_x", ts_offset=6.0),
    ]
    slices = group_events_by_turn(events)
    assert len(slices) == 1
    assert slices[0].started_at == _BASE_TS + 5.0
    assert slices[0].ended_at is None
    assert slices[0].outcome is None


def test_group_multiple_turns_preserve_first_seen_order():
    events = [
        _tool_begin("c1", "a", turn_id="turn_x", ts_offset=0.0),
        _tool_begin("c2", "b", turn_id="turn_y", ts_offset=1.0),
        _tool_end("c1", "a", turn_id="turn_x", ts_offset=2.0),
    ]
    slices = group_events_by_turn(events)
    assert [sl.turn_id for sl in slices] == ["turn_x", "turn_y"]


# ── render_narrative ──────────────────────────────────────────────────


def test_narrative_empty_window():
    assert "沒有可回憶" in render_narrative([], {})


def test_narrative_single_turn_with_user_message():
    events = [
        _ev(et="turn_start", ts_offset=0.0, payload={
            "prompt_stack_hash": "x", "prompt_stack_components": {},
        }),
        _tool_begin("c1", "run_bash", ts_offset=0.1),
        _tool_end("c1", "run_bash", ts_offset=1.4),
    ]
    slices = group_events_by_turn(events)
    msgs = {"turn_a": [{"role": "user", "content": "幫我跑一下 ls"}]}
    out = render_narrative(slices, msgs)
    assert "## session sess_1" in out
    assert "2026-05-18" in out
    assert "第 1 輪" in out
    assert "14:30" in out
    assert "你說：幫我跑一下 ls" in out
    assert "run_bash" in out
    assert "1 events" in out


def test_narrative_user_message_truncated_at_cap():
    events = [_tool_end("c1", "tool_x", ts_offset=0.5)]
    slices = group_events_by_turn(events)
    long_text = "我" * 200
    msgs = {"turn_a": [{"role": "user", "content": long_text}]}
    out = render_narrative(slices, msgs)
    assert "…" in out
    # The original 200-char message should NOT appear intact.
    assert long_text not in out


def test_narrative_fallback_when_no_user_message():
    events = [_tool_end("c1", "tool_x", ts_offset=0.5)]
    slices = group_events_by_turn(events)
    out = render_narrative(slices, {})
    assert "沒有捕捉到你的訊息" in out


def test_narrative_tool_failure_shown_in_chain():
    events = [
        _tool_end("c1", "tool_a", ts_offset=0.5),
        _tool_end("c2", "tool_b", ts_offset=1.5, error="target index not found"),
    ]
    slices = group_events_by_turn(events)
    out = render_narrative(slices, {})
    assert "tool_a" in out
    assert "tool_b（失敗：target index not found）" in out
    assert "1成 1敗" in out


def test_narrative_rollback_counted_separately():
    events = [
        _tool_end("c1", "tool_a", ts_offset=0.5),
        _tool_end("c2", "tool_b", ts_offset=1.5, rolled_back=True),
    ]
    slices = group_events_by_turn(events)
    out = render_narrative(slices, {})
    assert "tool_b（rollback）" in out
    assert "1回滾" in out


def test_narrative_compacts_when_many_tools_and_not_verbose():
    events = [
        _tool_end(f"c{i}", f"tool_{i % 3}", ts_offset=i * 0.1)
        for i in range(12)
    ]
    slices = group_events_by_turn(events)
    out_compact = render_narrative(slices, {}, verbose=False)
    assert "12 件事" in out_compact
    out_verbose = render_narrative(slices, {}, verbose=True)
    assert "12 件事" not in out_verbose
    assert " → " in out_verbose


def test_narrative_groups_by_session():
    events = [
        _tool_end("c1", "tool_a", turn_id="turn_x",
                  ts_offset=0.5),
        _ev(et="tool_lifecycle", ts_offset=10.0,
            turn_id="turn_y", session_id="sess_2",
            payload={"phase": "END", "tool_name": "tool_b",
                     "tool_call_id": "c2", "args_digest": "sha256:y"}),
    ]
    slices = group_events_by_turn(events)
    out = render_narrative(slices, {})
    assert "## session sess_1" in out
    assert "## session sess_2" in out


def test_narrative_appends_non_clean_outcome():
    events = [
        _ev(et="turn_start", ts_offset=0.0, payload={
            "prompt_stack_hash": "x", "prompt_stack_components": {},
        }),
        _tool_end("c1", "tool_a", ts_offset=0.5),
        _ev(et="turn_end", ts_offset=1.0, payload={
            "outcome": "abandoned", "duration_ms": 1000, "token_usage": {},
        }),
    ]
    slices = group_events_by_turn(events)
    out = render_narrative(slices, {})
    assert "turn 結局 abandoned" in out


def test_narrative_turn_with_no_tools():
    events = [
        _ev(et="turn_start", ts_offset=0.0, payload={
            "prompt_stack_hash": "x", "prompt_stack_components": {},
        }),
        _ev(et="turn_end", ts_offset=1.0, payload={
            "outcome": "clean", "duration_ms": 1000, "token_usage": {},
        }),
    ]
    slices = group_events_by_turn(events)
    out = render_narrative(slices, {})
    assert "沒有動工具" in out


# ── render_summary ────────────────────────────────────────────────────


def test_summary_empty_window():
    assert "沒有可回憶" in render_summary([])


def test_summary_aggregates_tools_and_outcomes():
    events = [
        _tool_end("c1", "run_bash", ts_offset=0.5),
        _tool_end("c2", "run_bash", ts_offset=1.0, error="boom"),
        _tool_end("c3", "read_file", ts_offset=1.5),
        _ev(et="turn_end", ts_offset=2.0, payload={
            "outcome": "clean", "duration_ms": 2000, "token_usage": {},
        }),
        _ev(et="turn_end", ts_offset=3.0, turn_id="turn_b", payload={
            "outcome": "retry", "duration_ms": 1000, "token_usage": {},
        }),
    ]
    out = render_summary(events)
    assert "事件總數：5" in out
    assert "run_bash: 2 次（成功 1, 失敗 1, 50%）" in out
    assert "read_file: 1 次（成功 1, 失敗 0, 100%）" in out
    assert "clean: 1" in out
    assert "retry: 1" in out


def test_summary_counts_rollback_as_failure():
    events = [
        _tool_end("c1", "tool_a", ts_offset=0.5, rolled_back=True),
        _tool_end("c2", "tool_a", ts_offset=1.0),
    ]
    out = render_summary(events)
    assert "tool_a: 2 次（成功 1, 失敗 1, 50%）" in out


def test_summary_shows_window_bounds():
    events = [
        _tool_end("c1", "tool_a", ts_offset=0.0),
        _tool_end("c2", "tool_b", ts_offset=60.0),
    ]
    out = render_summary(events)
    assert "2026-05-18 14:30:00" in out
    assert "2026-05-18 14:31:00" in out


# ── render_raw ────────────────────────────────────────────────────────


def test_raw_empty_window():
    assert "沒有可回憶" in render_raw([])


def test_raw_one_line_per_event():
    events = [
        _tool_begin("c1", "tool_a", ts_offset=0.0),
        _tool_end("c1", "tool_a", ts_offset=1.0, error="boom"),
    ]
    out = render_raw(events)
    assert "Raw events (2 total)" in out
    assert "tool=tool_a phase=BEGIN" in out
    assert "phase=END" in out
    assert "err=boom" in out


def test_raw_truncates_to_max_events():
    events = [
        _tool_end(f"c{i}", "tool_x", ts_offset=i * 0.1)
        for i in range(10)
    ]
    out = render_raw(events, max_events=3)
    assert "10 total" in out
    assert "showing first 3" in out
    # Only 1 header line + 3 event lines
    assert len(out.splitlines()) == 4


# ── infer_output_format ───────────────────────────────────────────────


def test_infer_explicit_override_wins():
    assert infer_output_format(session_id="s", explicit="raw") == "raw"
    assert infer_output_format(skill_id="x", explicit="narrative") == "narrative"


def test_infer_session_or_correlation_picks_narrative():
    assert infer_output_format(session_id="s") == "narrative"
    assert infer_output_format(correlation_id="c") == "narrative"


def test_infer_other_dimensions_pick_summary():
    assert infer_output_format(skill_id="opencli") == "summary"
    assert infer_output_format(tool_name="run_bash") == "summary"
    assert infer_output_format(since="2026-05-01") == "summary"
    assert infer_output_format() == "summary"


def test_infer_ignores_invalid_explicit_value():
    assert infer_output_format(session_id="s", explicit="garbage") == "narrative"
