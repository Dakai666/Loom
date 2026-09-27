"""Process-wide shared MCP clients (Issue #601).

Every ``LoomSession.start()`` used to open its own MCP servers, and Discord
thread sessions are never evicted — so a long-running bot held one set of
server subprocesses per thread it had ever touched (32 minimax processes for
8 threads, observed).

Contract: sessions in one event loop borrow one client per server config.
``load_mcp_servers_into_session`` acquires, ``release_mcp_client`` (called by
``session.stop()``) returns; the connection closes when its last borrower
releases it.  A single CLI session therefore behaves exactly as before.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

import loom.extensibility.mcp_client as mcp_client_mod
from loom.extensibility.mcp_client import (
    load_mcp_servers_into_session,
    release_mcp_client,
)


class _Registry:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def register(self, tool) -> None:
        self.tools[tool.name] = tool


def _session() -> SimpleNamespace:
    return SimpleNamespace(registry=_Registry())


class _FakeSession:
    fail_initialize = False

    def __init__(self, read, write) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def initialize(self):
        if type(self).fail_initialize:
            raise RuntimeError("server down")
        return SimpleNamespace(instructions=None)

    async def list_tools(self):
        return SimpleNamespace(tools=[
            SimpleNamespace(name="search", description="web search", inputSchema=None),
        ])


@pytest.fixture
def servers(monkeypatch):
    """Fake stdio transport; returns a log of opened / closed servers."""
    log: dict[str, list[str]] = {"opened": [], "closed": []}
    _FakeSession.fail_initialize = False

    @contextlib.asynccontextmanager
    async def fake_stdio(params):
        log["opened"].append(params.command)
        try:
            yield "r", "w"
        finally:
            log["closed"].append(params.command)

    monkeypatch.setattr(mcp_client_mod, "stdio_client", fake_stdio)
    monkeypatch.setattr(mcp_client_mod, "ClientSession", _FakeSession)
    return log


def _config(*names: str) -> dict:
    return {"mcp": {"servers": [{"name": n, "command": n} for n in names]}}


class TestSharing:
    async def test_sessions_share_one_client_per_server(self, servers) -> None:
        a, b = _session(), _session()
        clients_a = await load_mcp_servers_into_session(_config("minimax"), a)
        clients_b = await load_mcp_servers_into_session(_config("minimax"), b)

        assert clients_a[0] is clients_b[0]
        assert servers["opened"] == ["minimax"]
        assert "minimax__search" in a.registry.tools
        assert "minimax__search" in b.registry.tools

        for c in clients_a + clients_b:
            await release_mcp_client(c)

    async def test_closed_only_when_last_borrower_releases(self, servers) -> None:
        [client] = await load_mcp_servers_into_session(_config("minimax"), _session())
        await load_mcp_servers_into_session(_config("minimax"), _session())

        await release_mcp_client(client)
        assert servers["closed"] == []
        assert client._session is not None

        await release_mcp_client(client)
        assert servers["closed"] == ["minimax"]
        assert client._session is None

    async def test_single_session_closes_on_release(self, servers) -> None:
        """CLI shape: one session, stop() closes its servers as before."""
        clients = await load_mcp_servers_into_session(
            _config("minimax", "minimax_coding"), _session()
        )
        for c in clients:
            await release_mcp_client(c)
        assert sorted(servers["closed"]) == ["minimax", "minimax_coding"]

    async def test_reacquire_after_close_opens_fresh(self, servers) -> None:
        [first] = await load_mcp_servers_into_session(_config("minimax"), _session())
        await release_mcp_client(first)
        [second] = await load_mcp_servers_into_session(_config("minimax"), _session())

        assert second is not first
        assert servers["opened"] == ["minimax", "minimax"]
        await release_mcp_client(second)

    async def test_changed_config_gets_its_own_client(self, servers) -> None:
        old = {"mcp": {"servers": [{"name": "m", "command": "m", "args": ["v1"]}]}}
        new = {"mcp": {"servers": [{"name": "m", "command": "m", "args": ["v2"]}]}}
        [a] = await load_mcp_servers_into_session(old, _session())
        [b] = await load_mcp_servers_into_session(new, _session())

        assert a is not b
        assert len(servers["opened"]) == 2
        await release_mcp_client(a)
        await release_mcp_client(b)

    async def test_failed_connect_is_not_cached(self, servers) -> None:
        _FakeSession.fail_initialize = True
        assert await load_mcp_servers_into_session(_config("minimax"), _session()) == []

        _FakeSession.fail_initialize = False
        [client] = await load_mcp_servers_into_session(_config("minimax"), _session())
        assert client._session is not None
        await release_mcp_client(client)

    async def test_release_is_idempotent_per_borrow_and_tolerates_strangers(
        self, servers
    ) -> None:
        [client] = await load_mcp_servers_into_session(_config("minimax"), _session())
        await release_mcp_client(client)
        await release_mcp_client(client)       # extra release: no-op, no error
        assert servers["closed"] == ["minimax"]

        stranger = SimpleNamespace(_cfg=SimpleNamespace(name="x"))
        await release_mcp_client(stranger)     # never pooled: tolerated
