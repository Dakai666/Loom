"""`create_discord_forum_post` tool — validation + lifecycle contract tests.

The agent's contract: every code path returns a clean ToolResult, no
exceptions leak out. Live forum posting (real ForumChannel.create_thread,
real message splitting on the wire) is mocked here; the visual side still
wants one manual verification run.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.platform.discord.tools import (
    _FORUM_TITLE_MAX,
    _MESSAGE_CONTENT_MAX,
    _validate_forum_post_args,
    make_create_discord_forum_post_tool,
)


def _call(args: dict) -> ToolCall:
    return ToolCall(
        tool_name="create_discord_forum_post",
        args=args,
        trust_level=TrustLevel.GUARDED,
        session_id="test-session",
    )


# ── Validation ─────────────────────────────────────────────────────────────


def test_validate_rejects_missing_title():
    err = _validate_forum_post_args({"content": "hi"}, has_default_forum=True)
    assert err and "title" in err.lower()


def test_validate_rejects_empty_title():
    err = _validate_forum_post_args(
        {"title": "  ", "content": "hi"}, has_default_forum=True
    )
    assert err and "title" in err.lower()


def test_validate_rejects_missing_content():
    err = _validate_forum_post_args({"title": "t"}, has_default_forum=True)
    assert err and "content" in err.lower()


def test_validate_rejects_no_forum_id_without_default():
    err = _validate_forum_post_args(
        {"title": "t", "content": "c"}, has_default_forum=False
    )
    assert err and "forum" in err.lower()


def test_validate_accepts_explicit_forum_id_without_default():
    err = _validate_forum_post_args(
        {"title": "t", "content": "c", "forum_channel_id": 123},
        has_default_forum=False,
    )
    assert err is None


def test_validate_rejects_non_list_files():
    err = _validate_forum_post_args(
        {"title": "t", "content": "c", "files": "a.png"}, has_default_forum=True
    )
    assert err and "files" in err.lower()


def test_validate_rejects_non_list_tags():
    err = _validate_forum_post_args(
        {"title": "t", "content": "c", "tags": "news"}, has_default_forum=True
    )
    assert err and "tags" in err.lower()


def test_validate_accepts_overlong_title():
    # Over-length title is truncated at execution, not rejected.
    err = _validate_forum_post_args(
        {"title": "x" * (_FORUM_TITLE_MAX + 50), "content": "c"},
        has_default_forum=True,
    )
    assert err is None


# ── Lifecycle (mocked Discord) ───────────────────────────────────────────────


def _forum_with_thread():
    """A mock ForumChannel whose create_thread returns (thread, message)."""
    thread = MagicMock()
    thread.id = 999
    thread.jump_url = "https://discord.com/channels/1/2/999"
    thread.send = AsyncMock()

    created = MagicMock()
    created.thread = thread
    created.message = MagicMock()

    forum = MagicMock(spec=discord.ForumChannel)
    forum.available_tags = []
    forum.create_thread = AsyncMock(return_value=created)
    return forum, thread


async def test_happy_path_returns_thread_id_and_url(tmp_path: Path):
    forum, thread = _forum_with_thread()
    client = MagicMock()
    client.get_channel = MagicMock(return_value=forum)

    tool = make_create_discord_forum_post_tool(
        client, tmp_path, default_forum_id=1515
    )
    res = await tool.executor(_call({"title": "晨報", "content": "今天的新聞"}))

    assert res.success, res.error
    assert res.output["thread_id"] == 999
    assert res.output["jump_url"] == thread.jump_url
    forum.create_thread.assert_awaited_once()
    # Title + first chunk land on the post itself.
    kwargs = forum.create_thread.await_args.kwargs
    assert kwargs["name"] == "晨報"
    assert kwargs["content"] == "今天的新聞"
    # Short content → no follow-up sends.
    thread.send.assert_not_awaited()


async def test_overlong_title_truncated_on_post(tmp_path: Path):
    forum, _ = _forum_with_thread()
    client = MagicMock()
    client.get_channel = MagicMock(return_value=forum)

    tool = make_create_discord_forum_post_tool(
        client, tmp_path, default_forum_id=1515
    )
    res = await tool.executor(
        _call({"title": "y" * (_FORUM_TITLE_MAX + 30), "content": "c"})
    )
    assert res.success
    assert len(forum.create_thread.await_args.kwargs["name"]) == _FORUM_TITLE_MAX


async def test_long_content_splits_into_followups(tmp_path: Path):
    forum, thread = _forum_with_thread()
    client = MagicMock()
    client.get_channel = MagicMock(return_value=forum)

    tool = make_create_discord_forum_post_tool(
        client, tmp_path, default_forum_id=1515
    )
    # 2.5 messages worth of content → 1 on the post + 2 follow-ups.
    content = "a" * (_MESSAGE_CONTENT_MAX * 2 + 100)
    res = await tool.executor(_call({"title": "t", "content": content}))

    assert res.success
    assert thread.send.await_count == 2


async def test_explicit_forum_id_overrides_default(tmp_path: Path):
    forum, _ = _forum_with_thread()
    client = MagicMock()
    client.get_channel = MagicMock(return_value=forum)

    tool = make_create_discord_forum_post_tool(
        client, tmp_path, default_forum_id=1515
    )
    await tool.executor(
        _call({"title": "t", "content": "c", "forum_channel_id": 777})
    )
    client.get_channel.assert_called_once_with(777)


async def test_non_forum_channel_clean_error(tmp_path: Path):
    # A plain text channel is not a forum → clean tool error, no exception.
    not_forum = MagicMock(spec=discord.TextChannel)
    client = MagicMock()
    client.get_channel = MagicMock(return_value=not_forum)

    tool = make_create_discord_forum_post_tool(
        client, tmp_path, default_forum_id=1515
    )
    res = await tool.executor(_call({"title": "t", "content": "c"}))
    assert not res.success
    assert "forum" in res.error.lower()


async def test_channel_not_found_clean_error(tmp_path: Path):
    client = MagicMock()
    client.get_channel = MagicMock(return_value=None)
    client.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(), "nope"))

    tool = make_create_discord_forum_post_tool(
        client, tmp_path, default_forum_id=1515
    )
    res = await tool.executor(_call({"title": "t", "content": "c"}))
    assert not res.success
    assert res.error


async def test_missing_forum_id_clean_error(tmp_path: Path):
    client = MagicMock()
    tool = make_create_discord_forum_post_tool(
        client, tmp_path, default_forum_id=None
    )
    res = await tool.executor(_call({"title": "t", "content": "c"}))
    assert not res.success
    assert "forum" in res.error.lower()


async def test_file_outside_workspace_rejected(tmp_path: Path):
    forum, _ = _forum_with_thread()
    client = MagicMock()
    client.get_channel = MagicMock(return_value=forum)

    tool = make_create_discord_forum_post_tool(
        client, tmp_path, default_forum_id=1515
    )
    res = await tool.executor(
        _call({"title": "t", "content": "c", "files": ["../escape.png"]})
    )
    assert not res.success
    assert "workspace" in res.error.lower()
    forum.create_thread.assert_not_awaited()


async def test_attaches_workspace_file(tmp_path: Path):
    forum, _ = _forum_with_thread()
    client = MagicMock()
    client.get_channel = MagicMock(return_value=forum)
    (tmp_path / "quote.png").write_bytes(b"img")

    tool = make_create_discord_forum_post_tool(
        client, tmp_path, default_forum_id=1515
    )
    res = await tool.executor(
        _call({"title": "t", "content": "c", "files": ["quote.png"]})
    )
    assert res.success
    # File rides along on the post creation.
    assert forum.create_thread.await_args.kwargs.get("files")


def test_tool_definition_metadata(tmp_path: Path):
    client = MagicMock()
    tool = make_create_discord_forum_post_tool(client, tmp_path, default_forum_id=1)
    assert tool.name == "create_discord_forum_post"
    assert tool.trust_level == TrustLevel.GUARDED
    assert "title" in tool.input_schema["required"]
    assert "content" in tool.input_schema["required"]
