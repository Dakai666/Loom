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
import weakref
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

try:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.types import CallToolResult
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False

# Remote transports live in their own block: an environment installed under
# the old ``mcp>=1.0.0`` floor may lack them, and that must only disable
# http/sse servers — never the stdio servers that already worked there.
try:
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    _MCP_HTTP_AVAILABLE = True
except ImportError:
    _MCP_HTTP_AVAILABLE = False


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


def _check_mcp(transport: str = "stdio") -> None:
    if not _MCP_AVAILABLE:
        raise ImportError(
            "MCP SDK not installed. Run: pip install 'loom[mcp]'"
        )
    if transport != "stdio" and not _MCP_HTTP_AVAILABLE:
        raise ImportError(
            f"MCP SDK too old for {transport} transport (needs mcp>=1.24.0). "
            "Run: pip install -U 'loom[mcp]'"
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

    The transport stack is entered and exited inside a task the client owns
    (``_own_connection``).  The SDK transports and ``ClientSession`` hold
    anyio cancel scopes, which must be exited by the task that entered them,
    in LIFO order; entering them in the caller's task meant that closing two
    clients in connection order leaked a cancellation into the caller —
    which, during circadian nightly close, was the autonomy evaluator.
    """

    def __init__(self, cfg: MCPServerConfig) -> None:
        _check_mcp(cfg.type)
        self._cfg = cfg
        self._session: "ClientSession | None" = None
        self._cm: Any = None   # AsyncExitStack owning transport + session
        self._read: Any = None
        self._write: Any = None
        self._lock = asyncio.Lock()
        # The task that entered ``_cm`` and will exit it, and the event that
        # tells it to.  Both None while disconnected.
        self._owner: "asyncio.Task[None] | None" = None
        self._close_requested: asyncio.Event | None = None
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

        The close itself runs in the owner task, so it is safe from any task
        and in any order relative to other clients.  If the caller is
        cancelled while waiting, the owner still finishes closing on its own.

        Serialised with ``_ensure_connected`` on ``_lock``: a disconnect that
        lands mid-handshake waits for that connect to finish, then closes —
        rather than closing under a caller about to use the session.
        """
        async with self._lock:
            owner, self._owner = self._owner, None
            close_requested, self._close_requested = self._close_requested, None
            if owner is None or close_requested is None:
                return
            close_requested.set()
            # ``wait`` rather than ``await owner``: the owner's own outcome
            # (it may have been cancelled at loop shutdown) is not the
            # caller's business; only the caller's own cancellation propagates.
            await asyncio.wait({owner})

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

            ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            close_requested = asyncio.Event()
            owner = asyncio.create_task(
                self._own_connection(ready, close_requested),
                name=f"mcp-owner:{self._cfg.name}",
            )
            self._owner, self._close_requested = owner, close_requested
            try:
                await ready
            except BaseException as exc:
                self._owner = self._close_requested = None
                if isinstance(exc, asyncio.CancelledError):
                    # Caller gave up mid-handshake: the owner is still
                    # connecting and must be told to stop and clean up.
                    owner.cancel()
                raise

    async def _own_connection(
        self, ready: "asyncio.Future[None]", close_requested: asyncio.Event
    ) -> None:
        """Owner task: enter the transport stack, hold it, exit it — all here.

        Reports the handshake outcome through *ready*, then parks until
        ``disconnect()`` sets *close_requested* (or the task is cancelled at
        loop shutdown) and closes the stack in the same task that opened it.
        """
        stack = contextlib.AsyncExitStack()
        try:
            read, write = await self._open_transport(stack)
            session = await stack.enter_async_context(ClientSession(read, write))
            init = await session.initialize()
        except BaseException as exc:
            # Close the transport immediately so it is not orphaned.  An
            # un-exited anyio task group inside it would later crash when
            # Python's async-generator GC finalises it in a different task.
            await self._close_quietly(stack)
            if not ready.done():
                if isinstance(exc, asyncio.CancelledError):
                    ready.cancel()
                else:
                    ready.set_exception(exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return

        self._cm = stack
        self._read, self._write = read, write
        self._session = session
        self.instructions = getattr(init, "instructions", None)
        ready.set_result(None)
        try:
            await close_requested.wait()
        finally:
            self._cm = None
            self._session = None
            self._read = None
            self._write = None
            await self._close_quietly(stack)

    @staticmethod
    async def _close_quietly(stack: contextlib.AsyncExitStack) -> None:
        """Exit *stack*, swallowing every error — the connection is finished.

        A stdio server that printed a stdout banner flushes it on this close
        (the subprocess dies here), so the SDK's stdio-reader output is
        demoted for the window — otherwise a shutdown, or a failed
        handshake's real cause, is buried under a parse traceback.
        """
        try:
            with _quiet_stdio_reader():
                await stack.aclose()
        except BaseException:
            # Catch everything: Exception + GeneratorExit + CancelledError.
            # This is the owner's last act; nothing above it needs the error.
            pass

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

# ---------------------------------------------------------------------------
# Process-wide client sharing (Issue #601)
# ---------------------------------------------------------------------------
#
# Every session used to open its own servers, and Discord thread sessions are
# never evicted — so a long-running bot held one set of server subprocesses
# per thread it had ever touched.  Sessions now borrow one client per server
# config; the connection closes when its last borrower releases it, so a
# single CLI session behaves exactly as before.
#
# One pool per event loop: a client's owner task belongs to the loop that
# started it, and a lock or task must never cross loops.  Sharing is therefore
# per loop, not per process — in production (one Discord/CLI loop per
# process) the two coincide.


class _ClientPool:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # config key → (client, borrow count)
        self._entries: dict[str, tuple[LoomMCPClient, int]] = {}

    @staticmethod
    def _key(cfg: MCPServerConfig) -> str:
        # The full config, so an entry edited in loom.toml gets a new client.
        return json.dumps(asdict(cfg), sort_keys=True)

    async def acquire(self, cfg: MCPServerConfig) -> LoomMCPClient:
        async with self._lock:
            key = self._key(cfg)
            client, count = self._entries.get(key, (None, 0))
            if client is None:
                client = LoomMCPClient(cfg)
            self._entries[key] = (client, count + 1)
            return client

    async def release(self, client: Any) -> bool:
        """Return one borrow of *client*; True if it was the last one."""
        async with self._lock:
            for key, (pooled, count) in self._entries.items():
                if pooled is client:
                    if count > 1:
                        self._entries[key] = (pooled, count - 1)
                        return False
                    del self._entries[key]
                    return True
            return False


_pools: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _ClientPool]" = (
    weakref.WeakKeyDictionary()
)


def _pool() -> _ClientPool:
    loop = asyncio.get_running_loop()
    pool = _pools.get(loop)
    if pool is None:
        pool = _pools[loop] = _ClientPool()
    return pool


async def release_mcp_client(client: Any) -> None:
    """Give back one borrow of a client from ``load_mcp_servers_into_session``.

    Disconnects when the last borrower releases it.  Releasing more times than
    acquired, or a client the pool never handed out, is a no-op — teardown
    must never fail on bookkeeping.

    The entry leaves the pool before the disconnect runs, so a session that
    acquires the same config meanwhile gets a fresh client: for the seconds
    the old connection takes to close, two may coexist.  Deliberate — before
    #601 every session held its own connection anyway, and making acquire
    wait on an in-flight close would add cross-await state for no gain.
    """
    if await _pool().release(client):
        await client.disconnect()


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

    Clients are shared by every session in the event loop (Issue #601):
    a server already connected for another session is reused, not spawned
    again.  Returns the borrowed clients; the session must hand each back
    with ``release_mcp_client()`` on shutdown.
    """
    server_configs = load_mcp_server_configs(config, extra_env)
    if not server_configs:
        return []

    pool = _pool()
    clients: list[LoomMCPClient] = []
    for cfg in server_configs:
        client = await pool.acquire(cfg)
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
            # Hand the borrow back; if nobody else holds this client, that
            # also closes any partially-opened transport.
            await release_mcp_client(client)
    return clients
