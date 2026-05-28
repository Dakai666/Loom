"""
Tests for ``LoomDiscordBot.deliver_chime`` resolving the ``circadian_today``
target type (issue #460, doc/57 §3.2).

The bot owns the indirection: the autonomy daemon registers chimes pointing
at ``circadian_today`` without a thread id (because it doesn't know one yet),
and the bot rewrites that to a concrete ``discord_thread`` target by reading
``CircadianState`` at delivery time. Without this layer the daily thread id
would have to be hard-coded in ``loom.toml`` every morning.

Existing ``discord_thread`` behaviour must not regress.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from loom.autonomy.chime import ChimeRequest
from loom.autonomy.circadian import state as st
from loom.autonomy.circadian.state import CircadianState, state_lock


@pytest.fixture(autouse=True)
def _circ_dir(tmp_path):
    st.set_dir_for_test(tmp_path / "circadian")
    yield
    st.set_dir_for_test(None)


def _make_bot_stub():
    """Build a minimal LoomDiscordBot without going through __init__."""
    from loom.platform.discord.bot import LoomDiscordBot

    bot = LoomDiscordBot.__new__(LoomDiscordBot)
    bot._sessions = {}
    bot._running_turns = {}
    bot._chime_pending = {}
    bot._chime_dispatcher = {}
    bot._client = MagicMock()
    bot._client.get_channel = MagicMock(return_value=None)
    bot._run_chime_turn = AsyncMock(return_value=None)
    return bot


def _seed_today_state(thread_id: int = 2024) -> CircadianState:
    state = CircadianState.new_for_today(
        thread_id=thread_id,
        session_id="today-session",
        channel_id=999,
        now=datetime.now(timezone.utc),
        tz="Asia/Taipei",
    )
    with state_lock():
        state.save_atomic()
    return state


def _chime(target: dict) -> ChimeRequest:
    return ChimeRequest(
        schedule_name="circadian:phase_dawn",
        intent="meaningful body",
        fired_at=datetime.now(timezone.utc),
        target=target,
    )


class TestCircadianTodayDispatch:
    @pytest.mark.asyncio
    async def test_resolves_today_thread_and_queues_chime(self):
        seeded = _seed_today_state(thread_id=2024)
        bot = _make_bot_stub()
        # The bot must already have a live session for the resolved thread —
        # this is the same invariant that ``discord_thread`` chimes enforce.
        bot._sessions[seeded.thread_id] = MagicMock()

        accepted = await bot.deliver_chime(_chime({"type": "circadian_today"}))
        assert accepted is True

        # The chime landed in today's thread, not under the literal
        # "circadian_today" label.
        assert seeded.thread_id in bot._chime_pending
        queued = bot._chime_pending[seeded.thread_id]["circadian:phase_dawn"]
        assert queued.target["type"] == "discord_thread"
        assert queued.target["id"] == str(seeded.thread_id)
        # Fallback is propagated, defaulting to skip when caller didn't set it.
        assert queued.target["fallback"] == "skip"
        # The original schedule_name + intent survive the rewrite.
        assert queued.schedule_name == "circadian:phase_dawn"
        assert queued.intent == "meaningful body"

    @pytest.mark.asyncio
    async def test_no_state_returns_false(self):
        bot = _make_bot_stub()
        accepted = await bot.deliver_chime(_chime({"type": "circadian_today"}))
        assert accepted is False
        assert bot._chime_pending == {}

    @pytest.mark.asyncio
    async def test_state_present_but_session_not_loaded_returns_false(self):
        """If state exists but the bot hasn't loaded today's session yet
        (cold start, mid-restart), dispatch must defer to the daemon's
        fallback — same gate as ``discord_thread`` chimes."""
        seeded = _seed_today_state(thread_id=3030)
        bot = _make_bot_stub()
        # _sessions is empty — no live session for the resolved thread.

        accepted = await bot.deliver_chime(_chime({"type": "circadian_today"}))
        assert accepted is False
        assert seeded.thread_id not in bot._chime_pending

    @pytest.mark.asyncio
    async def test_caller_supplied_fallback_is_preserved(self):
        seeded = _seed_today_state(thread_id=4040)
        bot = _make_bot_stub()
        bot._sessions[seeded.thread_id] = MagicMock()

        accepted = await bot.deliver_chime(
            _chime({"type": "circadian_today", "fallback": "independent"})
        )
        assert accepted is True
        queued = bot._chime_pending[seeded.thread_id]["circadian:phase_dawn"]
        assert queued.target["fallback"] == "independent"


class TestDiscordThreadRegression:
    """The existing ``discord_thread`` path must not be affected by the new
    dispatch arm — acceptance criterion #4 of issue #460."""

    @pytest.mark.asyncio
    async def test_plain_discord_thread_still_routes(self):
        bot = _make_bot_stub()
        bot._sessions[5050] = MagicMock()

        accepted = await bot.deliver_chime(
            _chime({"type": "discord_thread", "id": "5050"})
        )
        assert accepted is True
        assert 5050 in bot._chime_pending

    @pytest.mark.asyncio
    async def test_unknown_target_type_still_rejected(self):
        bot = _make_bot_stub()
        accepted = await bot.deliver_chime(_chime({"type": "cli", "id": "1"}))
        assert accepted is False
