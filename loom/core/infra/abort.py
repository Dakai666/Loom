"""
AbortController — standard cancellation signal for Loom's async pipeline.

Inspired by the browser AbortController API and OpenClaw's
src/infra/abort-signal.ts. Uses asyncio.Event instead of a native signal
because Loom is async-only and Python's stdlib signal.AbortSignal is
sync-only.

Memory-leak safety
------------------
OpenClaw discovered that wrapping a controller in a closure for callbacks
creates a memory leak in long-running processes:

    # BAD — closure captures surrounding scope, accumulates in GC
    timer = setTimeout(() => { controller.abort(); }, 1000)

The fix is a bound function (see abort_bound() below), which has no
closure scope. Loom applies the same principle here: never use
`lambda: self.abort()` as a callback; always use `self._abort_bound`.

Reference: https://github.com/openclaw/openclaw/issues/7174
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class AbortController:
    """
    A cancellation controller compatible with asyncio.

    Wrap an operation in a `wait_aborted()` call to make it
    cancellable via `abort()`.  Multiple tasks can wait on the same
    controller simultaneously.

    Example
    -------
    >>> controller = AbortController()
    >>> async def long_task():
    ...     await wait_aborted(controller.signal)
    ...     # do work only if not aborted
    ...
    >>> asyncio.run(long_task())        # runs normally
    >>> controller.abort()              # cancels the task
    """

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = asyncio.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def signal(self) -> asyncio.Event:
        """Return the cancellation signal (an asyncio.Event)."""
        return self._cancelled

    def abort(self) -> None:
        """Signal cancellation to all waiting tasks."""
        self._cancelled.set()

    def reset(self) -> None:
        """Clear the abort signal so the controller can be reused for a new turn."""
        self._cancelled.clear()

    @property
    def aborted(self) -> bool:
        """Return True if abort() has been called."""
        return self._cancelled.is_set()

    # ------------------------------------------------------------------
    # Memory-leak safety
    # ------------------------------------------------------------------

    def _abort_bound(self) -> None:
        """
        Bound abort method — safe for use as a callback.

        Use this instead of ``lambda: controller.abort()`` to avoid
        closure-capture memory leaks in long-running processes.
        See: OpenClaw #7174.
        """
        self.abort()

    def bind(self) -> Callable[[], None]:
        """
        Return a bound zero-closure abort callback.

        Suitable for ``loop.call_later()``, ``asyncio.create_task()``,
        or any callback that would otherwise capture surrounding scope.
        """
        return self._abort_bound


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def wait_aborted(signal: asyncio.Event) -> None:
    """
    Wait until the signal is set.

    If the signal is already aborted, returns immediately.
    ``asyncio.CancelledError`` from an external task cancellation is
    **not caught** — it propagates normally up the call stack.
    Callers that need to handle both abort and external cancellation
    should wrap this function as needed.

    Parameters
    ----------
    signal:
        An ``asyncio.Event`` from an ``AbortController`` instance.
    """
    if signal.is_set():
        return
    await signal.wait()


def abort_bound(controller: AbortController) -> Callable[[], None]:
    """
    Return a bound abort function for use in callbacks.

    Equivalent to ``controller.bind()`` — provided as a standalone
    factory for clarity in call sites that prefer an explicit call.

    Use this instead of ``lambda: controller.abort()`` to avoid
    closure-capture memory leaks in long-running processes.
    """
    return controller.bind()
