"""
Tests for Issue #104 — Discord confirm prompt y/s/a/N decision contract.

P3 refactor (Issue #347):
- Standalone _ConfirmView double avoids importing loom.platform.discord.bot.
- _make_confirm_fn logic tested via a pure-function fake (no discord imports).

TUI widget half of the parity suite was retired with the TUI subsystem
itself; the Discord side documents the canonical confirm contract.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.scope import ConfirmDecision


# =====================================================================
# Standalone _ConfirmView — same decision-state contract as the real
# loom.platform.discord.bot._ConfirmView, without importing discord.
# =====================================================================

class _ConfirmView:
    def __init__(self, timeout: float = 60.0):
        self._decision: ConfirmDecision | None = None
        self._done = asyncio.Event()
        self.timeout = timeout

    def _set_decision(self, decision: ConfirmDecision) -> None:
        if self._decision is None:
            self._decision = decision
            self._done.set()

    async def wait_decision(self) -> ConfirmDecision:
        await self._done.wait()
        return self._decision if self._decision is not None else ConfirmDecision.DENY

    async def allow_button(self, interaction, _button) -> None:
        await interaction.response.defer()
        self._set_decision(ConfirmDecision.ONCE)

    async def lease_button(self, interaction, _button) -> None:
        await interaction.response.defer()
        self._set_decision(ConfirmDecision.SCOPE)

    async def auto_button(self, interaction, _button) -> None:
        await interaction.response.defer()
        self._set_decision(ConfirmDecision.AUTO)

    async def deny_button(self, interaction, _button) -> None:
        await interaction.response.defer()
        self._set_decision(ConfirmDecision.DENY)

    async def on_timeout(self) -> None:
        self._set_decision(ConfirmDecision.DENY)


# =====================================================================
# Standalone _make_confirm_fn — replicates the contract of
# LoomDiscordBot._make_confirm_fn without importing discord.
# =====================================================================

async def _make_confirm_fn(
    *,
    get_channel,
    thread_id: int,
    active_confirmations: dict,
    view_class=_ConfirmView,
) -> "callable":
    """Returns a confirm_fn matching LoomDiscordBot._make_confirm_fn contract."""

    async def confirm_fn(call: ToolCall) -> ConfirmDecision:
        channel = get_channel(thread_id)
        if channel is None:
            return ConfirmDecision.DENY

        view = view_class(timeout=60.0)
        active_confirmations[thread_id] = view

        try:
            prompt_text = (
                f"🔐 **{call.tool_name.upper()}** needs confirmation\n"
                f"Trust: `{call.trust_level.plain}`\n"
                f"Args: `{call.args}`\n\n"
                f"y = Once | s = Scope (30 min) | a = Auto | N = Deny"
            )
            await channel.send(prompt_text, view=view)
            decision = await view.wait_decision()

            if decision == ConfirmDecision.SCOPE:
                await channel.send(
                    f"✅ Granted **{call.tool_name}** for 30 minutes (scoped)."
                )
            elif decision == ConfirmDecision.AUTO:
                await channel.send(
                    f"✅ Auto-approved **{call.tool_name}** (permanent grant)."
                )
            return decision
        finally:
            active_confirmations.pop(thread_id, None)

    return confirm_fn


# =====================================================================
# Helpers
# =====================================================================

def _make_call() -> ToolCall:
    return ToolCall(
        tool_name="write_file",
        args={"path": "/tmp/x", "content": "hello"},
        trust_level=TrustLevel.GUARDED,
        session_id="test",
    )


# =====================================================================
# 1–6: _ConfirmView — standalone decision-state
# =====================================================================

class TestConfirmView:

    async def test_allow_button_returns_once(self):
        view = _ConfirmView(timeout=60.0)
        await view.allow_button(AsyncMock(), MagicMock())
        assert await view.wait_decision() == ConfirmDecision.ONCE

    async def test_lease_button_returns_scope(self):
        view = _ConfirmView(timeout=60.0)
        await view.lease_button(AsyncMock(), MagicMock())
        assert await view.wait_decision() == ConfirmDecision.SCOPE

    async def test_auto_button_returns_auto(self):
        view = _ConfirmView(timeout=60.0)
        await view.auto_button(AsyncMock(), MagicMock())
        assert await view.wait_decision() == ConfirmDecision.AUTO

    async def test_deny_button_returns_deny(self):
        view = _ConfirmView(timeout=60.0)
        await view.deny_button(AsyncMock(), MagicMock())
        assert await view.wait_decision() == ConfirmDecision.DENY

    async def test_timeout_falls_back_to_deny(self):
        view = _ConfirmView(timeout=60.0)
        await view.on_timeout()
        assert await view.wait_decision() == ConfirmDecision.DENY

    async def test_wait_decision_without_set_falls_back_to_deny(self):
        """Simulate edge case: _done is signaled but _decision was never set.

        This mimics what happens when on_timeout() fires after _done was
        already set by some other path — fallback to DENY.
        """
        view = _ConfirmView(timeout=60.0)
        view._done.set()
        assert await view.wait_decision() == ConfirmDecision.DENY


# =====================================================================
# 7–12: _make_confirm_fn — channel routing + follow-up messages
# =====================================================================

class TestMakeConfirmFn:

    async def test_channel_none_returns_deny_no_send(self):
        confirm_fn = await _make_confirm_fn(
            get_channel=lambda tid: None,
            thread_id=42,
            active_confirmations={},
        )
        result = await confirm_fn(_make_call())
        assert result == ConfirmDecision.DENY

    async def test_once_decision_no_followup(self):
        channel = MagicMock(send=AsyncMock())
        active: dict = {}

        with patch.object(_ConfirmView, "wait_decision", return_value=ConfirmDecision.ONCE):
            confirm_fn = await _make_confirm_fn(
                get_channel=lambda tid: channel,
                thread_id=42,
                active_confirmations=active,
            )
            result = await confirm_fn(_make_call())

        assert result == ConfirmDecision.ONCE
        assert channel.send.call_count == 1
        assert 42 not in active

    async def test_deny_decision_no_followup(self):
        channel = MagicMock(send=AsyncMock())
        active: dict = {}

        with patch.object(_ConfirmView, "wait_decision", return_value=ConfirmDecision.DENY):
            confirm_fn = await _make_confirm_fn(
                get_channel=lambda tid: channel,
                thread_id=42,
                active_confirmations=active,
            )
            result = await confirm_fn(_make_call())

        assert result == ConfirmDecision.DENY
        assert channel.send.call_count == 1
        assert 42 not in active

    async def test_scope_decision_posts_ttl_followup(self):
        channel = MagicMock(send=AsyncMock())
        active: dict = {}

        with patch.object(_ConfirmView, "wait_decision", return_value=ConfirmDecision.SCOPE):
            confirm_fn = await _make_confirm_fn(
                get_channel=lambda tid: channel,
                thread_id=42,
                active_confirmations=active,
            )
            result = await confirm_fn(_make_call())

        assert result == ConfirmDecision.SCOPE
        assert channel.send.call_count == 2
        followup_text: str = channel.send.call_args_list[1][0][0]
        assert "30 minutes" in followup_text

    async def test_auto_decision_posts_permanent_grant_followup(self):
        channel = MagicMock(send=AsyncMock())
        active: dict = {}

        with patch.object(_ConfirmView, "wait_decision", return_value=ConfirmDecision.AUTO):
            confirm_fn = await _make_confirm_fn(
                get_channel=lambda tid: channel,
                thread_id=42,
                active_confirmations=active,
            )
            result = await confirm_fn(_make_call())

        assert result == ConfirmDecision.AUTO
        assert channel.send.call_count == 2
        followup_text: str = channel.send.call_args_list[1][0][0]
        assert "auto" in followup_text.lower() or "permanent" in followup_text.lower()

    async def test_active_confirmations_cleared_after_decision(self):
        channel = MagicMock(send=AsyncMock())
        active: dict = {}

        with patch.object(_ConfirmView, "wait_decision", return_value=ConfirmDecision.ONCE):
            confirm_fn = await _make_confirm_fn(
                get_channel=lambda tid: channel,
                thread_id=42,
                active_confirmations=active,
            )
            await confirm_fn(_make_call())

        assert 42 not in active
