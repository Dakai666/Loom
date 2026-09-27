"""MCP client connection ownership — disconnect must never cancel its caller.

anyio cancel scopes (inside every SDK transport and ``ClientSession``) must be
exited by the task that entered them, in LIFO order.  When a session opens
two stdio servers in one task and ``session.stop()`` later closes them in
connection order (FIFO), the first client's scopes are exited while the
second's are still on top of that task's scope stack.  anyio then delivers a
cancellation to the *calling* task: ``disconnect()`` swallows it, but the
next ``await`` in the caller raises ``CancelledError``.

In production that caller was the autonomy evaluator (circadian dawn opens
the daily session, nightly close stops it — same task), so the leaked cancel
tore through ``session.stop()`` and silently killed the cron loop.

Contract: each client enters and exits its whole transport stack inside a
task it owns, so no caller — whatever task it runs in, whatever order it
closes clients in — ever shares a scope stack with a client.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import anyio
import pytest

import loom.extensibility.mcp_client as mcp_client_mod
from loom.extensibility.mcp_client import LoomMCPClient, MCPServerConfig


@contextlib.asynccontextmanager
async def _scoped():
    """Shape of an SDK transport: a live anyio task group held open."""
    async with anyio.create_task_group() as tg:
        tg.start_soon(anyio.sleep_forever)
        try:
            yield
        finally:
            tg.cancel_scope.cancel()


class _ScopedSession:
    """``ClientSession`` stand-in that, like the real one, owns a task group."""

    def __init__(self, read, write) -> None:
        self._cm = _scoped()

    async def __aenter__(self):
        await self._cm.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._cm.__aexit__(*exc)

    async def initialize(self):
        return SimpleNamespace(instructions=None)


@pytest.fixture
def scoped_transports(monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_stdio(_params):
        async with _scoped():
            yield "r", "w"

    monkeypatch.setattr(mcp_client_mod, "stdio_client", fake_stdio)
    monkeypatch.setattr(mcp_client_mod, "ClientSession", _ScopedSession)


def _client(name: str) -> LoomMCPClient:
    return LoomMCPClient(MCPServerConfig(name=name, command="true"))


async def _run_in_task(coro):
    """Run *coro* in a fresh task; report how that task ended."""
    task = asyncio.create_task(coro)
    try:
        return await task
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            raise
        return "caller-task-cancelled"


class TestDisconnectNeverCancelsCaller:
    async def test_fifo_close_in_connecting_task(self, scoped_transports) -> None:
        """The production shape: connect both, close in connection order."""
        async def lifecycle():
            clients = [_client("minimax"), _client("minimax_coding")]
            for c in clients:
                await c._ensure_connected()
            for c in clients:
                await c.disconnect()
            await asyncio.sleep(0)   # stands in for session.stop()'s db close
            return "survived", asyncio.current_task().cancelling()

        assert await _run_in_task(lifecycle()) == ("survived", 0)

    async def test_lifo_close_in_connecting_task(self, scoped_transports) -> None:
        async def lifecycle():
            clients = [_client("a"), _client("b")]
            for c in clients:
                await c._ensure_connected()
            for c in reversed(clients):
                await c.disconnect()
            await asyncio.sleep(0)
            return "survived"

        assert await _run_in_task(lifecycle()) == "survived"

    async def test_close_from_another_task(self, scoped_transports) -> None:
        clients = [_client("a"), _client("b")]

        async def connect_all():
            for c in clients:
                await c._ensure_connected()

        async def close_all():
            for c in clients:
                await c.disconnect()
            await asyncio.sleep(0)
            return "survived"

        await _run_in_task(connect_all())
        assert await _run_in_task(close_all()) == "survived"


class TestOwnerLifecycle:
    async def test_disconnect_clears_state_and_allows_reconnect(
        self, scoped_transports
    ) -> None:
        client = _client("a")
        await client._ensure_connected()
        assert client._session is not None

        await client.disconnect()
        assert client._session is None
        assert client._cm is None

        await client._ensure_connected()
        assert client._session is not None
        await client.disconnect()

    async def test_no_owner_task_left_behind(self, scoped_transports) -> None:
        before = asyncio.all_tasks()
        client = _client("a")
        await client._ensure_connected()
        await client.disconnect()
        leftover = [t for t in asyncio.all_tasks() - before if not t.done()]
        assert leftover == []

    async def test_handshake_failure_propagates_without_owner_leak(
        self, scoped_transports, monkeypatch
    ) -> None:
        class _Failing(_ScopedSession):
            async def initialize(self):
                raise RuntimeError("handshake failed")

        monkeypatch.setattr(mcp_client_mod, "ClientSession", _Failing)
        before = asyncio.all_tasks()
        client = _client("a")

        with pytest.raises(RuntimeError, match="handshake failed"):
            await client._ensure_connected()

        await asyncio.sleep(0)
        assert client._session is None
        assert [t for t in asyncio.all_tasks() - before if not t.done()] == []

    async def test_caller_cancelled_mid_connect_tears_owner_down(
        self, scoped_transports, monkeypatch
    ) -> None:
        started = asyncio.Event()

        class _Slow(_ScopedSession):
            async def initialize(self):
                started.set()
                await anyio.sleep_forever()

        monkeypatch.setattr(mcp_client_mod, "ClientSession", _Slow)
        before = asyncio.all_tasks()
        client = _client("a")

        caller = asyncio.create_task(client._ensure_connected())
        await started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        for _ in range(5):
            await asyncio.sleep(0)
        assert client._session is None
        assert [t for t in asyncio.all_tasks() - before if not t.done()] == []
