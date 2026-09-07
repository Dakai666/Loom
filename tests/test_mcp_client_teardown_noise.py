"""MCP teardown noise suppression.

Some third-party stdio MCP servers ``print()`` a banner to stdout instead of
stderr (``minimax-mcp`` / ``minimax-coding-plan-mcp`` both do:
``print("Starting Minimax MCP server")`` in ``main()``).  Because a piped
stdout is block-buffered, that line is flushed only when the subprocess
exits — i.e. exactly when Loom tears the client down — and the MCP SDK's
``stdout_reader`` then emits a full ``logger.exception`` traceback for a line
it cannot parse as JSON-RPC.

The traceback is pure noise on the way out (the session is already done with
the server), but it lands right after the "Compressing session to memory…"
rule and reads like the memory pipeline broke.  ``disconnect()`` therefore
silences the SDK's stdio logger for the duration of its own teardown only.
"""

from __future__ import annotations

import logging

from loom.extensibility.mcp_client import (
    MCPServerConfig,
    LoomMCPClient,
    _STDIO_LOGGER_NAME,
)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _client() -> LoomMCPClient:
    return LoomMCPClient(MCPServerConfig(name="noisy", command="true"))


def _attach(capture: _Capture) -> logging.Logger:
    log = logging.getLogger(_STDIO_LOGGER_NAME)
    log.addHandler(capture)
    log.setLevel(logging.DEBUG)
    return log


class TestDisconnectSuppressesStdioNoise:
    async def test_parse_traceback_during_teardown_is_swallowed(self) -> None:
        """The banner flushed at subprocess exit must not reach a handler."""
        capture = _Capture()
        log = _attach(capture)
        try:
            client = _client()

            class _CM:
                async def __aexit__(self, *_exc):
                    # What the MCP SDK's stdout_reader does when the child
                    # flushes its non-JSON banner on the way out.
                    log.exception("Failed to parse JSONRPC message from server")

            client._cm = _CM()
            await client.disconnect()

            assert capture.records == [], (
                "stdio parse noise during teardown leaked to a handler"
            )
        finally:
            log.removeHandler(capture)

    async def test_logger_is_live_again_after_disconnect(self) -> None:
        """Suppression is scoped to the teardown, not installed permanently."""
        capture = _Capture()
        log = _attach(capture)
        try:
            client = _client()

            class _CM:
                async def __aexit__(self, *_exc):
                    return None

            client._cm = _CM()
            await client.disconnect()

            log.error("a genuine error after teardown")
            assert len(capture.records) == 1
        finally:
            log.removeHandler(capture)

    async def test_suppression_lifted_even_when_aexit_raises(self) -> None:
        """``__aexit__`` blowing up must not leave the logger muted forever."""
        capture = _Capture()
        log = _attach(capture)
        try:
            client = _client()

            class _CM:
                async def __aexit__(self, *_exc):
                    raise RuntimeError("anyio task group cleanup exploded")

            client._cm = _CM()
            await client.disconnect()   # swallows, by contract

            log.error("a genuine error after a failed teardown")
            assert len(capture.records) == 1
        finally:
            log.removeHandler(capture)

    async def test_other_loggers_are_untouched(self) -> None:
        """Only the SDK's stdio logger is muted, not logging at large."""
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
        finally:
            other.removeHandler(capture)


class TestDisconnectStillTearsDown:
    async def test_state_is_cleared(self) -> None:
        """Suppression must not change what disconnect() actually does."""
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

    async def test_disconnect_without_connection_is_a_noop(self) -> None:
        client = _client()
        await client.disconnect()
        assert client._cm is None
