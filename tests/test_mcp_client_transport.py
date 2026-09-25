"""MCP client parity with Claude Code's ``.mcp.json`` (Issue #595).

Contract
--------
* ``[[mcp.servers]]`` takes the same fields as a Claude Code ``mcpServers``
  entry — ``type`` / ``command`` / ``args`` / ``env`` / ``url`` / ``headers``
  — plus Loom's own ``trust_level``.  Without ``type``, ``command`` means
  stdio and ``url`` means http, so pre-#595 configs load unchanged.
* ``${VAR}`` and ``${VAR:-default}`` expand in every string field, looked up
  in ``.env`` first and ``os.environ`` second.  A variable that is unset and
  has no default skips that server — the same outcome as Claude Code, which
  refuses such a config rather than sending an empty token.
* ``http`` connects over Streamable HTTP and ``sse`` over SSE, carrying the
  configured headers on every request.
* ``InitializeResult.instructions`` is kept per client and rendered into the
  system prompt as one ``## MCP server: <name>`` section per server, capped
  so a single server cannot flood the context.
"""

from __future__ import annotations

import contextlib
import logging
from types import SimpleNamespace

import pytest

import loom.extensibility.mcp_client as mcp_client_mod
from loom.extensibility.mcp_client import (
    LoomMCPClient,
    MCPServerConfig,
    load_mcp_server_configs,
    render_mcp_instructions,
)


def _load(*servers: dict, extra_env: dict | None = None) -> list[MCPServerConfig]:
    return load_mcp_server_configs({"mcp": {"servers": list(servers)}}, extra_env)


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

class TestTransportInference:
    def test_command_without_type_is_stdio(self) -> None:
        [cfg] = _load({"name": "fs", "command": "npx", "args": ["-y", "srv"]})
        assert cfg.type == "stdio"
        assert cfg.command == "npx"
        assert cfg.args == ["-y", "srv"]
        assert cfg.trust_level == "safe"

    def test_url_without_type_is_http(self) -> None:
        [cfg] = _load({"name": "substrate", "url": "http://127.0.0.1:7077/mcp"})
        assert cfg.type == "http"
        assert cfg.url == "http://127.0.0.1:7077/mcp"

    def test_explicit_sse(self) -> None:
        [cfg] = _load({"name": "s", "type": "sse", "url": "http://h/sse"})
        assert cfg.type == "sse"

    def test_neither_command_nor_url_is_skipped(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger=mcp_client_mod.__name__):
            assert _load({"name": "empty"}) == []
        assert "empty" in caplog.text

    def test_unknown_type_is_skipped(self) -> None:
        assert _load({"name": "x", "type": "websocket", "url": "ws://h"}) == []

    def test_http_without_url_is_skipped(self) -> None:
        assert _load({"name": "x", "type": "http", "command": "npx"}) == []

    def test_stdio_without_command_is_skipped(self) -> None:
        assert _load({"name": "x", "type": "stdio", "url": "http://h"}) == []

    def test_one_bad_entry_does_not_drop_the_others(self) -> None:
        cfgs = _load({"name": "bad"}, {"name": "good", "command": "true"})
        assert [c.name for c in cfgs] == ["good"]


class TestEnvExpansion:
    def test_every_string_field_expands(self, monkeypatch) -> None:
        monkeypatch.setenv("MCP_T_BIN", "uvx")
        [stdio] = _load({
            "name": "s",
            "command": "${MCP_T_BIN}",
            "args": ["--repo", "${REPO}"],
            "env": {"TOKEN": "${TOKEN}"},
        }, extra_env={"REPO": "/r", "TOKEN": "t0k"})
        assert stdio.command == "uvx"
        assert stdio.args == ["--repo", "/r"]
        assert stdio.env == {"TOKEN": "t0k"}

        [http] = _load({
            "name": "h",
            "url": "http://${HOST}/mcp",
            "headers": {"Authorization": "${AUTH}"},
        }, extra_env={"HOST": "127.0.0.1:7077", "AUTH": "Bearer abc"})
        assert http.url == "http://127.0.0.1:7077/mcp"
        assert http.headers == {"Authorization": "Bearer abc"}

    def test_dotenv_wins_over_process_env(self, monkeypatch) -> None:
        monkeypatch.setenv("MCP_T_AUTH", "from-os")
        [cfg] = _load(
            {"name": "h", "url": "http://h", "headers": {"A": "${MCP_T_AUTH}"}},
            extra_env={"MCP_T_AUTH": "from-dotenv"},
        )
        assert cfg.headers == {"A": "from-dotenv"}

    def test_default_applies_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("MCP_T_PORT", raising=False)
        [cfg] = _load({"name": "h", "url": "http://h:${MCP_T_PORT:-7077}/mcp"})
        assert cfg.url == "http://h:7077/mcp"

    def test_default_ignored_when_set(self) -> None:
        [cfg] = _load(
            {"name": "h", "url": "http://h:${PORT:-7077}/mcp"},
            extra_env={"PORT": "9000"},
        )
        assert cfg.url == "http://h:9000/mcp"

    def test_empty_default_is_allowed(self, monkeypatch) -> None:
        monkeypatch.delenv("MCP_T_OPT", raising=False)
        [cfg] = _load({"name": "s", "command": "x", "args": ["${MCP_T_OPT:-}"]})
        assert cfg.args == [""]

    def test_unset_without_default_skips_server(self, monkeypatch, caplog) -> None:
        monkeypatch.delenv("MCP_T_MISSING", raising=False)
        with caplog.at_level(logging.WARNING, logger=mcp_client_mod.__name__):
            cfgs = _load(
                {"name": "h", "url": "http://h",
                 "headers": {"Authorization": "${MCP_T_MISSING}"}},
                {"name": "ok", "command": "true"},
            )
        assert [c.name for c in cfgs] == ["ok"]
        assert "MCP_T_MISSING" in caplog.text


# ---------------------------------------------------------------------------
# Transport selection + instructions capture
# ---------------------------------------------------------------------------

class _FakeClientSession:
    """Stands in for ``mcp.ClientSession``; records the streams it was given."""

    instances: list["_FakeClientSession"] = []
    instructions: str | None = "use me wisely"

    def __init__(self, read, write) -> None:
        self.read, self.write = read, write
        _FakeClientSession.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def initialize(self):
        return SimpleNamespace(instructions=type(self).instructions)


@pytest.fixture
def transports(monkeypatch):
    """Replace every SDK transport with a recorder; returns the call log."""
    calls: list[tuple] = []
    _FakeClientSession.instances = []
    _FakeClientSession.instructions = "use me wisely"

    @contextlib.asynccontextmanager
    async def fake_stdio(params):
        calls.append(("stdio", params))
        yield "r-stdio", "w-stdio"

    @contextlib.asynccontextmanager
    async def fake_http(url, *, http_client=None, **_kw):
        calls.append(("http", url, http_client))
        yield "r-http", "w-http", lambda: None

    @contextlib.asynccontextmanager
    async def fake_sse(url, headers=None, **_kw):
        calls.append(("sse", url, headers))
        yield "r-sse", "w-sse"

    class _FakeHttpx:
        def __init__(self, headers):
            self.headers = headers

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(mcp_client_mod, "stdio_client", fake_stdio)
    monkeypatch.setattr(mcp_client_mod, "streamable_http_client", fake_http)
    monkeypatch.setattr(mcp_client_mod, "sse_client", fake_sse)
    monkeypatch.setattr(
        mcp_client_mod, "create_mcp_http_client",
        lambda headers=None, **_kw: _FakeHttpx(headers),
    )
    monkeypatch.setattr(mcp_client_mod, "ClientSession", _FakeClientSession)
    return calls


class TestTransportSelection:
    async def test_http_uses_streamable_http_with_headers(self, transports) -> None:
        client = LoomMCPClient(MCPServerConfig(
            name="substrate", type="http", url="http://127.0.0.1:7077/mcp",
            headers={"Authorization": "Bearer abc"},
        ))
        await client._ensure_connected()

        [(kind, url, http_client)] = transports
        assert kind == "http"
        assert url == "http://127.0.0.1:7077/mcp"
        assert http_client.headers == {"Authorization": "Bearer abc"}
        assert _FakeClientSession.instances[0].read == "r-http"
        await client.disconnect()

    async def test_sse_passes_headers(self, transports) -> None:
        client = LoomMCPClient(MCPServerConfig(
            name="s", type="sse", url="http://h/sse", headers={"X-K": "v"},
        ))
        await client._ensure_connected()
        assert transports == [("sse", "http://h/sse", {"X-K": "v"})]
        await client.disconnect()

    async def test_stdio_unchanged(self, transports, monkeypatch) -> None:
        monkeypatch.setenv("MCP_T_INHERITED", "1")
        client = LoomMCPClient(MCPServerConfig(
            name="fs", command="npx", args=["-y", "srv"], env={"K": "v"},
        ))
        await client._ensure_connected()
        [(kind, params)] = transports
        assert kind == "stdio"
        assert params.command == "npx"
        assert params.args == ["-y", "srv"]
        # Override env is merged over the parent env, not a replacement.
        assert params.env["K"] == "v"
        assert params.env["MCP_T_INHERITED"] == "1"
        await client.disconnect()

    async def test_instructions_captured_on_connect(self, transports) -> None:
        client = LoomMCPClient(MCPServerConfig(name="s", type="http", url="http://h"))
        assert client.instructions is None
        await client._ensure_connected()
        assert client.instructions == "use me wisely"
        await client.disconnect()

    async def test_disconnect_allows_reconnect(self, transports) -> None:
        client = LoomMCPClient(MCPServerConfig(name="s", type="http", url="http://h"))
        await client._ensure_connected()
        await client.disconnect()
        await client._ensure_connected()
        assert len(transports) == 2
        await client.disconnect()


class TestOldSdk:
    """An env installed under the old ``mcp>=1.0.0`` floor may lack the
    remote transports; that must not take the stdio servers down with it."""

    def test_stdio_still_constructs(self, monkeypatch) -> None:
        monkeypatch.setattr(mcp_client_mod, "_MCP_HTTP_AVAILABLE", False)
        LoomMCPClient(MCPServerConfig(name="fs", command="npx"))

    @pytest.mark.parametrize("transport", ["http", "sse"])
    def test_remote_transport_names_the_upgrade(self, monkeypatch, transport) -> None:
        monkeypatch.setattr(mcp_client_mod, "_MCP_HTTP_AVAILABLE", False)
        with pytest.raises(ImportError, match=r"mcp>=1\.24\.0"):
            LoomMCPClient(MCPServerConfig(name="r", type=transport, url="http://h"))


# ---------------------------------------------------------------------------
# System prompt rendering
# ---------------------------------------------------------------------------

def _named(name: str, instructions: str | None) -> SimpleNamespace:
    return SimpleNamespace(_cfg=SimpleNamespace(name=name), instructions=instructions)


class TestRenderInstructions:
    def test_one_section_per_server(self) -> None:
        text = render_mcp_instructions([
            _named("substrate", "call get_context first"),
            _named("fs", "paths are absolute"),
        ])
        assert "## MCP server: substrate\ncall get_context first" in text
        assert "## MCP server: fs\npaths are absolute" in text
        assert text.index("substrate") < text.index("## MCP server: fs")

    def test_servers_without_instructions_are_omitted(self) -> None:
        text = render_mcp_instructions([_named("a", None), _named("b", "  "), _named("c", "hi")])
        assert "## MCP server: a" not in text
        assert "## MCP server: b" not in text
        assert "## MCP server: c" in text

    def test_nothing_to_render_is_empty(self) -> None:
        assert render_mcp_instructions([]) == ""
        assert render_mcp_instructions([_named("a", None)]) == ""

    def test_clients_without_attribute_are_tolerated(self) -> None:
        assert render_mcp_instructions([SimpleNamespace(_cfg=SimpleNamespace(name="x"))]) == ""

    def test_long_instructions_are_truncated_per_server(self) -> None:
        text = render_mcp_instructions(
            [_named("big", "x" * 500), _named("small", "tiny")], max_chars=100,
        )
        big = text.split("## MCP server: small")[0]
        assert big.count("x") == 100
        assert "truncated" in big
        assert "## MCP server: small\ntiny" in text
