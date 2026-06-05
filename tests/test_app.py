"""
LoomApp unit tests (#249).

LoomApp is the persistent prompt_toolkit Application that owns the bottom
region of ``loom chat``. Its state machine — INPUT / CONFIRM / PAUSE /
REDIRECT modes plus a FooterState that drives the live footer line — is
2000+ lines of asyncio + UI plumbing with no dedicated test until now.

These tests don't drive the real Application's event loop (that would
need a TTY and a working renderer). Instead they exercise:

- the mode flag transitions around ``request_confirm`` / ``request_pause``
  / ``request_redirect_text`` (launched as tasks, completed by directly
  resolving the future the helper awaits)
- the render callbacks (``_render_footer`` / ``_render_tasklist`` /
  ``_render_confirm`` / ``_render_pause``) under the various
  FooterState shapes documented in the issue
- the TaskList state mutations (``update_tasklist`` collapse logic) and
  Markdown reblit infrastructure shared with #248

The aim is regression coverage: the next refactor that breaks one of
these contracts should fall over here, not in a user-facing surprise.
"""
from __future__ import annotations

import asyncio

import pytest
from prompt_toolkit.history import InMemoryHistory

from loom.core.events import TextChunk, TurnDropped
from loom.platform.cli import app as cli_app_module
from loom.platform.cli import main as cli_main
from loom.platform.cli.app import (
    FooterState,
    LoomApp,
    _ConfirmState,
    _PauseState,
    _TaskListState,
    build_loom_app,
)
from loom.platform.interaction_language import HeartbeatState


def _flat_text(formatted) -> str:
    """Concatenate all text fragments from a FormattedText for substring
    asserts. Style classes are dropped — we only care about visible
    output here."""
    return "".join(text for _style, text in formatted)


@pytest.fixture
def app() -> LoomApp:
    """Bare LoomApp with in-memory history. The Application object is
    constructed but never ``run()`` — we only exercise its state."""
    return LoomApp(history=InMemoryHistory())


# ---------------------------------------------------------------------------
# Construction + factory
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_mode_is_input(self, app: LoomApp) -> None:
        assert app.mode == "input"

    def test_footer_starts_empty(self, app: LoomApp) -> None:
        assert app.footer.token_pct == 0.0
        assert app.footer.compacting is False
        assert app.footer.grants_active == 0
        assert app.footer.heartbeat_state == HeartbeatState.IDLE.value
        assert app.footer.heartbeat_label == ""
        assert app.footer.heartbeat_subject == ""

    def test_factory_returns_loomapp(self) -> None:
        app = build_loom_app()
        assert isinstance(app, LoomApp)

    def test_factory_accepts_on_submit_callback(self) -> None:
        async def _on_submit(text: str) -> None:
            pass
        app = build_loom_app(on_submit=_on_submit)
        # _on_submit is held internally; can't introspect directly, but
        # the constructor accepting the callback is the contract
        assert app.mode == "input"


# ---------------------------------------------------------------------------
# Mode transitions — request_confirm / request_pause / request_redirect_text
# ---------------------------------------------------------------------------


class TestModeTransitions:
    """Each request_* helper flips the mode flag, awaits a future, and
    restores ``input`` mode in finally. Tests bypass the keybinding by
    resolving the future directly."""

    async def test_confirm_flips_mode_then_restores(self, app: LoomApp) -> None:
        task = asyncio.create_task(app.request_confirm(
            title="Allow run_bash?",
            body="ls /tmp",
            options=[("Yes", "yes", "y"), ("No", "no", "n")],
            cancel_value="cancel",
        ))
        # Yield once so request_confirm runs up to the await
        await asyncio.sleep(0)
        assert app.mode == "confirm"
        assert app._confirm_state is not None
        assert app._confirm_state.cursor == 0

        # Resolve the future as if the user picked the second option
        app._confirm_state.future.set_result("no")
        result = await task
        assert result == "no"
        assert app.mode == "input"
        assert app._confirm_state is None

    async def test_confirm_default_index_clamps_in_range(self, app: LoomApp) -> None:
        task = asyncio.create_task(app.request_confirm(
            title="Pick",
            body="",
            options=[("A", "a", None), ("B", "b", None)],
            default_index=99,  # out of range
            cancel_value=None,
        ))
        await asyncio.sleep(0)
        # Cursor must be clamped to the last valid index, not crash or wrap
        assert app._confirm_state.cursor == 1
        app._confirm_state.future.set_result("b")
        await task

    async def test_pause_flips_mode_then_restores(self, app: LoomApp) -> None:
        task = asyncio.create_task(app.request_pause(
            title="Paused",
            options=[("Resume", "resume", "r"), ("Cancel", "cancel", "c")],
            cancel_value="abort",
        ))
        await asyncio.sleep(0)
        assert app.mode == "pause"
        assert app._pause_state is not None

        app._pause_state.future.set_result("resume")
        result = await task
        assert result == "resume"
        assert app.mode == "input"
        assert app._pause_state is None

    async def test_redirect_flips_mode_and_focuses_buffer(self, app: LoomApp) -> None:
        task = asyncio.create_task(app.request_redirect_text())
        await asyncio.sleep(0)
        assert app.mode == "redirect"
        assert app._redirect_future is not None

        # Verify focus actually moved — this is the bug fixed in #266
        # (typed digits used to land in the hidden _input_buffer)
        assert app._app.layout.has_focus(app._redirect_buffer)

        app._redirect_buffer.text = "1"
        app._redirect_future.set_result("1")
        result = await task
        assert result == "1"
        assert app.mode == "input"
        # Buffer cleared on exit so next request_redirect_text starts fresh
        assert app._redirect_buffer.text == ""
        # Focus restored to input
        assert app._app.layout.has_focus(app._input_buffer)

    async def test_confirm_cancel_value_returned_on_explicit_set(self, app: LoomApp) -> None:
        task = asyncio.create_task(app.request_confirm(
            title="Allow?",
            body="",
            options=[("Yes", "yes", "y")],
            cancel_value="ESCAPED",
        ))
        await asyncio.sleep(0)
        # Mimic what the Esc handler does: resolve future with the stashed
        # cancel_value (the handler reads ``future._loom_cancel_value``)
        cancel = app._confirm_state.future._loom_cancel_value
        app._confirm_state.future.set_result(cancel)
        result = await task
        assert result == "ESCAPED"
        assert app.mode == "input"


# ---------------------------------------------------------------------------
# Footer rendering — exercises the FooterState branches
# ---------------------------------------------------------------------------


class TestFooterRender:
    """``_render_footer`` is the hottest render path — it ticks twice a
    second when anything's live. Lock down the visible output for each
    state branch documented in the issue."""

    def test_compacting_replaces_middle_with_spinner(self, app: LoomApp) -> None:
        app.footer.compacting = True
        app.footer.token_pct = 50.0  # would normally render
        text = _flat_text(app._render_footer())
        assert "壓縮中" in text
        # During compaction the budget / envelope info is suppressed —
        # only Loom brand + compaction message survive
        assert "context" not in text

    def test_token_pct_visible_above_zero(self, app: LoomApp) -> None:
        app.footer.token_pct = 42.5
        text = _flat_text(app._render_footer())
        assert "context 42.5%" in text

    def test_grants_seconds_format_under_one_minute(self, app: LoomApp) -> None:
        app.footer.grants_active = 1
        app.footer.grants_next_expiry_secs = 45
        text = _flat_text(app._render_footer())
        assert "🔑 1·0:45" in text

    def test_grants_minutes_format(self, app: LoomApp) -> None:
        app.footer.grants_active = 2
        app.footer.grants_next_expiry_secs = 65 + 7  # 1m 12s
        text = _flat_text(app._render_footer())
        assert "🔑 2·1:12" in text

    def test_grants_hours_format_above_sixty_minutes(self, app: LoomApp) -> None:
        app.footer.grants_active = 1
        # 75 minutes → 1h15m, not 75:00
        app.footer.grants_next_expiry_secs = 75 * 60
        text = _flat_text(app._render_footer())
        assert "🔑 1·1h15m" in text

    def test_grants_infinite_when_zero_ttl(self, app: LoomApp) -> None:
        # Session-scoped grants have valid_until=0 → ∞ display
        app.footer.grants_active = 3
        app.footer.grants_next_expiry_secs = 0
        text = _flat_text(app._render_footer())
        assert "🔑 3·∞" in text

    def test_heartbeat_replaces_active_envelope_label(self, app: LoomApp) -> None:
        # Heartbeat is the system-driven liveness signal that replaced the
        # raw ``▸ run_bash · 1.5s`` envelope label. Now the footer reads
        # the action-language label written by interaction_language.
        import time as _t

        app.footer.heartbeat_state = HeartbeatState.TOOLING.value
        app.footer.heartbeat_label = "執行指令"
        app.footer.heartbeat_subject = "pytest"
        app.footer.heartbeat_started_monotonic = _t.monotonic() - 8
        app.footer.heartbeat_last_event_monotonic = _t.monotonic()

        text = _flat_text(app._render_footer())

        assert "執行指令" in text
        assert "pytest" in text
        # Old raw glyph + tool name format must not leak back in
        assert "▸ run_bash" not in text
        assert app.footer.heartbeat_state == HeartbeatState.TOOLING.value

    def test_stalled_heartbeat_uses_warning_copy(self, app: LoomApp) -> None:
        # When the tool has been quiet past its stale_after_s the footer
        # tells the user we're still waiting. Only tooling states are
        # eligible — thinking / paused are waiting on someone else and
        # should never render this prefix (covered by other tests).
        import time as _t

        app.footer.heartbeat_state = HeartbeatState.TOOLING.value
        app.footer.heartbeat_label = "執行指令"
        app.footer.heartbeat_subject = "pytest"
        app.footer.heartbeat_started_monotonic = _t.monotonic() - 95
        app.footer.heartbeat_last_event_monotonic = _t.monotonic() - 95
        app.footer.heartbeat_stale_after_s = 90.0

        text = _flat_text(app._render_footer())

        assert "still waiting" in text
        assert "執行指令" in text

    def test_heartbeat_state_sequence_thinking_tooling_stalled_idle(self, app: LoomApp) -> None:
        # Sanity-check the full state machine the stream loop drives:
        # turn-start → THINKING; first ToolBegin → TOOLING; quiet past
        # stale_after_s → stalled prefix; turn-done → IDLE.
        import time as _t

        app.start_heartbeat(
            state=HeartbeatState.THINKING.value,
            label="Loom is thinking",
            stale_after_s=30.0,
        )
        assert app.footer.heartbeat_state == HeartbeatState.THINKING.value

        app.start_heartbeat(
            state=HeartbeatState.TOOLING.value,
            label="執行指令",
            subject="pytest",
            stale_after_s=90.0,
        )
        assert app.footer.heartbeat_state == HeartbeatState.TOOLING.value
        assert "執行指令" in _flat_text(app._render_footer())

        # Force the tool to look quiet past stale_after_s.
        app.footer.heartbeat_last_event_monotonic = _t.monotonic() - 91
        text = _flat_text(app._render_footer())
        assert "still waiting" in text

        # ``force=True`` bypasses the min-dwell guard so the test
        # doesn't need to sleep. The deferred-stop path has its own
        # dedicated tests below.
        app.stop_heartbeat(force=True)
        assert app.footer.heartbeat_state == HeartbeatState.IDLE.value

    def test_paused_blocking_heartbeat_does_not_render_stalled_prefix(self, app: LoomApp) -> None:
        # The PAUSED_BLOCKING state is waiting on the user, not on a
        # tool, so the footer must never call it "still waiting"
        # regardless of how long the human takes to decide.
        import time as _t

        app.start_heartbeat(
            state=HeartbeatState.PAUSED_BLOCKING.value,
            label="等待你的決定",
            stale_after_s=30.0,
        )
        app.footer.heartbeat_last_event_monotonic = _t.monotonic() - 120

        text = _flat_text(app._render_footer())
        assert "等待你的決定" in text
        assert "still waiting" not in text

    def test_transient_hint_does_not_disturb_active_heartbeat(self, app: LoomApp) -> None:
        # Regression for the codex review of PR #426: a CompressDone
        # event lands at the start of a turn before the LLM call (see
        # ``LoomSession.stream_turn`` draining ``_pending_compactions``).
        # The CLI fires a transient hint for it, but the heartbeat
        # must keep showing THINKING because the agent round-trip is
        # still pending. Any future helper that does both "show hint"
        # and "clear heartbeat" would break this invariant — this test
        # pins the contract so the regression can't slip back in.
        app.start_heartbeat(
            state=HeartbeatState.THINKING.value,
            label="Loom is thinking",
        )
        app.show_transient_hint("🗜 compacted → 42 facts", severity="info")
        assert app.footer.heartbeat_state == HeartbeatState.THINKING.value
        assert app.footer.heartbeat_label == "Loom is thinking"

    def test_thinking_heartbeat_does_not_render_stalled_prefix(self, app: LoomApp) -> None:
        # Mirror of the paused case: THINKING is waiting on the agent
        # (LLM round-trip). A slow first token is not "still waiting"
        # on a tool, so the stalled prefix must stay off.
        import time as _t

        app.start_heartbeat(
            state=HeartbeatState.THINKING.value,
            label="Loom is thinking",
            stale_after_s=30.0,
        )
        app.footer.heartbeat_last_event_monotonic = _t.monotonic() - 95

        text = _flat_text(app._render_footer())
        assert "Loom is thinking" in text
        assert "still waiting" not in text

    def test_last_turn_stats_hidden_while_heartbeat_active(self, app: LoomApp) -> None:
        # Stats from the previous turn surface only when nothing is in
        # flight — otherwise the heartbeat owns the middle column.
        app.footer.last_turn_input_tokens = 1234
        app.footer.last_turn_output_tokens = 567
        app.footer.last_turn_elapsed_s = 2.3
        text = _flat_text(app._render_footer())
        assert "1234in / 567out" in text

        # Now light up the heartbeat — stats should disappear.
        import time as _t
        app.footer.heartbeat_state = HeartbeatState.TOOLING.value
        app.footer.heartbeat_label = "執行指令"
        app.footer.heartbeat_subject = "x"
        app.footer.heartbeat_started_monotonic = _t.monotonic()
        app.footer.heartbeat_last_event_monotonic = _t.monotonic()
        text = _flat_text(app._render_footer())
        assert "1234in" not in text


# ---------------------------------------------------------------------------
# Heartbeat min-dwell — fast tools shouldn't flash
# ---------------------------------------------------------------------------


class TestHeartbeatMinDwell:
    """Regression: short tool calls (< 1 s) were producing labels that
    appeared and disappeared before the user could read them. The dwell
    guard holds the label for at least ``_HEARTBEAT_MIN_DWELL_S`` before
    a stop is allowed to clear it; an arriving ``start_heartbeat``
    (next tool, THINKING transition) overrides instantly as the
    "replaced by next action" case.
    """

    def test_stop_within_dwell_defers_not_clears(self, app: LoomApp) -> None:
        # User-visible regression case: a 0.3 s tool used to flash and
        # disappear. After the fix, the label stays as TOOLING until
        # the ticker promotes the pending stop.
        app.start_heartbeat(
            state=HeartbeatState.TOOLING.value,
            label="執行指令",
            subject="pytest",
            stale_after_s=90.0,
        )
        # Immediate stop simulates a sub-second tool.
        app.stop_heartbeat()
        assert app.footer.heartbeat_state == HeartbeatState.TOOLING.value
        assert app.footer.heartbeat_pending_stop is True
        assert "執行指令" in _flat_text(app._render_footer())

    def test_ticker_finalizes_stop_after_dwell_elapses(self, app: LoomApp) -> None:
        import time as _t

        app.start_heartbeat(
            state=HeartbeatState.TOOLING.value,
            label="執行指令",
            subject="pytest",
        )
        app.stop_heartbeat()
        assert app.footer.heartbeat_pending_stop is True

        # Simulate the dwell elapsing by pushing ``min_alive_until``
        # into the past. ``maybe_finalize_pending_stop`` is what the
        # footer_ticker calls every 0.5s, so this stands in for the
        # next tick after the dwell window.
        app.footer.heartbeat_min_alive_until = _t.monotonic() - 0.01
        app.maybe_finalize_pending_stop()

        assert app.footer.heartbeat_state == HeartbeatState.IDLE.value
        assert app.footer.heartbeat_pending_stop is False
        assert app.footer.heartbeat_label == ""

    def test_next_start_during_dwell_replaces_pending_stop(self, app: LoomApp) -> None:
        # "Replaced by next action" case — a fast pytest followed by
        # an immediate read_file should NOT freeze on pytest pending
        # for a full second; the new label should take over instantly.
        app.start_heartbeat(
            state=HeartbeatState.TOOLING.value,
            label="執行指令",
            subject="pytest",
        )
        app.stop_heartbeat()  # deferred
        assert app.footer.heartbeat_pending_stop is True

        app.start_heartbeat(
            state=HeartbeatState.TOOLING.value,
            label="查詢檔案",
            subject="loom/core/session.py",
        )
        # New label is live, pending_stop cleared so the dwell guard
        # restarts on this label rather than carrying forward the
        # previous one's state.
        assert app.footer.heartbeat_state == HeartbeatState.TOOLING.value
        assert app.footer.heartbeat_label == "查詢檔案"
        assert app.footer.heartbeat_pending_stop is False
        text = _flat_text(app._render_footer())
        assert "查詢檔案" in text
        assert "pytest" not in text

    def test_force_stop_bypasses_dwell(self, app: LoomApp) -> None:
        # Safety-net paths (turn_loop's outer finally, tests) need an
        # immediate clear that doesn't wait for the ticker.
        app.start_heartbeat(
            state=HeartbeatState.TOOLING.value,
            label="執行指令",
            subject="pytest",
        )
        app.stop_heartbeat(force=True)
        assert app.footer.heartbeat_state == HeartbeatState.IDLE.value
        assert app.footer.heartbeat_pending_stop is False

    def test_force_stop_clears_existing_pending_dwell(self, app: LoomApp) -> None:
        # A fast ToolEnd can legitimately enter pending dwell, but later
        # hard-boundary clears (TurnDone / outer cleanup / first text)
        # must still be able to release stale runtime truth immediately.
        app.start_heartbeat(
            state=HeartbeatState.TOOLING.value,
            label="執行指令",
            subject="pytest",
        )
        app.stop_heartbeat()
        assert app.footer.heartbeat_pending_stop is True

        app.stop_heartbeat(force=True)
        assert app.footer.heartbeat_state == HeartbeatState.IDLE.value
        assert app.footer.heartbeat_pending_stop is False
        assert app.footer.heartbeat_label == ""

    def test_write_family_renders_a_write_animation(self, app: LoomApp) -> None:
        # A write-family beat resolves to one of the write pool variants
        # (pen_stroke / rising_columns), so "writing a file" reads
        # differently from the probe spinner.
        from loom.platform.interaction_language import ActionFamily, family_variants

        app.start_heartbeat(
            state=HeartbeatState.TOOLING.value,
            label="寫入檔案",
            subject="loom/core/session.py",
            family=ActionFamily.WRITE.value,
        )
        assert app.footer.heartbeat_animation in family_variants(ActionFamily.WRITE.value)
        assert app.footer.heartbeat_family == ActionFamily.WRITE.value

    def test_family_variants_rotate_across_calls(self, app: LoomApp) -> None:
        # Two consecutive probe beats pick different pool variants — the
        # rotation that gives repeated same-family actions freshness.
        from loom.platform.interaction_language import ActionFamily

        seen = []
        for _ in range(3):
            app.start_heartbeat(
                state=HeartbeatState.TOOLING.value,
                label="搜尋",
                family=ActionFamily.PROBE.value,
            )
            seen.append(app.footer.heartbeat_animation)
        # PROBE has 3 variants — three consecutive picks are all distinct.
        assert len(set(seen)) == 3

    def test_write_linger_rearms_dwell_consumed_by_confirm(self, app: LoomApp) -> None:
        # Confirm-gated writes burn the original min-dwell while the user
        # reads the diff, so a plain ToolEnd stop would clear instantly —
        # the actual write moment would have no beat. ``linger_heartbeat``
        # re-arms the dwell at apply time so the write beat lingers.
        import time as _t

        from loom.platform.interaction_language import ActionFamily

        app.start_heartbeat(
            state=HeartbeatState.TOOLING.value,
            label="寫入檔案",
            subject="loom/core/session.py",
            family=ActionFamily.WRITE.value,
        )
        # Simulate the confirm dialog eating the dwell window.
        app.footer.heartbeat_min_alive_until = _t.monotonic() - 5.0

        # Apply-time re-arm (keyed off the write family), then the stop.
        assert app.footer.heartbeat_family == ActionFamily.WRITE.value
        app.linger_heartbeat(1.0)
        app.stop_heartbeat()

        assert app.footer.heartbeat_pending_stop is True
        assert "寫入檔案" in _flat_text(app._render_footer())

    def test_linger_heartbeat_is_noop_when_idle(self, app: LoomApp) -> None:
        # Nothing to hold open when the footer is already idle.
        app.linger_heartbeat(1.0)
        assert app.footer.heartbeat_state == HeartbeatState.IDLE.value
        assert app.footer.heartbeat_min_alive_until == 0.0

    def test_first_text_hard_boundary_clears_thinking_immediately(self, app: LoomApp) -> None:
        class _Session:
            _loom_app = app

            async def stream_turn(self, _user_input: str):
                yield TextChunk(text="hello")

        app.start_heartbeat(
            state=HeartbeatState.THINKING.value,
            label="Loom is thinking",
        )

        asyncio.run(cli_main._run_streaming_turn(_Session(), "hi"))

        assert app.footer.heartbeat_state == HeartbeatState.IDLE.value
        assert app.footer.heartbeat_pending_stop is False

    def test_turn_dropped_renders_cli_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[tuple[str, str]] = []

        class _Harness:
            def inline(self, message: str, *, level: str = "info") -> None:
                captured.append((level, message))

        class _Session:
            _loom_app = None

            async def stream_turn(self, _user_input: str):
                yield TurnDropped(
                    stop_reason="provider_ttfb_timeout",
                    exhausted=True,
                    provider_error_detail=(
                        "provider=Codex Responses; failure_type=ttfb_timeout; "
                        "phase=first_event_wait"
                    ),
                )

        monkeypatch.setattr(cli_main, "harness", _Harness())

        asyncio.run(cli_main._run_streaming_turn(_Session(), "hi"))

        assert captured == [(
            "error",
            "turn dropped: stop_reason=provider_ttfb_timeout; "
            "provider=Codex Responses; failure_type=ttfb_timeout; "
            "phase=first_event_wait",
        )]

    def test_stop_after_dwell_clears_immediately(self, app: LoomApp) -> None:
        # When a tool legitimately runs longer than the dwell, stop
        # should clear right away — no deferral needed.
        import time as _t

        app.start_heartbeat(
            state=HeartbeatState.TOOLING.value,
            label="執行指令",
            subject="pytest",
        )
        app.footer.heartbeat_min_alive_until = _t.monotonic() - 0.01
        app.stop_heartbeat()
        assert app.footer.heartbeat_state == HeartbeatState.IDLE.value
        assert app.footer.heartbeat_pending_stop is False


# ---------------------------------------------------------------------------
# TaskList floating panel
# ---------------------------------------------------------------------------


class TestTaskListPanel:
    def test_empty_list_renders_nothing(self, app: LoomApp) -> None:
        app.update_tasklist([])
        ft = app._render_tasklist()
        assert list(ft) == []

    def test_partial_list_renders_full_panel(self, app: LoomApp) -> None:
        app.update_tasklist([
            {"id": "a", "content": "first",  "status": "completed"},
            {"id": "b", "content": "second", "status": "in_progress"},
            {"id": "c", "content": "third",  "status": "pending"},
        ])
        text = _flat_text(app._render_tasklist())
        assert "📋 task list  1/3" in text
        assert "✓ first" in text
        assert "▸ second" in text
        assert "○ third" in text

    def test_in_progress_row_breathes_only_while_heartbeat_active(
        self, app: LoomApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli_app_module, "monotonic", lambda: 0.0)
        app.update_tasklist([
            {"id": "a", "content": "active", "status": "in_progress"},
        ])

        idle_text = _flat_text(app._render_tasklist())
        assert "▸ active" in idle_text

        app.start_heartbeat(
            state=HeartbeatState.THINKING.value,
            label="Loom is thinking",
        )
        active_text = _flat_text(app._render_tasklist())
        assert "⠂ active" in active_text

    def test_all_completed_collapses_to_one_liner(self, app: LoomApp) -> None:
        app.update_tasklist([
            {"id": "a", "content": "x", "status": "completed"},
            {"id": "b", "content": "y", "status": "completed"},
        ])
        assert app._tasklist_state.collapsed is True
        text = _flat_text(app._render_tasklist())
        assert "✓ 2/2 done" in text
        assert "📋" not in text  # collapsed view drops the header

    def test_re_writing_partial_list_unsets_collapsed(self, app: LoomApp) -> None:
        # Collapse, then add a new pending todo — should expand again
        app.update_tasklist([{"id": "a", "content": "x", "status": "completed"}])
        assert app._tasklist_state.collapsed is True

        app.update_tasklist([
            {"id": "a", "content": "x", "status": "completed"},
            {"id": "b", "content": "y", "status": "pending"},
        ])
        assert app._tasklist_state.collapsed is False
        text = _flat_text(app._render_tasklist())
        assert "1/2" in text

    def test_long_content_truncated(self, app: LoomApp) -> None:
        long = "x" * 200
        app.update_tasklist([
            {"id": "a", "content": long, "status": "pending"},
        ])
        text = _flat_text(app._render_tasklist())
        assert "…" in text  # truncation marker
        # Truncate cap is 56 in the source; allow some slack but no full 200
        assert "x" * 60 not in text

    def test_done_when_renders_dash_when_empty(self, app: LoomApp) -> None:
        # Issue #437: pending/in_progress rows always show the
        # criterion sub-line; "—" makes the absence visible.
        app.update_tasklist([
            {"id": "a", "content": "research", "status": "pending"},
        ])
        text = _flat_text(app._render_tasklist())
        assert "done when: —" in text

    def test_done_when_renders_text_when_filled(self, app: LoomApp) -> None:
        app.update_tasklist([
            {
                "id": "a",
                "content": "review",
                "status": "in_progress",
                "done_when": "P0–P3 findings each have file/line",
            },
        ])
        text = _flat_text(app._render_tasklist())
        assert "done when: P0–P3 findings each have file/line" in text

    def test_done_when_hidden_for_completed_rows(self, app: LoomApp) -> None:
        # Once completed, the ✓ is the answer — don't shame a missing
        # criterion retroactively. Mixed list: completed row hides
        # done_when, pending row still shows it.
        app.update_tasklist([
            {"id": "a", "content": "ship", "status": "completed"},
            {"id": "b", "content": "next", "status": "pending"},
        ])
        text = _flat_text(app._render_tasklist())
        # Exactly one "done when:" line — for the pending row only.
        assert text.count("done when:") == 1


# ---------------------------------------------------------------------------
# Confirm + Pause widget rendering
# ---------------------------------------------------------------------------


class TestWidgetRender:
    def test_confirm_render_contains_title_body_options(self, app: LoomApp) -> None:
        app._confirm_state = _ConfirmState(
            title="Allow tool",
            body="run_bash 'ls /tmp'",
            options=[("Yes", "yes", "y"), ("No", "no", "n")],
            cursor=0,
            
        )
        text = _flat_text(app._render_confirm())
        assert "Allow tool" in text
        assert "run_bash 'ls /tmp'" in text
        assert "Yes" in text
        assert "No" in text

    def test_confirm_render_marks_cursor_position(self, app: LoomApp) -> None:
        # cursor=1 → arrow on the second option
        app._confirm_state = _ConfirmState(
            title="Pick", body="",
            options=[("A", "a", None), ("B", "b", None)],
            cursor=1, 
        )
        text = _flat_text(app._render_confirm())
        # The cursor glyph is ``▸`` per _render_confirm
        a_idx = text.index("A")
        b_idx = text.index("B")
        # Cursor row sits before the cursored option; check the arrow
        # appears closer to B than to A
        arrow_idx = text.index("▸")
        assert arrow_idx > a_idx
        assert arrow_idx < b_idx

    def test_pause_render_contains_options(self, app: LoomApp) -> None:
        app._pause_state = _PauseState(
            title="Paused — tool batch finished",
            options=[
                ("Resume", "resume", "r"),
                ("Cancel turn", "cancel", "c"),
                ("Redirect", "redirect", "d"),
            ],
            cursor=0,
            
        )
        text = _flat_text(app._render_pause())
        assert "Resume" in text
        assert "Cancel turn" in text
        assert "Redirect" in text

    def test_confirm_render_returns_empty_when_state_none(self, app: LoomApp) -> None:
        app._confirm_state = None
        assert list(app._render_confirm()) == []

    def test_pause_render_returns_empty_when_state_none(self, app: LoomApp) -> None:
        app._pause_state = None
        assert list(app._render_pause()) == []
