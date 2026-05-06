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
- the render callbacks (``_render_footer`` / ``_render_thinking`` /
  ``_render_tasklist`` / ``_render_confirm`` / ``_render_pause``) under
  the various FooterState shapes documented in the issue
- the TaskList state mutations (``update_tasklist`` collapse logic) and
  Markdown reblit infrastructure shared with #248

The aim is regression coverage: the next refactor that breaks one of
these contracts should fall over here, not in a user-facing surprise.
"""
from __future__ import annotations

import asyncio

import pytest
from prompt_toolkit.history import InMemoryHistory

from loom.platform.cli.app import (
    FooterState,
    LoomApp,
    _ActiveEnvelope,
    _ConfirmState,
    _PauseState,
    _TaskListState,
    build_loom_app,
)


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
        assert app.footer.thinking is False
        assert app.footer.compacting is False
        assert app.footer.grants_active == 0
        assert app.footer.active_envelopes == []

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

    def test_active_envelope_shown_with_elapsed(self, app: LoomApp) -> None:
        import time as _t
        app.footer.active_envelopes.append(
            _ActiveEnvelope(name="run_bash", started_monotonic=_t.monotonic() - 1.5)
        )
        text = _flat_text(app._render_footer())
        assert "▸ run_bash" in text
        # Elapsed format — at least the seconds suffix is fixed
        assert "s" in text.split("▸ run_bash")[1]

    def test_multiple_envelopes_show_count_prefix(self, app: LoomApp) -> None:
        import time as _t
        for name in ("read_file", "list_dir", "grep"):
            app.footer.active_envelopes.append(
                _ActiveEnvelope(name=name, started_monotonic=_t.monotonic())
            )
        text = _flat_text(app._render_footer())
        # ``Nx ▸ <latest> · <elapsed>`` — count visible, latest one named
        assert "3×" in text
        assert "grep" in text  # most recent

    def test_last_turn_stats_only_when_no_active_envelope(self, app: LoomApp) -> None:
        # Stats from the previous turn surface only when nothing is in
        # flight — otherwise the active envelope owns the middle column
        app.footer.last_turn_input_tokens = 1234
        app.footer.last_turn_output_tokens = 567
        app.footer.last_turn_elapsed_s = 2.3
        text = _flat_text(app._render_footer())
        assert "1234in / 567out" in text

        # Now add an active envelope — stats should disappear
        import time as _t
        app.footer.active_envelopes.append(
            _ActiveEnvelope(name="x", started_monotonic=_t.monotonic())
        )
        text = _flat_text(app._render_footer())
        assert "1234in" not in text


# ---------------------------------------------------------------------------
# Thinking indicator
# ---------------------------------------------------------------------------


class TestThinkingIndicator:
    def test_render_thinking_contains_loom_marker(self, app: LoomApp) -> None:
        text = _flat_text(app._render_thinking())
        assert "Loom is thinking" in text

    def test_thinking_flag_default_off(self, app: LoomApp) -> None:
        # ConditionalContainer reads ``footer.thinking`` directly; the
        # render only fires when True, but the flag default matters
        assert app.footer.thinking is False


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
