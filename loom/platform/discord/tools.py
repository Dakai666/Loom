"""
Discord-specific tools for the Loom Agent.
Provides capabilities to send files and rich embeds directly to the Discord thread.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

from loom.core.harness.registry import ToolDefinition
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.middleware import ToolCall, ToolResult

def make_send_discord_file_tool(client: discord.Client, thread_id: int, workspace: Path) -> ToolDefinition:
    async def executor(call: ToolCall) -> ToolResult:
        import discord as _discord
        channel = client.get_channel(thread_id)
        if channel is None:
            return ToolResult(call.id, call.tool_name, False, error="Discord channel/thread not found or accessible.")

        filepath = call.args.get("filepath", "")
        if not filepath:
            return ToolResult(call.id, call.tool_name, False, error="Missing 'filepath'.")

        target_path = (workspace / filepath).resolve()
        if not target_path.is_relative_to(workspace):
            return ToolResult(call.id, call.tool_name, False, error="Cannot access files outside the workspace.")

        if not target_path.exists() or not target_path.is_file():
            return ToolResult(call.id, call.tool_name, False, error=f"File not found or is a directory: {filepath}")

        try:
            await channel.send(file=_discord.File(str(target_path)))
            return ToolResult(call.id, call.tool_name, True, output=f"Successfully sent file {filepath} to Discord.")
        except Exception as e:
            return ToolResult(call.id, call.tool_name, False, error=f"Failed to send file: {e}")

    return ToolDefinition(
        name="send_discord_file",
        description="Send a file from your workspace directly into the current Discord thread. Use this if the user asks you to send them an image, document, or media file.",
        trust_level=TrustLevel.GUARDED,
        input_schema={
            "type": "object",
            "properties": {
                "filepath": {"type": "string", "description": "The relative path to the file in the workspace."},
            },
            "required": ["filepath"]
        },
        executor=executor
    )

def make_send_discord_embed_tool(client: discord.Client, thread_id: int) -> ToolDefinition:
    async def executor(call: ToolCall) -> ToolResult:
        import discord as _discord
        channel = client.get_channel(thread_id)
        if channel is None:
            return ToolResult(call.id, call.tool_name, False, error="Discord channel/thread not found or accessible.")

        title = call.args.get("title")
        description = call.args.get("description", "")
        color_hex = call.args.get("color", "#0099ff")
        fields = call.args.get("fields", [])

        try:
            color_int = int(str(color_hex).lstrip("#"), 16)
        except (ValueError, TypeError):
            color_int = 0x0099ff

        embed = _discord.Embed(title=title, description=description, color=color_int)
        
        if isinstance(fields, list):
            for field in fields:
                if isinstance(field, dict):
                    name = field.get("name", "Field")
                    value = field.get("value", "-")
                    inline = field.get("inline", False)
                    embed.add_field(name=str(name), value=str(value), inline=bool(inline))
            
        try:
            await channel.send(embed=embed)
            return ToolResult(call.id, call.tool_name, True, output="Successfully sent rich embed to Discord.")
        except Exception as e:
            return ToolResult(call.id, call.tool_name, False, error=f"Failed to send embed: {e}")

    return ToolDefinition(
        name="send_discord_embed",
        description="Send a beautiful rich embed panel to Discord. Use this to present structured data, summarize points, or give aesthetic feedback.",
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Title of the embed."},
                "description": {"type": "string", "description": "Main description text."},
                "color": {"type": "string", "description": "Hex color code (e.g. '#ff0000'). Default is '#0099ff'."},
                "fields": {
                    "type": "array",
                    "description": "Optional fields to add to the embed.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Field title."},
                            "value": {"type": "string", "description": "Field content."},
                            "inline": {"type": "boolean", "description": "Whether to display field inline."}
                        },
                        "required": ["name", "value"]
                    }
                }
            },
            "required": ["title", "description"]
        },
        executor=executor
    )
