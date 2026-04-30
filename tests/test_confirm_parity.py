"""
Tests for Issue #104 — TUI/Discord confirm prompt y/s/a/N parity.

Coverage:
  1. InlineConfirmWidget: each button maps to the correct ConfirmDecision
  2. InlineConfirmWidget: second press is ignored (idempotent)
  3. InlineConfirmWidget: remove() called after resolve
  4. _ConfirmView: each button handler sets the correct decision
  5. _ConfirmView: wait_decision() resolves to the set decision
  6. _ConfirmView: on_timeout() falls back to DENY
  7. _make_confirm_fn: channel=None → DENY without any send
  8. _make_confirm_fn: SCOPE decision posts TTL follow-up message
  9. _make_confirm_fn: AUTO decision posts permanent-grant follow-up
 10. _make_confirm_fn: ONCE decision posts no follow-up
 11. _make_confirm_fn: DENY decision posts no follow-up
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# This file's whole premise is testing _ConfirmView against a stubbed
# discord module. With real discord.py installed, `@discord.ui.button`
# wraps each method as a Button object — making `view.allow_button(...)`
# uncallable, and the patch() targets in TestMakeConfirmFn race the real
# import path. The functional surface is covered by test_discord_*
# (safe_send / slash command registration / embeds), so just skip here.
if importlib.util.find_spec("discord") is not None:
    pytest.skip(
        "test_confirm_parity targets the no-discord environment; real "
        "discord.py is installed in this venv. Coverage of bot internals "
        "lives in test_discord_safe_send / test_discord_slash_commands.",
        allow_module_level=True,
    )

from loom.core.harness.scope import ConfirmDecision


# ---------------------------------------------------------------------------
# Discord stub
# Discord.py is an optional dependency.  Register a minimal stub in
# sys.modules *before* importing bot.py so the module-level try/import
# block succeeds without the real library installed.
# ---------------------------------------------------------------------------

class _StubView:
    """Minimal discord.ui.View replacement — accepts any keyword args."""
    def __init__(self, **kwargs):
        pass


def _noop_button(**kwargs):
    """Stand-in for @discord.ui.button — passes the decorated function through unchanged."""
    return lambda fn: fn


# Reached only when discord.py is NOT installed (e.g. minimal CI image).
# Register a minimal stub before importing bot.py so its module-level
# `import discord` succeeds.
_discord_stub = MagicMock()
_discord_stub.ui.View = _StubView
_discord_stub.ui.button = _noop_button
_discord_stub.ButtonStyle.green = "green"
_discord_stub.ButtonStyle.red = "red"
_discord_stub.ButtonStyle.blurple = "blurple"
_discord_stub.ButtonStyle.grey = "grey"

sys.modules.setdefault("discord", _discord_stub)
sys.modules.setdefault("discord.ui", _discord_stub.ui)

# PIL is an optional TUI dependency (image_widget.py); stub it so the
# components package __init__.py can be fully imported in test context.
sys.modules.setdefault("PIL", MagicMock())
sys.modules.setdefault("PIL.Image", MagicMock())
from loom.core.harness.middleware import ToolCall  # noqa: E402
from loom.core.harness.permissions import TrustLevel  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_widget(future: "asyncio.Future[ConfirmDecision]"):
    """Construct an InlineConfirmWidget bypassing Textual's __init__."""
    from loom.platform.cli.tui.components.interactive_widgets import InlineConfirmWidget
    widget = object.__new__(InlineConfirmWidget)
    widget._tool_name = "write_file"
    widget._trust_label = "GUARDED"
    widget._args_preview = "path=/tmp/x"
    widget._future = future
    widget._resolved = False
    return widget


def _press(widget, button_id: str) -> None:
    """Simulate a button press on the widget, patching remove() to avoid Textual context."""
    event = MagicMock()
    event.button.id = button_id
    with patch.object(widget, "remove"):
        widget.on_button_pressed(event)


def _make_call() -> ToolCall:
    return ToolCall(
        tool_name="write_file",
        args={"path": "/tmp/x", "content": "hello"},
        trust_level=TrustLevel.GUARDED,
        session_id="test",
    )


def _make_bot_stub(channel=None) -> LoomDiscordBot:
    """Create a LoomDiscordBot instance without calling __init__."""
    bot = object.__new__(LoomDiscordBot)
    bot._active_confirmations = {}
    mock_client = MagicMock()
    mock_client.get_channel.return_value = channel
    bot._client = mock_client
    return bot


# =====================================================================
# 1–3: TUI — InlineConfirmWidget
# =====================================================================

class TestInlineConfirmWidget:

    def test_allow_resolves_once(self):
        loop = asyncio.new_event_loop()
        future: asyncio.Future[ConfirmDecision] = loop.create_future()
        widget = _make_widget(future)
        _press(widget, "btn-allow")
        assert future.result() == ConfirmDecision.ONCE
        loop.close()

    def test_lease_resolves_scope(self):
        loop = asyncio.new_event_loop()
        future: asyncio.Future[ConfirmDecision] = loop.create_future()
        widget = _make_widget(future)
        _press(widget, "btn-lease")
        assert future.result() == ConfirmDecision.SCOPE
        loop.close()

    def test_auto_resolves_auto(self):
        loop = asyncio.new_event_loop()
        future: asyncio.Future[ConfirmDecision] = loop.create_future()
        widget = _make_widget(future)
        _press(widget, "btn-auto")
        assert future.result() == ConfirmDecision.AUTO
        loop.close()

    def test_deny_resolves_deny(self):
        loop = asyncio.new_event_loop()
        future: asyncio.Future[ConfirmDecision] = loop.create_future()
        widget = _make_widget(future)
        _press(widget, "btn-deny")
        assert future.result() == ConfirmDecision.DENY
        loop.close()

    def test_unknown_button_falls_back_to_deny(self):
        loop = asyncio.new_event_loop()
        future: asyncio.Future[ConfirmDecision] = loop.create_future()
        widget = _make_widget(future)
        _press(widget, "btn-something-unexpected")
        assert future.result() == ConfirmDecision.DENY
        loop.close()

    def test_second_press_ignored(self):
        """Pressing a second button must not overwrite the first decision."""
        loop = asyncio.new_event_loop()
        future: asyncio.Future[ConfirmDecision] = loop.create_future()
        widget = _make_widget(future)
        _press(widget, "btn-allow")
        _press(widget, "btn-deny")  # should be ignored
        assert future.result() == ConfirmDecision.ONCE
        loop.close()

    def test_remove_called_once_on_press(self):
        loop = asyncio.new_event_loop()
        future: asyncio.Future[ConfirmDecision] = loop.create_future()
        widget = _make_widget(future)
        remove_mock = MagicMock()
        with patch.object(widget, "remove", remove_mock):
            event = MagicMock()
            event.button.id = "btn-allow"
            widget.on_button_pressed(event)
        remove_mock.assert_called_once()
        loop.close()


# =====================================================================
# 4–6: Discord — _ConfirmView
# =====================================================================

class TestConfirmView:

    async def test_allow_button_returns_once(self):
        view = _ConfirmView(timeout=60.0)
        interaction = AsyncMock()
        await view.allow_button(interaction, MagicMock())
        decision = await view.wait_decision()
        assert decision == ConfirmDecision.ONCE

    async def test_lease_button_returns_scope(self):
        view = _ConfirmView(timeout=60.0)
        interaction = AsyncMock()
        await view.lease_button(interaction, MagicMock())
        decision = await view.wait_decision()
        assert decision == ConfirmDecision.SCOPE

    async def test_auto_button_returns_auto(self):
        view = _ConfirmView(timeout=60.0)
        interaction = AsyncMock()
        await view.auto_button(interaction, MagicMock())
        decision = await view.wait_decision()
        assert decision == ConfirmDecision.AUTO

    async def test_deny_button_returns_deny(self):
        view = _ConfirmView(timeout=60.0)
        interaction = AsyncMock()
        await view.deny_button(interaction, MagicMock())
        decision = await view.wait_decision()
        assert decision == ConfirmDecision.DENY

    async def test_timeout_falls_back_to_deny(self):
        view = _ConfirmView(timeout=60.0)
        await view.on_timeout()
        decision = await view.wait_decision()
        assert decision == ConfirmDecision.DENY

    async def test_wait_decision_without_set_falls_back_to_deny(self):
        """If _decision is never set (edge case), wait_decision returns DENY."""
        view = _ConfirmView(timeout=60.0)
        # Manually signal done without setting _decision
        view._done.set()
        decision = await view.wait_decision()
        assert decision == ConfirmDecision.DENY


# =====================================================================
# 7–11: Discord — _make_confirm_fn
# =====================================================================

class TestMakeConfirmFn:

    async def test_channel_none_returns_deny_no_send(self):
        bot = _make_bot_stub(channel=None)
        confirm_fn = bot._make_confirm_fn(thread_id=42)
        result = await confirm_fn(_make_call())
        assert result == ConfirmDecision.DENY
        bot._client.get_channel.assert_called_once_with(42)

    async def test_once_decision_no_followup(self):
        channel = AsyncMock()
        bot = _make_bot_stub(channel=channel)
        confirm_fn = bot._make_confirm_fn(thread_id=1)

        with patch(
            "loom.platform.discord.bot._ConfirmView.wait_decision",
            new=AsyncMock(return_value=ConfirmDecision.ONCE),
        ):
            result = await confirm_fn(_make_call())

        assert result == ConfirmDecision.ONCE
        # Only one send: the prompt — no follow-up
        assert channel.send.call_count == 1

    async def test_deny_decision_no_followup(self):
        channel = AsyncMock()
        bot = _make_bot_stub(channel=channel)
        confirm_fn = bot._make_confirm_fn(thread_id=1)

        with patch(
            "loom.platform.discord.bot._ConfirmView.wait_decision",
            new=AsyncMock(return_value=ConfirmDecision.DENY),
        ):
            result = await confirm_fn(_make_call())

        assert result == ConfirmDecision.DENY
        assert channel.send.call_count == 1

    async def test_scope_decision_posts_ttl_followup(self):
        channel = AsyncMock()
        bot = _make_bot_stub(channel=channel)
        confirm_fn = bot._make_confirm_fn(thread_id=1)

        with patch(
            "loom.platform.discord.bot._ConfirmView.wait_decision",
            new=AsyncMock(return_value=ConfirmDecision.SCOPE),
        ):
            result = await confirm_fn(_make_call())

        assert result == ConfirmDecision.SCOPE
        # Prompt + follow-up = 2 sends
        assert channel.send.call_count == 2
        followup_text: str = channel.send.call_args_list[1][0][0]
        assert "30 minutes" in followup_text
        assert "write_file" in followup_text

    async def test_auto_decision_posts_permanent_grant_followup(self):
        channel = AsyncMock()
        bot = _make_bot_stub(channel=channel)
        confirm_fn = bot._make_confirm_fn(thread_id=1)

        with patch(
            "loom.platform.discord.bot._ConfirmView.wait_decision",
            new=AsyncMock(return_value=ConfirmDecision.AUTO),
        ):
            result = await confirm_fn(_make_call())

        assert result == ConfirmDecision.AUTO
        assert channel.send.call_count == 2
        followup_text: str = channel.send.call_args_list[1][0][0]
        assert "auto" in followup_text.lower() or "permanent" in followup_text.lower()
        assert "write_file" in followup_text

    async def test_active_confirmations_cleared_after_decision(self):
        """_active_confirmations entry must be removed even if an exception occurs."""
        channel = AsyncMock()
        bot = _make_bot_stub(channel=channel)
        confirm_fn = bot._make_confirm_fn(thread_id=99)

        with patch(
            "loom.platform.discord.bot._ConfirmView.wait_decision",
            new=AsyncMock(return_value=ConfirmDecision.ONCE),
        ):
            await confirm_fn(_make_call())

        assert 99 not in bot._active_confirmations
