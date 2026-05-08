"""Push API — async iterator subscribe + bounded buffer + drop-oldest.

doc/53 §6.1. Subscribers are read-only observers (§6.5); they cannot
block, modify, or cancel emit. The store fans out under an emit lock
so the (historical replay → live stream) handoff has no race.

Buffer policy: per-subscriber deque(maxlen=N) with drop-oldest on
overflow. Drops increment a monotonic counter, observable via
``is_live`` and ``lag_events``.

Usage:

    async with store.subscribe(
        event_types=["tool_lifecycle", "judge_verdict"],
        correlation_id="user_q_xyz",
        replay_from=time.time() - 10,
    ) as subscriber:
        async for event in subscriber:
            handle(event)
            if not subscriber.is_live:
                show_lag_indicator(subscriber.lag_events)
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING

from loom.core.ledger.schema import DEFAULT_BRANCH, LedgerEvent

if TYPE_CHECKING:
    from loom.core.ledger.store import LedgerStore

_log = logging.getLogger(__name__)


class LedgerSubscriber:
    """One bounded-buffer observer of the ledger event stream.

    Acquired via ``async with store.subscribe(...) as subscriber``; the
    context manager handles registration and cleanup. Direct
    instantiation is package-private.
    """

    def __init__(
        self,
        *,
        buffer_size: int = 100,
        event_types: list[str] | None = None,
        correlation_id: str | None = None,
        branch_id: str | None = DEFAULT_BRANCH,
        session_id: str | None = None,
    ) -> None:
        self._buffer: deque[LedgerEvent] = deque(maxlen=buffer_size)
        self._wakeup = asyncio.Event()
        self._closed = False
        self._dropped_total = 0
        self._event_types = set(event_types) if event_types else None
        self._correlation_id = correlation_id
        self._branch_id = branch_id
        self._session_id = session_id
        self._last_ts: float | None = None

    # -- read-only properties (doc/53 §6.1) ------------------------------

    @property
    def is_live(self) -> bool:
        """False once any drop has occurred — the stream has gaps and
        is no longer authoritative for replay. Re-subscribe to reset
        (drops are facts of history; toggling on drain would be a lie).
        """
        return self._dropped_total == 0

    @property
    def lag_events(self) -> int:
        return len(self._buffer)

    @property
    def last_event_timestamp(self) -> float | None:
        return self._last_ts

    @property
    def dropped_total(self) -> int:
        """Cumulative drop count over the subscriber's lifetime."""
        return self._dropped_total

    # -- internal: matched against subscriber filters --------------------

    def _matches(self, event: LedgerEvent) -> bool:
        if self._branch_id is not None and event.branch_id != self._branch_id:
            return False
        if self._event_types is not None and event.event_type not in self._event_types:
            return False
        if (
            self._correlation_id is not None
            and event.correlation_id != self._correlation_id
        ):
            return False
        if self._session_id is not None and event.session_id != self._session_id:
            return False
        return True

    def _push(self, event: LedgerEvent) -> None:
        """Push one event into the buffer, dropping oldest on overflow.

        Called by LedgerStore.emit() under the emit lock. Sync — never
        awaits, never blocks the publisher.
        """
        if self._closed:
            return
        if not self._matches(event):
            return
        if len(self._buffer) >= (self._buffer.maxlen or 0):
            self._dropped_total += 1
            _log.warning(
                "ledger subscriber dropped event %s (buffer=%d, total drops=%d)",
                event.event_id,
                self._buffer.maxlen,
                self._dropped_total,
            )
        # deque.append handles drop-oldest automatically when at maxlen
        self._buffer.append(event)
        self._wakeup.set()

    def _close(self) -> None:
        """Mark closed and wake any pending __anext__ so it can exit."""
        self._closed = True
        self._wakeup.set()

    # -- async iterator protocol -----------------------------------------

    def __aiter__(self) -> "LedgerSubscriber":
        return self

    async def __anext__(self) -> LedgerEvent:
        while True:
            if self._buffer:
                event = self._buffer.popleft()
                self._last_ts = event.timestamp
                return event
            if self._closed:
                raise StopAsyncIteration
            self._wakeup.clear()
            await self._wakeup.wait()


class _SubscribeContextManager:
    """Async context manager returned by ``LedgerStore.subscribe()``.

    Handles atomic (replay history → register live) handoff under the
    store's emit lock so no event is missed or duplicated at the
    subscription boundary.
    """

    def __init__(
        self,
        store: "LedgerStore",
        *,
        buffer_size: int = 100,
        event_types: list[str] | None = None,
        correlation_id: str | None = None,
        branch_id: str | None = DEFAULT_BRANCH,
        session_id: str | None = None,
        replay_from: float | datetime | None = None,
    ) -> None:
        self._store = store
        self._kwargs = dict(
            buffer_size=buffer_size,
            event_types=event_types,
            correlation_id=correlation_id,
            branch_id=branch_id,
            session_id=session_id,
        )
        if isinstance(replay_from, datetime):
            replay_from = replay_from.timestamp()
        self._replay_from: float | None = replay_from
        self._subscriber: LedgerSubscriber | None = None

    async def __aenter__(self) -> LedgerSubscriber:
        sub = LedgerSubscriber(**self._kwargs)
        self._subscriber = sub
        # Atomic handoff: replay historical (if requested) and register
        # in one critical section so emit() cannot interleave and miss
        # or duplicate any event at the boundary.
        async with self._store._emit_lock:
            if self._replay_from is not None:
                # Push the subscriber's branch_id filter down to SQL so
                # the historical leg doesn't fetch rows that _matches
                # would just drop. event_types / correlation_id /
                # session_id intentionally stay at the _matches stage
                # (no covering index makes them useful at SQL layer).
                historical = await self._store._fetch_since(
                    self._replay_from,
                    branch_id=sub._branch_id,
                )
                for event in historical:
                    sub._push(event)
            self._store._subscribers.append(sub)
        return sub

    async def __aexit__(self, *_exc) -> None:
        sub = self._subscriber
        if sub is None:
            return
        async with self._store._emit_lock:
            try:
                self._store._subscribers.remove(sub)
            except ValueError:
                pass
        sub._close()
