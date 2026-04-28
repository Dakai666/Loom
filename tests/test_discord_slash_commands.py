"""Issue #189 — slash commands surface.

Two layers under test:

* **`_cmd_*` backends** — each command body is now a pure-string helper on
  ``LoomDiscordBot``. We exercise them directly with mocked sessions so
  the same logic that the legacy text dispatcher and the new `/loom-*`
  slash dispatcher both call is locked down.

* **Registration** — `register_slash_commands` must attach all expected
  commands to the bot's tree and not blow up on import.

Live Discord interaction (autocomplete callbacks firing in a real client,
sync round-trip) still needs manual verification.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from loom.platform.discord.bot import LoomDiscordBot


# ── Helpers ──────────────────────────────────────────────────────────


def _bot() -> LoomDiscordBot:
    """Bare-bones bot instance — __init__ wires up the slash command tree
    so we get full registration coverage as a side effect."""
    return LoomDiscordBot(
        model="claude-opus-4-7",
        db_path="/tmp/loom-test-discord.db",
    )


def _fake_session(
    *,
    model: str = "claude-opus-4-7",
    personality: str | None = None,
    available_personalities: tuple[str, ...] = ("adversarial", "minimalist"),
    used: int = 1234,
    total: int = 200_000,
    grants: list | None = None,
    last_think: str | None = "thought trace…",
    strict_sandbox: bool = False,
):
    """Minimal stand-in matching the attributes _cmd_* helpers actually read."""
    session = MagicMock()
    session.session_id = "sess-test"
    session.model = model
    session.current_personality = personality
    session._stack = MagicMock()
    session._stack.available_personalities = MagicMock(return_value=list(available_personalities))
    session.budget = SimpleNamespace(
        usage_fraction=used / total if total else 0,
        used_tokens=used,
        total_tokens=total,
    )
    session.router = SimpleNamespace(providers=["anthropic", "minimax"])
    session._last_think = last_think
    session._strict_sandbox = strict_sandbox
    session.hitl_mode = False
    session.perm = MagicMock()
    session.perm.grants = grants or []
    session.perm.exec_auto = False
    session.perm._usage = {}
    session.perm.revoke_matching = MagicMock()
    return session


# ── Pure-string commands ─────────────────────────────────────────────


def test_help_text_lists_every_command():
    bot = _bot()
    text = bot._cmd_help()
    for cmd in (
        "/new", "/sessions", "/title", "/model", "/personality", "/think",
        "/compact", "/auto", "/pause", "/stop", "/budget", "/scope",
        "/summary", "/help",
    ):
        assert cmd in text


def test_think_returns_placeholder_when_empty():
    bot = _bot()
    out = bot._cmd_think(_fake_session(last_think=None))
    assert "no reasoning chain" in out


def test_think_renders_truncation_marker_for_long_chains():
    bot = _bot()
    out = bot._cmd_think(_fake_session(last_think="x" * 5000))
    assert "truncated" in out


def test_model_no_arg_shows_current_and_providers():
    bot = _bot()
    out = bot._cmd_model(_fake_session(), "")
    assert "claude-opus-4-7" in out
    assert "anthropic" in out


def test_model_switch_success():
    sess = _fake_session()
    sess.set_model = MagicMock(return_value=True)
    out = LoomDiscordBot._cmd_model(_bot(), sess, "claude-sonnet-4-6")
    sess.set_model.assert_called_once_with("claude-sonnet-4-6")
    assert "switched" in out.lower()


def test_model_switch_failure():
    sess = _fake_session()
    sess.set_model = MagicMock(return_value=False)
    out = LoomDiscordBot._cmd_model(_bot(), sess, "bogus-model")
    assert "Cannot switch" in out


def test_personality_off_clears():
    sess = _fake_session(personality="adversarial")
    sess.switch_personality = MagicMock(return_value=True)
    out = LoomDiscordBot._cmd_personality(_bot(), sess, "off")
    sess.switch_personality.assert_called_once_with("off")
    assert "cleared" in out.lower()


def test_personality_unknown_lists_available():
    sess = _fake_session()
    sess.switch_personality = MagicMock(return_value=False)
    out = LoomDiscordBot._cmd_personality(_bot(), sess, "ghost")
    assert "Unknown personality" in out
    assert "adversarial" in out  # available list surfaced


def test_auto_blocked_without_strict_sandbox():
    bot = _bot()
    out = bot._cmd_auto(_fake_session(strict_sandbox=False))
    assert "strict_sandbox" in out


def test_auto_toggles_when_sandboxed():
    bot = _bot()
    sess = _fake_session(strict_sandbox=True)
    out = bot._cmd_auto(sess)
    assert sess.perm.exec_auto is True
    assert "on" in out


def test_pause_toggles_state():
    bot = _bot()
    sess = _fake_session()
    sess.hitl_mode = False
    out = bot._cmd_pause(sess)
    assert sess.hitl_mode is True
    assert "on" in out


def test_stop_reports_no_running_turn():
    bot = _bot()
    out = bot._cmd_stop(channel_id=123)
    assert "nothing is running" in out


def test_stop_cancels_running_turn():
    bot = _bot()
    task = MagicMock()
    task.done = MagicMock(return_value=False)
    task.cancel = MagicMock()
    bot._running_turns[42] = task
    out = bot._cmd_stop(channel_id=42)
    task.cancel.assert_called_once()
    assert "Stopped" in out


def test_budget_renders_progress_bar():
    bot = _bot()
    out = bot._cmd_budget(_fake_session(used=50_000, total=200_000))
    assert "25.0%" in out
    assert "█" in out and "░" in out


def test_summary_shows_current_when_no_arg():
    bot = _bot()
    bot._summary_mode = "on"
    out = bot._cmd_summary("")
    assert "**on**" in out


def test_summary_sets_known_mode():
    bot = _bot()
    out = bot._cmd_summary("detail")
    assert bot._summary_mode == "detail"
    assert "detail" in out


def test_summary_rejects_unknown_mode():
    bot = _bot()
    bot._summary_mode = "on"
    out = bot._cmd_summary("bogus")
    assert bot._summary_mode == "on"  # unchanged
    assert "Unknown mode" in out


# ── /scope subcommands ───────────────────────────────────────────────


def _grant(action: str = "run_bash", selector: str = "*", ttl_remaining: float = 1800):
    return SimpleNamespace(
        action=action,
        resource="*",
        selector=selector,
        valid_until=time.time() + ttl_remaining if ttl_remaining > 0 else 0,
        source="lease",
    )


def test_scope_list_empty():
    bot = _bot()
    out = bot._cmd_scope(_fake_session(grants=[]), "list", "")
    assert "no active scope grants" in out


def test_scope_list_renders_table():
    bot = _bot()
    sess = _fake_session(grants=[_grant("run_bash"), _grant("memorize", "topic:*")])
    out = bot._cmd_scope(sess, "list", "")
    assert "run_bash" in out and "memorize" in out
    assert "TTL" in out


def test_scope_revoke_validates_id():
    bot = _bot()
    sess = _fake_session(grants=[_grant()])
    out = bot._cmd_scope(sess, "revoke", "")
    assert "Usage" in out
    out = bot._cmd_scope(sess, "revoke", "99")
    assert "Invalid grant ID" in out


def test_scope_revoke_success():
    bot = _bot()
    sess = _fake_session(grants=[_grant("run_bash")])
    out = bot._cmd_scope(sess, "revoke", "0")
    sess.perm.revoke_matching.assert_called_once()
    assert "Revoked" in out


def test_scope_clear():
    bot = _bot()
    grants = [_grant(), _grant()]
    sess = _fake_session(grants=grants)
    out = bot._cmd_scope(sess, "clear", "")
    assert sess.perm.grants == []
    assert "Cleared 2" in out


# ── Slash command registration ───────────────────────────────────────


def test_every_expected_loom_command_is_registered():
    """Catches typos / forgotten registrations on the slash side."""
    bot = _bot()
    registered = {cmd.name for cmd in bot._tree.get_commands()}
    expected = {
        "loom-help", "loom-new", "loom-sessions", "loom-think", "loom-compact",
        "loom-model", "loom-personality", "loom-auto", "loom-pause",
        "loom-stop", "loom-budget", "loom-title", "loom-summary", "loom-scope",
    }
    missing = expected - registered
    assert not missing, f"missing slash commands: {missing}"


def test_guilds_intent_enabled():
    """Slash command sync needs the `guilds` intent — regression guard."""
    bot = _bot()
    assert bot._client.intents.guilds is True
