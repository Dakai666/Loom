"""Issue #188 — embed v2 + reactions: validation, build, lifecycle.

The validation half also doubles as the #231 follow-up (oversize embeds
should produce clean tool errors instead of 50035s).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import discord

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.platform.discord.embeds import (
    COLOR_TIERS,
    DEFAULT_COLOR,
    EMBED_DESCRIPTION_MAX,
    EMBED_FIELDS_MAX,
    EMBED_TITLE_MAX,
    EMBED_TOTAL_MAX,
    build_embed,
    resolve_color,
    validate_embed_args,
)
from loom.platform.discord.reactions import REACTION, resolve
from loom.platform.discord.tools import (
    make_add_discord_reaction_tool,
    make_send_discord_embed_tool,
)


def _call(tool_name: str, args: dict) -> ToolCall:
    return ToolCall(
        tool_name=tool_name,
        args=args,
        trust_level=TrustLevel.SAFE,
        session_id="test",
    )


# ── resolve_color ────────────────────────────────────────────────────


def test_resolve_color_named_tier():
    assert resolve_color("alert") == COLOR_TIERS["alert"]
    assert resolve_color("INFO") == COLOR_TIERS["info"]


def test_resolve_color_hex_with_and_without_hash():
    assert resolve_color("#ff0000") == 0xff0000
    assert resolve_color("00ff00") == 0x00ff00


def test_resolve_color_int_passthrough():
    assert resolve_color(0xabcdef) == 0xabcdef


def test_resolve_color_none_or_empty_returns_default():
    assert resolve_color(None) == DEFAULT_COLOR
    assert resolve_color("") == DEFAULT_COLOR


def test_resolve_color_garbage_falls_back_to_default():
    assert resolve_color("not-a-color") == DEFAULT_COLOR


# ── validate_embed_args ──────────────────────────────────────────────


def test_validate_accepts_minimal_valid():
    assert validate_embed_args({"title": "t", "description": "d"}) is None


def test_validate_rejects_oversize_title():
    err = validate_embed_args({"title": "x" * (EMBED_TITLE_MAX + 1), "description": "d"})
    assert err and "title" in err.lower()


def test_validate_rejects_oversize_description():
    err = validate_embed_args({"title": "t", "description": "x" * (EMBED_DESCRIPTION_MAX + 1)})
    assert err and "description" in err.lower()


def test_validate_rejects_too_many_fields():
    fields = [{"name": f"n{i}", "value": "v"} for i in range(EMBED_FIELDS_MAX + 1)]
    err = validate_embed_args({"title": "t", "description": "d", "fields": fields})
    assert err and str(EMBED_FIELDS_MAX) in err


def test_validate_rejects_oversize_field_value():
    err = validate_embed_args({
        "title": "t", "description": "d",
        "fields": [{"name": "n", "value": "x" * 1025}],
    })
    assert err and "fields[0]" in err


def test_validate_rejects_total_over_6000():
    """Discord's 6000-char ceiling — the failure mode #231 was meant to prevent.
    All individual caps are within bounds; only the cross-field total trips."""
    # description maxes out at 4096; pile on enough field text to cross 6000
    # without violating any single-field cap.
    fields = [{"name": "n" * 100, "value": "v" * 1024} for _ in range(2)]
    err = validate_embed_args({
        "title": "t" * 100,
        "description": "x" * 4000,
        "fields": fields,
    })
    assert err and "6000" in err


def test_validate_rejects_wrong_types():
    assert "string" in (validate_embed_args({"title": 123, "description": "d"}) or "")
    assert "list" in (validate_embed_args({"title": "t", "description": "d", "fields": "nope"}) or "")


# ── build_embed ──────────────────────────────────────────────────────


def test_build_embed_uses_resolved_color():
    e = build_embed({"title": "t", "description": "d", "color": "alert"})
    assert e.color.value == COLOR_TIERS["alert"]


def test_build_embed_attaches_thumbnail_and_author():
    e = build_embed({
        "title": "t", "description": "d",
        "thumbnail": "https://example.com/icon.png",
        "author_name": "絲絲", "author_icon": "https://example.com/avatar.png",
        "footer": "fin", "timestamp": True,
        "fields": [{"name": "F", "value": "V", "inline": True}],
    })
    assert e.thumbnail.url == "https://example.com/icon.png"
    assert e.author.name == "絲絲"
    assert e.footer.text == "fin"
    assert e.timestamp is not None
    assert len(e.fields) == 1 and e.fields[0].inline is True


# ── send_discord_embed executor ──────────────────────────────────────


@pytest.mark.asyncio
async def test_send_embed_returns_validation_error_without_calling_discord():
    client = MagicMock()
    client.get_channel = MagicMock()
    tool = make_send_discord_embed_tool(client, 123)

    result = await tool.executor(_call(tool.name, {
        "title": "x" * (EMBED_TITLE_MAX + 1), "description": "d",
    }))
    assert not result.success
    assert "title" in result.error.lower()
    client.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_send_embed_happy_path():
    channel = MagicMock()
    channel.send = AsyncMock()
    client = MagicMock()
    client.get_channel = MagicMock(return_value=channel)
    tool = make_send_discord_embed_tool(client, 123)

    result = await tool.executor(_call(tool.name, {
        "title": "Hello", "description": "World", "color": "report",
    }))
    assert result.success
    channel.send.assert_awaited_once()
    sent_embed = channel.send.call_args.kwargs["embed"]
    assert sent_embed.color.value == COLOR_TIERS["report"]


# ── Reactions: resolve ───────────────────────────────────────────────


def test_resolve_shortcode_to_emoji():
    assert resolve("celebrate") == REACTION["celebrate"]
    assert resolve("done") == REACTION["done"]


def test_resolve_passes_through_raw_emoji():
    assert resolve("🎉") == "🎉"


def test_resolve_rejects_empty():
    with pytest.raises(ValueError):
        resolve("")
    with pytest.raises(ValueError):
        resolve("   ")


# ── add_discord_reaction tool ────────────────────────────────────────


@pytest.mark.asyncio
async def test_reaction_tool_uses_default_message_id():
    target = MagicMock()
    target.add_reaction = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=target)
    client = MagicMock()
    client.get_channel = MagicMock(return_value=channel)

    last_msg = {123: 999}
    tool = make_add_discord_reaction_tool(
        client, 123, last_user_message_lookup=last_msg.get,
    )

    result = await tool.executor(_call(tool.name, {"emoji": "celebrate"}))
    assert result.success
    channel.fetch_message.assert_awaited_once_with(999)
    target.add_reaction.assert_awaited_once_with(REACTION["celebrate"])
    assert result.output == {"emoji": REACTION["celebrate"], "message_id": 999}


@pytest.mark.asyncio
async def test_reaction_tool_explicit_message_id_overrides_default():
    target = MagicMock()
    target.add_reaction = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=target)
    client = MagicMock()
    client.get_channel = MagicMock(return_value=channel)

    tool = make_add_discord_reaction_tool(
        client, 123, last_user_message_lookup=lambda _: 999,
    )

    await tool.executor(_call(tool.name, {"emoji": "🎉", "message_id": 42}))
    channel.fetch_message.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_reaction_tool_errors_when_no_target_available():
    client = MagicMock()
    client.get_channel = MagicMock(return_value=MagicMock())
    tool = make_add_discord_reaction_tool(
        client, 123, last_user_message_lookup=lambda _: None,
    )

    result = await tool.executor(_call(tool.name, {"emoji": "🎉"}))
    assert not result.success
    assert "No target message" in result.error


@pytest.mark.asyncio
async def test_reaction_tool_validates_emoji():
    client = MagicMock()
    tool = make_add_discord_reaction_tool(
        client, 123, last_user_message_lookup=lambda _: 1,
    )
    result = await tool.executor(_call(tool.name, {"emoji": ""}))
    assert not result.success


def test_reaction_tool_description_mentions_expressive_shortcodes():
    """Agent has to be able to discover the vocabulary from the tool
    description alone — no out-of-band docs."""
    tool = make_add_discord_reaction_tool(
        MagicMock(), 123, last_user_message_lookup=lambda _: None,
    )
    assert "celebrate" in tool.description
    assert "puzzled" in tool.description
    # Lifecycle keys are bot-managed and should NOT appear in the agent-
    # facing vocabulary list to avoid duplicating bot reactions.
    assert "received=" not in tool.description
    assert "done=" not in tool.description
    assert "failed=" not in tool.description
