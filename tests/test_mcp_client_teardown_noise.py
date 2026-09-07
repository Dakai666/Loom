"""MCP stdio teardown noise.

Some third-party stdio MCP servers ``print()`` a banner to stdout instead of
stderr (``minimax-mcp`` / ``minimax-coding-plan-mcp`` both do:
``print("Starting Minimax MCP server")`` in ``main()``).  Because a piped
stdout is block-buffered, that line is flushed only when the subprocess
exits — i.e. exactly when Loom closes the ``stdio_client`` — and the MCP
SDK's ``stdout_reader`` then emits a full ``logger.exception`` traceback for
a line it cannot parse as JSON-RPC.

The traceback is noise (the client is on its way out either way), but it
lands right after the "Compressing session to memory…" rule and reads like
the memory pipeline broke.  Both closing paths therefore demote the SDK's
stdio-reader output for the length of their own cleanup:

* ``disconnect()`` — normal shutdown
* ``_ensure_connected()``'s failed-handshake branch — same ``__aexit__``,
  same banner flush

Demoted, not dropped: ``mcp.client.stdio`` is one process-wide logger shared
by every session's clients, so a hard filter would swallow a concurrent
session's genuine parse errors too.
"""

from __future__ import annotations

import logging

import pytest

import loom.extensibility.mcp_client as mcp_client_mod
from loom.extensibility.mcp_client import (
    MCPServerConfig,
    LoomMCPClient,
    _STDIO_LOGGER_NAME,
)


class _Capture(logging.Handler):
    def __init__(self, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _client() -> LoomMCPClient:
    return LoomMCPClient(MCPServerConfig(name="noisy", command="true"))


@pytest.fixture
def console() -> logging.Handler:
    """A handler standing in for a normal console sink (WARNING and up)."""
    capture = _Capture(logging.WARNING)
    log = logging.getLogger(_STDIO_LOGGER_NAME)
    log.addHandler(capture)
    log.setLevel(logging.DEBUG)
    yield capture
    log.removeHandler(capture)


def _banner_traceback() -> None:
    """What the SDK's stdout_reader does with the flushed banner line."""
    logging.getLogger(_STDIO_LOGGER_NAME).exception(
        "Failed to parse JSONRPC message from server"
    )


class TestDisconnect:
    async def test_banner_traceback_stays_off_the_console(self, console) -> None:
        client = _client()

        class _CM:
            async def __aexit__(self, *_exc):
                _banner_traceback()

        client._cm = _CM()
        await client.disconnect()

        assert console.records == [], (
            "stdio parse noise during teardown reached the console"
        )

    async def test_record_is_demoted_not_discarded(self) -> None:
        """A shared logger must not lose records — only their severity."""
        debug_sink = _Capture(logging.DEBUG)
        log = logging.getLogger(_STDIO_LOGGER_NAME)
        log.addHandler(debug_sink)
        log.setLevel(logging.DEBUG)
        try:
            client = _client()

            class _CM:
                async def __aexit__(self, *_exc):
                    _banner_traceback()

            client._cm = _CM()
            await client.disconnect()

            assert len(debug_sink.records) == 1
            assert debug_sink.records[0].levelno == logging.DEBUG
            assert debug_sink.records[0].levelname == "DEBUG"
        finally:
            log.removeHandler(debug_sink)

    async def test_logger_is_live_again_afterwards(self, console) -> None:
        """Quieting is scoped to the cleanup, not installed permanently."""
        client = _client()

        class _CM:
            async def __aexit__(self, *_exc):
                return None

        client._cm = _CM()
        await client.disconnect()

        logging.getLogger(_STDIO_LOGGER_NAME).error("a genuine error")
        assert len(console.records) == 1

    async def test_scope_lifted_even_when_aexit_raises(self, console) -> None:
        client = _client()

        class _CM:
            async def __aexit__(self, *_exc):
                raise RuntimeError("anyio task group cleanup exploded")

        client._cm = _CM()
        await client.disconnect()   # swallows, by contract

        logging.getLogger(_STDIO_LOGGER_NAME).error("a genuine error")
        assert len(console.records) == 1

    async def test_other_loggers_are_untouched(self) -> None:
        capture = _Capture()
        other = logging.getLogger("loom.test.unrelated")
        other.addHandler(capture)
        other.setLevel(logging.DEBUG)
        try:
            client = _client()

            class _CM:
                async def __aexit__(self, *_exc):
                    other.error("unrelated subsystem still talking")

            client._cm = _CM()
            await client.disconnect()

            assert len(capture.records) == 1
            assert capture.records[0].levelno == logging.ERROR
        finally:
            other.removeHandler(capture)

    async def test_teardown_still_clears_state(self) -> None:
        """Quieting must not change what disconnect() actually does."""
        client = _client()
        exited: list[bool] = []

        class _CM:
            async def __aexit__(self, *_exc):
                exited.append(True)

        client._cm = _CM()
        client._session = object()
        client._read = object()
        client._write = object()

        await client.disconnect()

        assert exited == [True]
        assert client._cm is None
        assert client._session is None
        assert client._read is None
        assert client._write is None

    async def test_without_connection_is_a_noop(self) -> None:
        client = _client()
        await client.disconnect()
        assert client._cm is None


class TestFailedHandshakeCleanup:
    """The connect-failure path closes the same stdio_client, so the banner
    flushes there too — and burying a handshake failure's real cause under a
    parse traceback is worse than the shutdown case."""

    @staticmethod
    def _arrange(monkeypatch, *, on_close) -> None:
        class _CM:
            async def __aenter__(self):
                return ("read", "write")

            async def __aexit__(self, *_exc):
                on_close()

        class _Session:
            def __init__(self, *_a):
                pass

            async def __aenter__(self):
                raise RuntimeError("handshake failed")

        monkeypatch.setattr(mcp_client_mod, "stdio_client", lambda _p: _CM())
        monkeypatch.setattr(mcp_client_mod, "ClientSession", _Session)

    async def test_banner_traceback_stays_off_the_console(
        self, console, monkeypatch
    ) -> None:
        self._arrange(monkeypatch, on_close=_banner_traceback)
        client = _client()

        with pytest.raises(RuntimeError, match="handshake failed"):
            await client._ensure_connected()

        assert console.records == [], (
            "stdio parse noise during failed-handshake cleanup reached the console"
        )

    async def test_logger_is_live_again_afterwards(
        self, console, monkeypatch
    ) -> None:
        self._arrange(monkeypatch, on_close=lambda: None)
        client = _client()

        with pytest.raises(RuntimeError):
            await client._ensure_connected()

        logging.getLogger(_STDIO_LOGGER_NAME).error("a genuine error")
        assert len(console.records) == 1

    async def test_original_failure_still_propagates(self, monkeypatch) -> None:
        """Quieting the noise must not also swallow the real cause."""
        def _explode():
            raise RuntimeError("cleanup also failed")

        self._arrange(monkeypatch, on_close=_explode)
        client = _client()

        with pytest.raises(RuntimeError, match="handshake failed"):
            await client._ensure_connected()

        assert client._cm is None
        assert client._session is None
