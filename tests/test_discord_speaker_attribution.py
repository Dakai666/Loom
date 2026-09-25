"""Speaker attribution on Discord user turns.

When several people share one Loom agent (e.g. two friends in the same
thread), the agent must be told who is speaking — otherwise every turn
arrives unsigned and the agent can't tell users apart ("face blindness").
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from loom.platform.discord.bot import LoomDiscordBot, _attribute_speaker


def _author(display_name: str, name: str, uid: int = 42):
    return SimpleNamespace(display_name=display_name, name=name, id=uid, bot=False)


class TestAttributeSpeaker:
    def test_prefixes_display_name_and_username(self):
        out = _attribute_speaker("你好", _author("小明", "ming_01"))
        assert out == "[發話者：小明 (@ming_01)]\n你好"

    def test_omits_username_when_same_as_display_name(self):
        out = _attribute_speaker("hi", _author("ming_01", "ming_01"))
        assert out == "[發話者：ming_01]\nhi"

    def test_distinct_users_get_distinct_prefixes(self):
        a = _attribute_speaker("x", _author("小明", "ming"))
        b = _attribute_speaker("x", _author("小華", "hua"))
        assert a != b


def _bot_stub():
    bot = LoomDiscordBot.__new__(LoomDiscordBot)
    bot._run_turn = AsyncMock(return_value=None)
    bot._handle_slash = AsyncMock(return_value=None)
    bot._get_thread_session = AsyncMock(return_value=MagicMock())
    return bot


def _message(author):
    return SimpleNamespace(author=author, attachments=[], channel=MagicMock())


class TestHandleMessageAttribution:
    async def test_thread_turn_carries_speaker(self):
        bot = _bot_stub()
        msg = _message(_author("小明", "ming"))
        await bot._handle_message(msg, "今天天氣如何", is_thread=True)
        user_input = bot._run_turn.await_args.args[1]
        assert user_input == "[發話者：小明 (@ming)]\n今天天氣如何"

    async def test_slash_commands_are_not_prefixed(self):
        bot = _bot_stub()
        msg = _message(_author("小明", "ming"))
        await bot._handle_message(msg, "/summary off", is_thread=True)
        assert bot._handle_slash.await_args.args[1] == "/summary off"
        bot._run_turn.assert_not_awaited()
