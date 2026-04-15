"""
MCP Client — import tools from an external MCP server into Loom (Issue #9).

Connects to an MCP server (over stdio subprocess) and wraps each remote
tool as a Loom ``ToolDefinition`` so it appears in the session registry
like any built-in tool.

Usage
-----
In ``loom.toml``::

    [[mcp.servers]]
    name    = "filesystem"
    command = "npx"
    args    = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]

    [[mcp.servers]]
    name    = "github"
    command = "uvx"
    args    = ["mcp-server-git"]
    # env values support ${ENV_VAR} syntax for secrets kept in .env
    env     = { GITHUB_TOKEN = "${GITHUB_TOKEN}" }

Then in LoomSession.start(), ``_load_mcp_servers()`` is called
automatically to connect and register tools from each configured server.

Or manually::

    from loom.extensibility.mcp_client import LoomMCPClient
    client = LoomMCPClient(
        name="my-server",
        command="python",
        args=["-m", "my_mcp_server"],
    )
    tools = await client.connect_and_list_tools()
    for tool in tools:
        session.registry.register(tool)

Requirements
------------
    pip install loom[mcp]   # installs mcp>=1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

try:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.types import CallToolResult
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False


def _check_mcp() -> None:
    if not _MCP_AVAILABLE:
        raise ImportError(
            "MCP SDK not installed. Run: pip install 'loom[mcp]'"
        )


# ---------------------------------------------------------------------------
# Config data class (mirrors loom.toml [[mcp.servers]] entries)
# ---------------------------------------------------------------------------

@dataclass
class MCPServerConfig:
    """Configuration for one external MCP server."""
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    trust_level: str = "safe"   # safe | guarded — maps to Loom TrustLevel


_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _expand_env(value: str, extra_env: dict[str, str] | None = None) -> str:
    """
    Expand ${VAR} placeholders in *value*.

    Lookup order: *extra_env* (e.g. values from .env) first, then
    ``os.environ``.  Unset variables are replaced with the empty string.
    """
    merged = {**os.environ, **(extra_env or {})}

    def _replace(m: re.Match) -> str:
        return merged.get(m.group(1), "")

    return _ENV_VAR_PATTERN.sub(_replace, value)


def load_mcp_server_configs(
    config: dict,
    extra_env: dict[str, str] | None = None,
) -> list[MCPServerConfig]:
    """
    Parse ``[[mcp.servers]]`` entries from the loaded loom.toml dict.

    Environment-variable placeholders (``${VAR}``) in ``env`` values are
    expanded using *extra_env* (typically the dict returned by
    ``_load_env()``) merged over ``os.environ``, so secrets kept in
    ``.env`` are resolved without needing them injected into the process
    environment first.

    Returns an empty list if no MCP servers are configured.
    """
    raw = config.get("mcp", {}).get("servers", [])
    result: list[MCPServerConfig] = []
    for item in raw:
        try:
            raw_env: dict[str, str] = dict(item.get("env", {}))
            expanded_env: dict[str, str] = {
                k: _expand_env(v, extra_env) for k, v in raw_env.items()
            }
            result.append(MCPServerConfig(
                name=item.get("name", "unknown"),
                command=item.get("command", ""),
                args=list(item.get("args", [])),
                env=expanded_env,
                trust_level=item.get("trust_level", "safe"),
            ))
        except Exception as exc:
            logger.warning("mcp_client: invalid server config %r — %s", item, exc)
    return result


# ---------------------------------------------------------------------------
# LoomMCPClient
# ---------------------------------------------------------------------------

class LoomMCPClient:
    """
    Connects to one external MCP server and imports its tools as Loom
    ``ToolDefinition`` objects.

    Each remote tool becomes an async Loom tool that:
    1. Spawns (or reuses) a connection to the MCP subprocess
    2. Calls the remote tool via MCP ``tools/call``
    3. Returns the text result as a ``ToolResult``

    One client instance corresponds to one external MCP server process.
    The subprocess is launched lazily on first use and terminated on
    ``disconnect()``.
    """

    def __init__(self, cfg: MCPServerConfig) -> None:
        _check_mcp()
        self._cfg = cfg
        self._session: "ClientSession | None" = None
        self._cm: Any = None   # context manager for stdio_client
        self._read: Any = None
        self._write: Any = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def connect_and_list_tools(self) -> list:
        """
        Connect to the MCP server, list its tools, and return
        a list of Loom ``ToolDefinition`` objects ready to register.
        """
        from loom.core.harness.middleware import ToolCall, ToolResult
        from loom.core.harness.permissions import TrustLevel
        from loom.core.harness.registry import ToolDefinition

        await self._ensure_connected()
        assert self._session is not None

        result = await self._session.list_tools()
        tool_defs: list[ToolDefinition] = []

        # Keywords in a tool's name or description that signal mutation.
        # Used to assign MUTATES capability only to tools that actually write
        # state, rather than blanket-flagging all GUARDED tools in the server.
        _MUTATING_KEYWORDS = frozenset({
            "write", "create", "delete", "update", "patch", "put", "insert",
            "remove", "rename", "move", "overwrite", "append", "replace", "edit",
        })

        trust_level_str = self._cfg.trust_level.upper()
        try:
            trust = TrustLevel[trust_level_str]
        except KeyError:
            trust = TrustLevel.SAFE

        for mcp_tool in result.tools:
            tool_name = mcp_tool.name
            _client_ref = self  # close over client

            async def _executor(call: ToolCall, _name: str = tool_name) -> ToolResult:
                try:
                    call_result = await _client_ref._call_tool(_name, call.args)
                    if call_result.isError:
                        error_text = _extract_text(call_result)
                        return ToolResult(
                            call_id=call.id,
                            tool_name=_name,
                            success=False,
                            error=error_text,
                            failure_type="execution_error",
                        )
                    return ToolResult(
                        call_id=call.id,
                        tool_name=_name,
                        success=True,
                        output=_extract_text(call_result),
                    )
                except Exception as exc:
                    return ToolResult(
                        call_id=call.id,
                        tool_name=_name,
                        success=False,
                        error=f"MCP call failed: {exc}",
                        failure_type="execution_error",
                    )

            # Prefix tool names to avoid collision: "filesystem:list_files"
            prefixed_name = f"{self._cfg.name}:{tool_name}"
            desc = mcp_tool.description or f"MCP tool from {self._cfg.name}"
            schema = mcp_tool.inputSchema if mcp_tool.inputSchema else {
                "type": "object", "properties": {}
            }
            # Shallow-copy to avoid mutating the MCP-provided schema object.
            schema_dict = dict(schema if isinstance(schema, dict) else schema.model_dump())

            from loom.core.harness.registry import ToolCapability
            combined = f"{tool_name} {desc}".lower()
            is_mutating = trust == TrustLevel.CRITICAL or (
                trust == TrustLevel.GUARDED
                and any(kw in combined for kw in _MUTATING_KEYWORDS)
            )
            caps = ToolCapability.MUTATES if is_mutating else ToolCapability.NONE
            if is_mutating:
                props = dict(schema_dict.get("properties") or {})
                props["justification"] = {
                    "type": "string",
                    "description": "簡短說明為何在目前的脈絡下執行此工具是合理且必要的（給人類審核看）。",
                }
                schema_dict["properties"] = props
                existing_required = list(schema_dict.get("required") or [])
                if "justification" not in existing_required:
                    existing_required.append("justification")
                schema_dict["required"] = existing_required

            tool_defs.append(
                ToolDefinition(
                    name=prefixed_name,
                    description=f"[MCP/{self._cfg.name}] {desc}",
                    trust_level=trust,
                    capabilities=caps,
                    input_schema=schema_dict,
                    executor=_executor,
                    tags=["mcp", self._cfg.name],
                )
            )

        logger.info(
            "mcp_client: connected to %r, imported %d tool(s): %s",
            self._cfg.name,
            len(tool_defs),
            [t.name for t in tool_defs],
        )
        return tool_defs

    async def disconnect(self) -> None:
        """Close the connection to the MCP server subprocess.

        Suppresses all exceptions during __aexit__ so that:
        - stdio_client async-generator GC finalizer errors (which can fire
          in unrelated async contexts) do not propagate
        - Session shutdown is never derailed by a failing MCP cleanup
        See: "an error occurred during closing of async generator stdio_client"
        """
        if self._cm is not None:
            cm, self._cm = self._cm, None
            try:
                await cm.__aexit__(None, None, None)
            except BaseException:
                # Catch everything: Exception + GeneratorExit + CancelledError.
                # The anyio task group inside stdio_client may attempt cleanup
                # in a stale event-loop context; swallow the error silently.
                pass
            self._session = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _ensure_connected(self) -> None:
        async with self._lock:
            if self._session is not None:
                return

            # Merge override env on top of the full parent environment so the
            # subprocess retains PATH and other inherited vars.  Without this,
            # passing a non-None env dict to StdioServerParameters replaces the
            # entire subprocess environment and breaks PATH lookup (e.g. uvx).
            merged_env = {**os.environ, **self._cfg.env} if self._cfg.env else None
            params = StdioServerParameters(
                command=self._cfg.command,
                args=self._cfg.args,
                env=merged_env,
            )
            cm = stdio_client(params)
            read, write = await cm.__aenter__()
            try:
                session = ClientSession(read, write)
                await session.__aenter__()
                await session.initialize()
            except BaseException:
                # Clean up the stdio_client CM immediately so it is not
                # orphaned.  An un-exited anyio task group inside the CM
                # would later crash when Python's async-generator GC
                # finalises it in a different task.
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    pass
                raise
            self._cm = cm
            self._read, self._write = read, write
            self._session = session

    async def _call_tool(self, name: str, arguments: dict) -> "CallToolResult":
        await self._ensure_connected()
        assert self._session is not None
        return await self._session.call_tool(name, arguments)


def _extract_text(result: "CallToolResult") -> str:
    """Extract plain text from an MCP CallToolResult."""
    parts: list[str] = []
    for content in (result.content or []):
        if hasattr(content, "text"):
            parts.append(content.text)
        elif hasattr(content, "data"):
            parts.append(str(content.data))
    return "\n".join(parts) if parts else "(no output)"


# ---------------------------------------------------------------------------
# Session-level loader (called from LoomSession.start())
# ---------------------------------------------------------------------------

async def load_mcp_servers_into_session(
    config: dict,
    session: Any,
    extra_env: dict[str, str] | None = None,
) -> list[LoomMCPClient]:
    """
    Read ``[[mcp.servers]]`` from *config*, connect to each, and register
    the tools into *session*.

    Pass *extra_env* (the dict returned by ``_load_env()``) so that
    ``${VAR}`` placeholders in loom.toml env values are resolved against
    the .env file even when those variables are not in ``os.environ``.

    Returns the list of ``LoomMCPClient`` instances so the session can
    call ``disconnect()`` on shutdown.
    """
    server_configs = load_mcp_server_configs(config, extra_env)
    if not server_configs:
        return []

    clients: list[LoomMCPClient] = []
    for cfg in server_configs:
        if not cfg.command:
            logger.warning("mcp_client: server %r has no command — skipping", cfg.name)
            continue
        client = LoomMCPClient(cfg)
        try:
            tools = await client.connect_and_list_tools()
            for tool in tools:
                session.registry.register(tool)
            clients.append(client)
        except Exception as exc:
            logger.warning(
                "mcp_client: failed to connect to %r: %s — skipping",
                cfg.name, exc
            )
            # Ensure any partially-opened stdio_client CM is closed so
            # its anyio task group doesn't leak and crash later.
            await client.disconnect()
    return clients
