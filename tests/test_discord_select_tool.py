"""Issue #190 — `send_discord_select` tool: validation + lifecycle tests.

Live Discord interaction (rendering the SelectMenu, real button clicks) is
exercised via mocks here; manual verification still required for the visual
side. These tests pin the contract that the **agent** sees: a clean tool
result on every code path, no exceptions leaking out.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import discord

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.platform.discord.tools import (
    _SELECT_MAX_OPTIONS,
    _SELECT_TIMEOUT_MAX,
    _validate_select_args,
    make_send_discord_select_tool,
)


def _call(args: dict) -> ToolCall:
    return ToolCall(
        tool_name="send_discord_select",
        args=args,
        trust_level=TrustLevel.SAFE,
        session_id="test-session",
    )


# ── Validation ───────────────────────────────────────────────────────────


def test_validate_rejects_missing_title():
    err = _validate_select_args({"options": [{"label": "L", "value": "v"}]})
    assert err and "title" in err.lower()


def test_validate_rejects_empty_options():
    err = _validate_select_args({"title": "t", "options": []})
    assert err and "non-empty" in err


def test_validate_rejects_too_many_options():
    opts = [{"label": f"L{i}", "value": f"v{i}"} for i in range(_SELECT_MAX_OPTIONS + 1)]
    err = _validate_select_args({"title": "t", "options": opts})
    assert err and str(_SELECT_MAX_OPTIONS) in err


def test_validate_rejects_duplicate_values():
    err = _validate_select_args({
        "title": "t",
        "options": [
            {"label": "A", "value": "x"},
            {"label": "B", "value": "x"},
        ],
    })
    assert err and "duplicate" in err.lower()


def test_validate_rejects_oversize_label():
    err = _validate_select_args({
        "title": "t",
        "options": [{"label": "x" * 101, "value": "v"}],
    })
    assert err and "label" in err.lower()


def test_validate_rejects_max_values_above_options():
    err = _validate_select_args({
        "title": "t",
        "options": [{"label": "A", "value": "a"}],
        "max_values": 2,
    })
    assert err and "max_values" in err


def test_validate_rejects_oversize_timeout():
    err = _validate_select_args({
        "title": "t",
        "options": [{"label": "A", "value": "a"}],
        "timeout_seconds": _SELECT_TIMEOUT_MAX + 1,
    })
    assert err and "timeout" in err


def test_validate_accepts_minimal_valid_args():
    err = _validate_select_args({
        "title": "t",
        "options": [{"label": "A", "value": "a"}],
    })
    assert err is None


# ── Tool definition shape ────────────────────────────────────────────────


def test_tool_definition_metadata():
    tool = make_send_discord_select_tool(MagicMock(), 123)
    assert tool.name == "send_discord_select"
    assert tool.trust_level == TrustLevel.SAFE
    assert "title" in tool.input_schema["required"]
    assert "options" in tool.input_schema["required"]


# ── Executor: error paths short-circuit before touching Discord ──────────


@pytest.mark.asyncio
async def test_executor_returns_validation_error_without_calling_discord():
    client = MagicMock()
    client.get_channel = MagicMock()
    tool = make_send_discord_select_tool(client, 123)

    result = await tool.executor(_call({"title": "t", "options": []}))
    assert not result.success
    assert "non-empty" in result.error
    client.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_executor_returns_error_when_channel_missing():
    client = MagicMock()
    client.get_channel = MagicMock(return_value=None)
    tool = make_send_discord_select_tool(client, 999)

    result = await tool.executor(_call({
        "title": "t",
        "options": [{"label": "A", "value": "a"}],
    }))
    assert not result.success
    assert "channel" in result.error.lower()


# ── Executor: happy path + cancellation via timeout ──────────────────────


def _patch_select_view(monkeypatch):
    """Replace `discord.ui.Select` and `discord.ui.View` with stand-ins that
    don't try to talk to a real Discord gateway. We capture the callback so
    the test can fire a synthetic interaction."""
    captured: dict = {}

    class _FakeSelect:
        def __init__(self, *, placeholder, min_values, max_values, options):
            self.values: list[str] = []
            self.callback = None
            captured["select"] = self
            captured["max_values"] = max_values

    class _FakeView:
        def __init__(self, *, timeout):
            self.timeout = timeout
            self.items = []

        def add_item(self, item):
            self.items.append(item)

    monkeypatch.setattr(discord.ui, "Select", _FakeSelect)
    monkeypatch.setattr(discord.ui, "View", _FakeView)

    class _FakeOption:
        def __init__(self, *, label, value, description=None):
            self.label = label
            self.value = value
            self.description = description

    monkeypatch.setattr(discord, "SelectOption", _FakeOption)
    return captured


@pytest.mark.asyncio
async def test_executor_returns_selection_after_user_picks(monkeypatch):
    captured = _patch_select_view(monkeypatch)

    sent_msg = MagicMock()
    sent_msg.edit = AsyncMock()

    channel = MagicMock()
    channel.send = AsyncMock(return_value=sent_msg)
    client = MagicMock()
    client.get_channel = MagicMock(return_value=channel)

    tool = make_send_discord_select_tool(client, 123)

    async def _drive_selection():
        # Wait until the executor wires the callback, then simulate a click.
        for _ in range(50):
            if "select" in captured and captured["select"].callback is not None:
                break
            await asyncio.sleep(0.01)
        select = captured["select"]
        select.values = ["b"]
        interaction = MagicMock()
        interaction.response = MagicMock()
        interaction.response.edit_message = AsyncMock()
        await select.callback(interaction)

    driver = asyncio.create_task(_drive_selection())
    result = await tool.executor(_call({
        "title": "Pick one",
        "options": [
            {"label": "Apple", "value": "a"},
            {"label": "Banana", "value": "b"},
        ],
        "timeout_seconds": 2,
    }))
    await driver

    assert result.success is True
    assert result.output == {"selected": "b", "label": "Banana"}


@pytest.mark.asyncio
async def test_executor_returns_cancelled_on_timeout(monkeypatch):
    _patch_select_view(monkeypatch)

    sent_msg = MagicMock()
    sent_msg.edit = AsyncMock()

    channel = MagicMock()
    channel.send = AsyncMock(return_value=sent_msg)
    client = MagicMock()
    client.get_channel = MagicMock(return_value=channel)

    tool = make_send_discord_select_tool(client, 123)

    result = await tool.executor(_call({
        "title": "Pick one",
        "options": [{"label": "A", "value": "a"}],
        "timeout_seconds": 0.05,
    }))

    assert result.success is True
    assert result.output == {"cancelled": True}


@pytest.mark.asyncio
async def test_executor_multi_select_returns_list(monkeypatch):
    captured = _patch_select_view(monkeypatch)

    sent_msg = MagicMock()
    sent_msg.edit = AsyncMock()

    channel = MagicMock()
    channel.send = AsyncMock(return_value=sent_msg)
    client = MagicMock()
    client.get_channel = MagicMock(return_value=channel)

    tool = make_send_discord_select_tool(client, 123)

    async def _drive_selection():
        for _ in range(50):
            if "select" in captured and captured["select"].callback is not None:
                break
            await asyncio.sleep(0.01)
        select = captured["select"]
        select.values = ["a", "c"]
        interaction = MagicMock()
        interaction.response = MagicMock()
        interaction.response.edit_message = AsyncMock()
        await select.callback(interaction)

    driver = asyncio.create_task(_drive_selection())
    result = await tool.executor(_call({
        "title": "Pick many",
        "options": [
            {"label": "A", "value": "a"},
            {"label": "B", "value": "b"},
            {"label": "C", "value": "c"},
        ],
        "max_values": 2,
        "timeout_seconds": 2,
    }))
    await driver

    assert result.success is True
    assert result.output == {"selected": ["a", "c"], "labels": ["A", "C"]}


# ── Active-tracking hook is exercised on success and timeout ─────────────


@pytest.mark.asyncio
async def test_register_unregister_called_on_timeout(monkeypatch):
    _patch_select_view(monkeypatch)

    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    client = MagicMock()
    client.get_channel = MagicMock(return_value=channel)

    registered: list[tuple[int, object]] = []
    unregistered: list[int] = []

    tool = make_send_discord_select_tool(
        client, 42,
        register_active=lambda tid, msg: registered.append((tid, msg)),
        unregister_active=lambda tid: unregistered.append(tid),
    )

    await tool.executor(_call({
        "title": "t",
        "options": [{"label": "A", "value": "a"}],
        "timeout_seconds": 0.05,
    }))

    assert len(registered) == 1 and registered[0][0] == 42
    assert unregistered == [42]
