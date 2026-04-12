"""
MCP Server — expose Loom's tools as an MCP server (Issue #9).

Wraps the session's ``ToolRegistry`` in an MCP ``Server`` so any
MCP-compatible client (Claude Desktop, Cursor, Continue, etc.) can
call Loom's tools directly over the stdio transport.

Usage (stdio)
-------------
    loom mcp serve

This starts the MCP server on stdin/stdout using the official
``mcp`` SDK's stdio transport.  Connect from a client like Claude Desktop
by adding to ``claude_desktop_config.json``::

    {
      "mcpServers": {
        "loom": {
          "command": "loom",
          "args": ["mcp", "serve"],
          "env": {}
        }
      }
    }

The server surfaces all SAFE tools automatically.  GUARDED tools are
included but marked with a ``guarded=true`` annotation so clients can
apply their own confirmation flow.  CRITICAL tools are excluded.

Requirements
------------
    pip install loom[mcp]   # installs mcp>=1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        CallToolResult,
        TextContent,
        Tool,
    )
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False

if TYPE_CHECKING:
    from loom.core.harness.middleware import MiddlewarePipeline
    from loom.core.harness.registry import ToolRegistry

_LOOM_MCP_SERVER_NAME = "loom"
_LOOM_MCP_VERSION = "1.0"


def _check_mcp() -> None:
    if not _MCP_AVAILABLE:
        raise ImportError(
            "MCP SDK not installed. Run: pip install 'loom[mcp]'"
        )


def _registry_to_mcp_tools(registry: "ToolRegistry") -> list["Tool"]:
    """
    Convert Loom ``ToolDefinition`` objects to MCP ``Tool`` objects.

    CRITICAL tools are excluded.  GUARDED tools are marked with an
    annotation so clients can decide their own confirmation strategy.
    """
    from loom.core.harness.permissions import TrustLevel

    tools: list[Tool] = []
    for td in registry.list():
        if td.trust_level == TrustLevel.CRITICAL:
            continue  # never expose critical tools via MCP

        annotations: dict[str, Any] = {}
        if td.trust_level == TrustLevel.GUARDED:
            annotations["x-loom-guarded"] = True

        tools.append(
            Tool(
                name=td.name,
                description=td.description,
                inputSchema=td.input_schema,
                annotations=annotations or None,  # type: ignore[arg-type]
            )
        )
    return tools


async def run_mcp_server(
    registry: "ToolRegistry",
    pipeline: "MiddlewarePipeline | None" = None,
    session_id: str = "mcp",
) -> None:
    """
    Start an MCP server that exposes *registry* tools on stdio.

    This coroutine runs until the client disconnects or the process exits.
    Call it from ``loom mcp serve`` after the session is initialised.

    When *pipeline* is provided, every tool call is routed through
    the full middleware chain (scope checks, lifecycle, etc.) with
    ``origin="mcp"``.  Without it, calls fall back to direct executor
    invocation (legacy behavior) with a warning.
    """
    _check_mcp()

    import logging
    from loom.core.harness.middleware import ToolCall, ToolResult
    from loom.core.harness.permissions import TrustLevel

    _log = logging.getLogger(__name__)

    server = Server(_LOOM_MCP_SERVER_NAME)

    # ── list_tools handler ────────────────────────────────────────────
    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return _registry_to_mcp_tools(registry)

    # ── call_tool handler ─────────────────────────────────────────────
    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        tool_def = registry.get(name)
        if tool_def is None:
            return [TextContent(type="text", text=f"Error: unknown tool '{name}'")]

        if tool_def.trust_level == TrustLevel.CRITICAL:
            return [TextContent(type="text", text=f"Error: tool '{name}' is CRITICAL and cannot be called via MCP")]

        call = ToolCall(
            tool_name=name,
            args=arguments,
            trust_level=tool_def.trust_level,
            session_id=session_id,
            capabilities=tool_def.capabilities,
            origin="mcp",
        )
        try:
            if pipeline is not None:
                result: ToolResult = await pipeline.execute(call, tool_def.executor)
            else:
                _log.warning(
                    "MCP call to '%s' bypassing pipeline (no pipeline provided)", name,
                )
                result = await tool_def.executor(call)
        except Exception as exc:
            return [TextContent(type="text", text=f"Error executing '{name}': {exc}")]

        if result.success:
            text = result.output or ""
        else:
            text = f"Error: {result.error}"

        return [TextContent(type="text", text=text)]

    # ── run over stdio ────────────────────────────────────────────────
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
