import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.platform.discord.middleware import TaskWriteDiscordReminderMiddleware

# Mock classes for Discord to avoid needing the real discord.py in CI unless necessary
class MockDiscordChannel:
    def __init__(self):
        self.send = AsyncMock()

class MockDiscordClient:
    def __init__(self):
        self.get_channel = MagicMock()

class MockLoomSession:
    def __init__(self, config=None):
        self._loom_config = config or {}
        self._provisional_title = "Test Session"

@pytest.mark.asyncio
async def test_middleware_sends_embed_on_success():
    client = MockDiscordClient()
    channel = MockDiscordChannel()
    client.get_channel.return_value = channel

    # Configure session with discord_reminder = true
    session = MockLoomSession({"task_write": {"discord_reminder": True}})

    middleware = TaskWriteDiscordReminderMiddleware(client, 12345, session)

    call = ToolCall(
        tool_name="task_write",
        trust_level=TrustLevel.SAFE,
        session_id="test",
        args={
            "todos": [
                {"id": "task1", "content": "First task", "status": "completed"},
                {"id": "task2", "content": "Second task", "status": "in_progress"},
                {"id": "task3", "content": "Third task", "status": "pending"}
            ]
        }
    )

    # Mock tool handler that returns success
    async def mock_handler(c: ToolCall) -> ToolResult:
        return ToolResult(call_id=c.id, tool_name=c.tool_name, success=True)

    with patch('loom.platform.discord.middleware.discord.Embed') as MockEmbed:
        result = await middleware.process(call, mock_handler)

        assert result.success is True
        client.get_channel.assert_called_once_with(12345)
        
        # Verify an embed was created and sent
        MockEmbed.assert_called_once()
        kwargs = MockEmbed.call_args.kwargs
        assert "Test Session" in kwargs.get("title", "")
        
        desc = kwargs.get("description", "")
        assert "✅ **task1**: First task" in desc
        assert "▶️ **task2**: Second task" in desc
        assert "⬜ **task3**: Third task" in desc

        channel.send.assert_called_once()

@pytest.mark.asyncio
async def test_middleware_skips_when_config_false():
    client = MockDiscordClient()
    channel = MockDiscordChannel()
    client.get_channel.return_value = channel

    # Configure session with discord_reminder = false
    session = MockLoomSession({"task_write": {"discord_reminder": False}})
    middleware = TaskWriteDiscordReminderMiddleware(client, 12345, session)

    call = ToolCall(
        tool_name="task_write",
        trust_level=TrustLevel.SAFE,
        session_id="test",
        args={"todos": [{"id": "t1", "content": "task", "status": "pending"}]}
    )

    async def mock_handler(c: ToolCall) -> ToolResult:
        return ToolResult(call_id=c.id, tool_name=c.tool_name, success=True)

    with patch('loom.platform.discord.middleware.discord.Embed') as MockEmbed:
        await middleware.process(call, mock_handler)
        
        # Embed should not be sent
        MockEmbed.assert_not_called()
        channel.send.assert_not_called()

@pytest.mark.asyncio
async def test_middleware_skips_on_tool_failure():
    client = MockDiscordClient()
    channel = MockDiscordChannel()
    client.get_channel.return_value = channel

    session = MockLoomSession({"task_write": {"discord_reminder": True}})
    middleware = TaskWriteDiscordReminderMiddleware(client, 12345, session)

    call = ToolCall(
        tool_name="task_write",
        trust_level=TrustLevel.SAFE,
        session_id="test",
        args={"todos": [{"id": "t1", "content": "task", "status": "pending"}]}
    )

    # Mock tool handler that returns failure
    async def mock_handler(c: ToolCall) -> ToolResult:
        return ToolResult(call_id=c.id, tool_name=c.tool_name, success=False)

    with patch('loom.platform.discord.middleware.discord.Embed') as MockEmbed:
        await middleware.process(call, mock_handler)
        
        MockEmbed.assert_not_called()
        channel.send.assert_not_called()

@pytest.mark.asyncio
async def test_middleware_skips_non_task_write_tools():
    client = MockDiscordClient()
    channel = MockDiscordChannel()
    client.get_channel.return_value = channel

    session = MockLoomSession({"task_write": {"discord_reminder": True}})
    middleware = TaskWriteDiscordReminderMiddleware(client, 12345, session)

    call = ToolCall(
        tool_name="other_tool",
        trust_level=TrustLevel.SAFE,
        session_id="test",
        args={"some_arg": "value"}
    )

    async def mock_handler(c: ToolCall) -> ToolResult:
        return ToolResult(call_id=c.id, tool_name=c.tool_name, success=True)

    with patch('loom.platform.discord.middleware.discord.Embed') as MockEmbed:
        await middleware.process(call, mock_handler)
        
        MockEmbed.assert_not_called()
        channel.send.assert_not_called()
