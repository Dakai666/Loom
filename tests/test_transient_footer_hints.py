"""Issue #284 — transient footer hints (CLI).

Covers the LoomApp.show_transient_hint primitive: dedup, severity gating,
expiry-driven auto-clear, and the master config toggle. Trigger wiring
in main.py (TierChanged / CompressDone / context-budget / turn-milestone)
is verified end-to-end via the existing integration tests; here we focus
on the primitive's contract.
"""

from time import monotonic

import pytest
from prompt_toolkit.history import InMemoryHistory

from loom.platform.cli.app import LoomApp, _TransientHint


@pytest.fixture
def app():
    return LoomApp(history=InMemoryHistory())


def test_show_hint_sets_state(app):
    app.show_transient_hint("hello", severity="info", duration_s=2.0)
    h = app.footer.transient_hint
    assert isinstance(h, _TransientHint)
    assert h.text == "hello"
    assert h.severity == "info"
    assert h.expires_at > monotonic()


def test_show_hint_unknown_severity_falls_back_to_info(app):
    app.show_transient_hint("x", severity="critical")
    assert app.footer.transient_hint.severity == "info"


def test_dedup_key_suppresses_repeat(app):
    app.show_transient_hint("first", dedup_key="ctx_80")
    first_expiry = app.footer.transient_hint.expires_at
    # Second call with same key should be a no-op — state unchanged
    app.show_transient_hint("second", dedup_key="ctx_80")
    assert app.footer.transient_hint.text == "first"
    assert app.footer.transient_hint.expires_at == first_expiry


def test_dedup_keys_are_independent(app):
    app.show_transient_hint("ctx warn", dedup_key="ctx_80")
    app.show_transient_hint("turn marker", dedup_key="turn_50")
    # Last write wins on .transient_hint, but neither call was suppressed
    assert app.footer.transient_hint.text == "turn marker"
    # Repeat of either key is now suppressed
    app.show_transient_hint("ignored", dedup_key="ctx_80")
    assert app.footer.transient_hint.text == "turn marker"


def test_no_dedup_key_always_replaces(app):
    app.show_transient_hint("first")
    app.show_transient_hint("second")
    assert app.footer.transient_hint.text == "second"


def test_disabled_toggle_makes_show_a_noop(app):
    app.transient_hints_enabled = False
    app.show_transient_hint("should not appear")
    assert app.footer.transient_hint is None


def test_hint_visible_clears_expired(app):
    # Manually plant an already-expired hint to drive the filter past
    # its TTL without sleeping
    app.footer.transient_hint = _TransientHint(
        text="stale", severity="info",
        expires_at=monotonic() - 1.0,
    )
    assert app._hint_visible() is False
    # Filter must also clear the state so it doesn't linger
    assert app.footer.transient_hint is None


def test_hint_visible_true_while_active(app):
    app.show_transient_hint("active", duration_s=10.0)
    assert app._hint_visible() is True
    # Calling again shouldn't clear it
    assert app._hint_visible() is True
    assert app.footer.transient_hint is not None


def test_hint_visible_false_when_no_hint(app):
    assert app.footer.transient_hint is None
    assert app._hint_visible() is False


def test_render_hint_severity_drives_class(app):
    app.show_transient_hint("warning text", severity="warn", duration_s=10.0)
    parts = app._render_hint()
    assert len(parts) == 1
    cls, text = parts[0]
    assert cls == "class:hint.warn"
    assert "warning text" in text


def test_render_hint_returns_empty_when_none(app):
    parts = app._render_hint()
    assert list(parts) == []
