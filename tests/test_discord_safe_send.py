"""Issue #231 — `_safe_send` must absorb the 2000-char limit so the turn
loop survives oversize payloads instead of bubbling 50035 errors out.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

import discord

from loom.platform.discord.bot import _safe_send, _MAX_CHARS


@pytest.mark.asyncio
async def test_safe_send_chunks_oversized_content():
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(spec=discord.Message))

    payload = "x" * (_MAX_CHARS * 3 + 17)  # forces ≥4 chunks
    await _safe_send(channel, payload)

    assert channel.send.call_count == 4
    for call in channel.send.call_args_list:
        sent = call.args[0]
        assert len(sent) <= _MAX_CHARS, f"chunk exceeded cap: {len(sent)}"


@pytest.mark.asyncio
async def test_safe_send_short_content_single_call():
    channel = MagicMock()
    channel.send = AsyncMock()

    await _safe_send(channel, "hello")
    channel.send.assert_awaited_once_with("hello")


@pytest.mark.asyncio
async def test_safe_send_swallows_50035_instead_of_raising():
    """The original bug: 50035 propagated out of channel.send and aborted
    the turn. _safe_send must catch HTTPException so the caller continues."""
    channel = MagicMock()
    response = MagicMock()
    response.status = 400
    response.reason = "Bad Request"
    err = discord.HTTPException(response, "50035: content too long")
    channel.send = AsyncMock(side_effect=err)

    # Must not raise.
    result = await _safe_send(channel, "anything")
    assert result is None


@pytest.mark.asyncio
async def test_safe_send_returns_last_message():
    """Callers that store the handle (status_msg, fake_msg) need the most
    recent message so subsequent edits land on the right target."""
    channel = MagicMock()
    msgs = [MagicMock(spec=discord.Message, name=f"m{i}") for i in range(3)]
    channel.send = AsyncMock(side_effect=msgs)

    payload = "y" * (_MAX_CHARS * 2 + 5)
    last = await _safe_send(channel, payload)

    assert channel.send.call_count == 3
    assert last is msgs[-1]


@pytest.mark.asyncio
async def test_safe_send_kwargs_only_attach_to_first_chunk():
    """view= / reference= must not duplicate across chunks — that would
    re-render the same buttons or reply pointer on every part."""
    channel = MagicMock()
    channel.send = AsyncMock()
    view = MagicMock()

    payload = "z" * (_MAX_CHARS * 2 + 1)
    await _safe_send(channel, payload, view=view)

    assert channel.send.call_count == 3
    # First call carries the view; subsequent calls do not.
    assert channel.send.call_args_list[0].kwargs == {"view": view}
    for call in channel.send.call_args_list[1:]:
        assert "view" not in call.kwargs


@pytest.mark.asyncio
async def test_max_chars_under_discord_hard_limit():
    """Headroom guard: the constant must stay strictly below Discord's
    2000-char cap so prefixes / continuation markers don't push us over."""
    assert _MAX_CHARS < 2000
