"""
MCP Client — import tools from an external MCP server into Loom (Issue #9).

Connects to an MCP server and wraps each remote tool as a Loom
``ToolDefinition`` so it appears in the session registry like any built-in
tool.

Entries mirror a Claude Code ``.mcp.json`` ``mcpServers`` entry field for
field (Issue #595) — ``type`` / ``command`` / ``args`` / ``env`` / ``url`` /
``headers`` — so one server definition can be copied between the two.
``trust_level`` is the only Loom-specific field.

Usage
-----
In ``loom.toml``::

    [[mcp.servers]]
    name    = "filesystem"
    command = "npx"          # no ``type`` + ``command`` → stdio
    args    = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]

    [[mcp.servers]]
    name    = "substrate"
    type    = "http"         # stdio | http (Streamable HTTP) | sse
    url     = "http://127.0.0.1:7077/mcp"
    # Every string field supports ${VAR} and ${VAR:-default}, resolved
    # against .env first, then the process environment.
    headers = { Authorization = "${SUBSTRATE_AUTH_HEADER}" }

The ``instructions`` a server returns from ``initialize`` are rendered into
the system prompt by ``render_mcp_instructions()``, as Claude Code does.

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
    pip install loom[mcp]   # installs mcp>=1.24.0
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

try:
    from mcp.client.session import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    from mcp.types import CallToolResult
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False


# Logger the MCP SDK's stdio transport uses for its stdout reader
# (``mcp/client/stdio/__init__.py`` — ``logging.getLogger(__name__)``).
_STDIO_LOGGER_NAME = "mcp.client.stdio"


def _demote_stdio_noise(record: logging.LogRecord) -> bool:
    """Rewrite one stdio-reader record down to DEBUG, keeping it in the log.

    Demotion rather than a drop: ``mcp.client.stdio`` is a single
    process-wide logger shared by every client, and Loom runs one session
    (with its own MCP clients) per Discord thread.  A filter installed while
    thread A tears down is live for thread B too, so discarding records
    outright could swallow a genuine parse error from a server that really
    is corrupting its channel.  At DEBUG the record stops reaching console
    handlers but still lands anywhere debug logging is configured.
    """
    record.levelno = logging.DEBUG
    record.levelname = "DEBUG"
    return True


@contextlib.contextmanager
def _quiet_stdio_reader():
    """Demote the MCP SDK's stdio-reader output for the length of a teardown.

    Servers that ``print()`` a banner to stdout instead of stderr violate the
    stdio contract (stdout is JSON-RPC only), but the violation only becomes
    visible on the way out: a piped stdout is block-buffered, so the banner is
    flushed when the subprocess exits — while we are closing it — and the SDK's
    ``stdout_reader`` answers with a full ``logger.exception`` traceback for a
    line it cannot parse.  Observed with ``minimax-mcp`` and
    ``minimax-coding-plan-mcp`` (both do ``print("Starting Minimax MCP
    server")`` in ``main()``), where it lands right after the "Compressing
    session to memory…" rule and reads as if the memory pipeline had failed.

    Both places that close a ``stdio_client`` — ``disconnect()`` and the
    failed-handshake cleanup in ``_ensure_connected()`` — flush that banner,
    so both are wrapped.

    A filter rather than ``setLevel()``: it neither clobbers a level the user
    configured nor mis-restores one if two clients tear down at once.  The
    window is deliberately narrow, and nothing is discarded — see
    ``_demote_stdio_noise`` for why a shared logger must not be silenced
    outright.
    """
    log = logging.getLogger(_STDIO_LOGGER_NAME)
    log.addFilter(_demote_stdio_noise)
    try:
        yield
    finally:
        log.removeFilter(_demote_stdio_noise)


def _check_mcp() -> None:
    if not _MCP_AVAILABLE:
        raise ImportError(
            "MCP SDK not installed. Run: pip install 'loom[mcp]'"
        )


# ---------------------------------------------------------------------------
# Config data class (mirrors loom.toml [[mcp.servers]] entries)
# ---------------------------------------------------------------------------

_TRANSPORTS = frozenset({"stdio", "http", "sse"})

# Per-server cap on rendered ``instructions`` — generous for a usage guide,
# small enough that one server cannot crowd out the rest of the prompt.
MCP_INSTRUCTIONS_MAX_CHARS = 4000


@dataclass
class MCPServerConfig:
    """Configuration for one external MCP server."""
    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    trust_level: str = "safe"   # safe | guarded — maps to Loom TrustLevel
    type: str = "stdio"         # stdio | http | sse
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


# ${VAR} or ${VAR:-default}
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


def _expand_env(
    value: str,
    extra_env: dict[str, str] | None = None,
    missing: list[str] | None = None,
) -> str:
    """
    Expand ``${VAR}`` and ``${VAR:-default}`` placeholders in *value*.

    Lookup order: *extra_env* (e.g. values from .env) first, then
    ``os.environ``.  ``:-default`` applies when the variable is unset or
    empty, as in the shell.  An unset variable without a default expands to
    the empty string and, when *missing* is given, its name is appended so
    the caller can refuse the config instead of sending a blank secret.
    """
    merged = {**os.environ, **(extra_env or {})}

    def _replace(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        value = merged.get(name)
        if default is not None:
            return value or default
        if value is None:
            if missing is not None:
                missing.append(name)
            return ""
        return value

    return _ENV_VAR_PATTERN.sub(_replace, value)


def load_mcp_server_configs(
    config: dict,
    extra_env: dict[str, str] | None = None,
) -> list[MCPServerConfig]:
    """
    Parse ``[[mcp.servers]]`` entries from the loaded loom.toml dict.

    Environment-variable placeholders (``${VAR}`` / ``${VAR:-default}``) in
    ``command``, ``args``, ``env``, ``url`` and ``headers`` are expanded
    using *extra_env* (typically the dict returned by ``_load_env()``)
    merged over ``os.environ``, so secrets kept in ``.env`` are resolved
    without needing them injected into the process environment first.

    Without ``type``, a ``command`` means stdio and a ``url`` means http.
    An entry that references an unset variable with no default, or lacks
    what its transport needs, is skipped with a warning — like Claude Code,
    which refuses such a config rather than connecting with a blank secret.

    Returns an empty list if no MCP servers are configured.
    """
    raw = config.get("mcp", {}).get("servers", [])
    result: list[MCPServerConfig] = []
    for item in raw:
        try:
            name = item.get("name", "unknown")
            missing: list[str] = []

            def _x(value: Any) -> str:
                return _expand_env(str(value), extra_env, missing)

            transport = item.get("type") or (
                "stdio" if item.get("command") else "http" if item.get("url") else ""
            )
            cfg = MCPServerConfig(
                name=name,
                type=transport,
                command=_x(item.get("command", "")),
                args=[_x(a) for a in item.get("args", [])],
                env={k: _x(v) for k, v in dict(item.get("env", {})).items()},
                url=_x(item.get("url", "")),
                headers={k: _x(v) for k, v in dict(item.get("headers", {})).items()},
                trust_level=item.get("trust_level", "safe"),
            )
        except Exception as exc:
            logger.warning("mcp_client: invalid server config %r — %s", item, exc)
            continue

        if missing:
            logger.warning(
                "mcp_client: server %r references unset variable(s) %s "
                "with no default — skipping",
                name, ", ".join(sorted(set(missing))),
            )
        elif transport not in _TRANSPORTS:
            logger.warning(
                "mcp_client: server %r has %s — skipping", name,
                f"unknown type {transport!r}" if transport else "neither command nor url",
            )
        elif transport == "stdio" and not cfg.command:
            logger.warning("mcp_client: stdio server %r has no command — skipping", name)
        elif transport != "stdio" and not cfg.url:
            logger.warning("mcp_client: %s server %r has no url — skipping", transport, name)
        else:
            result.append(cfg)
    return result


# ---------------------------------------------------------------------------
# LoomMCPClient
# ---------------------------------------------------------------------------

class LoomMCPClient:
    """
    Connects to one external MCP server and imports its tools as Loom
    ``ToolDefinition`` objects.

    Each remote tool becomes an async Loom tool that:
    1. Opens (or reuses) a connection to the MCP server
    2. Calls the remote tool via MCP ``tools/call``
    3. Returns the text result as a ``ToolResult``

    One client instance corresponds to one external MCP server.  The
    connection (a subprocess for stdio, an HTTP session for http/sse) is
    opened lazily on first use and closed on ``disconnect()``.
    """

    def __init__(self, cfg: MCPServerConfig) -> None:
        _check_mcp()
        self._cfg = cfg
        self._session: "ClientSession | None" = None
        self._cm: Any = None   # AsyncExitStack owning transport + session
        self._read: Any = None
        self._write: Any = None
        self._lock = asyncio.Lock()
        # Server usage guide from ``initialize`` (Issue #595); None if absent.
        self.instructions: str | None = None

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

            # Prefix tool names to avoid collision: "filesystem__list_files".
            # Use "__" rather than ":" so the name matches ^[a-zA-Z0-9_-]+$,
            # which strict providers (DeepSeek, OpenAI) enforce on tool
            # names. Same convention Claude Code uses for MCP tools.
            prefixed_name = f"{self._cfg.name}__{tool_name}"
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
        """Close the connection to the MCP server (session, then transport).

        Suppresses all exceptions during __aexit__ so that:
        - transport async-generator GC finalizer errors (which can fire
          in unrelated async contexts) do not propagate
        - Session shutdown is never derailed by a failing MCP cleanup
        See: "an error occurred during closing of async generator stdio_client"

        The SDK's stdio-reader output is demoted for the same window — see
        ``_quiet_stdio_reader`` — because a server that buffered a stdout
        banner flushes it exactly here, and the resulting parse traceback is
        noise from a session that is already finished with the server.
        """
        if self._cm is not None:
            cm, self._cm = self._cm, None
            try:
                with _quiet_stdio_reader():
                    await cm.__aexit__(None, None, None)
            except BaseException:
                # Catch everything: Exception + GeneratorExit + CancelledError.
                # The anyio task group inside stdio_client may attempt cleanup
                # in a stale event-loop context; swallow the error silently.
                pass
            self._session = None
            self._read = None
            self._write = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _open_transport(self, stack: contextlib.AsyncExitStack) -> tuple:
        """Enter the configured transport on *stack*; return ``(read, write)``."""
        cfg = self._cfg
        if cfg.type == "http":
            http_client = await stack.enter_async_context(
                create_mcp_http_client(headers=cfg.headers or None)
            )
            read, write, _get_session_id = await stack.enter_async_context(
                streamable_http_client(cfg.url, http_client=http_client)
            )
            return read, write
        if cfg.type == "sse":
            return await stack.enter_async_context(
                sse_client(cfg.url, headers=cfg.headers or None)
            )

        # Merge override env on top of the full parent environment so the
        # subprocess retains PATH and other inherited vars.  Without this,
        # passing a non-None env dict to StdioServerParameters replaces the
        # entire subprocess environment and breaks PATH lookup (e.g. uvx).
        merged_env = {**os.environ, **cfg.env} if cfg.env else None
        params = StdioServerParameters(
            command=cfg.command,
            args=cfg.args,
            env=merged_env,
        )
        return await stack.enter_async_context(stdio_client(params))

    async def _ensure_connected(self) -> None:
        async with self._lock:
            if self._session is not None:
                return

            stack = contextlib.AsyncExitStack()
            try:
                read, write = await self._open_transport(stack)
                session = await stack.enter_async_context(ClientSession(read, write))
                init = await session.initialize()
            except BaseException:
                # Close the transport immediately so it is not orphaned.  An
                # un-exited anyio task group inside it would later crash when
                # Python's async-generator GC finalises it in a different
                # task.
                #
                # A stdio server that printed a stdout banner flushes it here
                # too (the subprocess dies on this close), so this path needs
                # the same quieting as disconnect() — otherwise a failed
                # handshake buries its real cause under a parse traceback.
                try:
                    with _quiet_stdio_reader():
                        await stack.aclose()
                except Exception:
                    pass
                raise
            self._cm = stack
            self._read, self._write = read, write
            self._session = session
            self.instructions = getattr(init, "instructions", None)

    async def _call_tool(self, name: str, arguments: dict) -> "CallToolResult":
        await self._ensure_connected()
        assert self._session is not None
        return await self._session.call_tool(name, arguments)


def render_mcp_instructions(
    clients: list[Any],
    max_chars: int = MCP_INSTRUCTIONS_MAX_CHARS,
) -> str:
    """Render connected servers' ``instructions`` as system-prompt sections.

    One ``## MCP server: <name>`` section per server that sent non-blank
    instructions, in connection order; each body is capped at *max_chars*.
    Returns ``""`` when there is nothing to add.
    """
    sections: list[str] = []
    for client in clients:
        text = (getattr(client, "instructions", None) or "").strip()
        if not text:
            continue
        if len(text) > max_chars:
            text = (
                text[:max_chars].rstrip()
                + f"\n\n[… truncated at {max_chars} chars]"
            )
        sections.append(f"## MCP server: {client._cfg.name}\n{text}")
    return "\n\n".join(sections)


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
    ``${VAR}`` placeholders in loom.toml are resolved against the .env
    file even when those variables are not in ``os.environ``.

    Returns the list of ``LoomMCPClient`` instances so the session can
    call ``disconnect()`` on shutdown.
    """
    server_configs = load_mcp_server_configs(config, extra_env)
    if not server_configs:
        return []

    clients: list[LoomMCPClient] = []
    for cfg in server_configs:
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
            # Ensure any partially-opened transport is closed so its
            # anyio task group doesn't leak and crash later.
            await client.disconnect()
    return clients
