"""
Memory Maintenance Loop — Autonomy-driven (Issue #281 P3).

Periodic, non-LLM background sweep that calls ``MemoryLifecycle.run()``
on a fixed cadence. Lives in the autonomy layer (not core/memory) because
it's a *scheduling* concern — the lifecycle algorithm itself is content-
agnostic and stateless across calls.

Why a dedicated loop instead of registering as another ``CronTrigger``:
the existing trigger pipeline routes through ``ActionPlanner`` and runs
an *LLM intent* — overkill for a deterministic scan. This loop calls
the Python entry directly, avoids token cost, and stays cancellable via
the same ``AbortController`` the daemon uses for shutdown.

Throttle is enforced inside ``MemoryLifecycle.run()`` itself (via
``min_gap_minutes`` from ``[memory.lifecycle]``), so this loop and
``session.stop()``'s decay-cycle path can both fire freely without
double-scanning.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, UTC
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from loom.core.infra import AbortController, wait_aborted
from loom.core.memory.lifecycle import MemoryLifecycle

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


class MaintenanceLoop:
    """Drives MemoryLifecycle on a fixed interval until aborted."""

    # First sweep delay — short enough that a daemon restart (after a long
    # idle gap where session.stop also didn't run) gets a sweep within minutes,
    # long enough that startup work doesn't compete for the DB lock. The
    # throttle inside run() still skips if session.stop just ran one.
    _FIRST_SWEEP_DELAY_SECONDS = 300.0

    def __init__(
        self,
        db: "aiosqlite.Connection",
        abort: AbortController,
        interval_hours: float = 24.0,
        threshold: float = 0.1,
        min_gap_minutes: float = 30.0,
    ) -> None:
        self._db = db
        self._abort = abort
        self._interval_seconds = max(60.0, interval_hours * 3600.0)
        self._threshold = threshold
        self._min_gap_minutes = min_gap_minutes

    async def run_forever(self) -> None:
        """Run a sweep ``_FIRST_SWEEP_DELAY_SECONDS`` after start, then every
        ``interval_hours``. Returns when the abort signal fires (shutdown)."""
        first_iter = True
        while not self._abort.signal.is_set():
            timeout = (
                self._FIRST_SWEEP_DELAY_SECONDS if first_iter
                else self._interval_seconds
            )
            first_iter = False
            try:
                await asyncio.wait_for(
                    wait_aborted(self._abort.signal),
                    timeout=timeout,
                )
                # wait_aborted returned cleanly → signal set → exit
                return
            except asyncio.TimeoutError:
                pass  # interval elapsed, run a cycle

            try:
                cycle = MemoryLifecycle(self._db, threshold=self._threshold)
                result = await cycle.run(min_gap_minutes=self._min_gap_minutes)
                if result.skipped:
                    logger.debug("[maintenance] cycle skipped (throttle)")
                elif result.total_archived or result.total_deleted:
                    logger.info(
                        "[maintenance] archived=%d deleted=%d "
                        "(semantic %d/%d, relational %d/%d)",
                        result.total_archived, result.total_deleted,
                        result.semantic_archived, result.semantic_deleted,
                        result.relational_archived, result.relational_deleted,
                    )
            except Exception as exc:  # never let maintenance crash the daemon
                logger.warning("[maintenance] cycle failed: %s", exc)


# How often to re-check due-ness. The cadence is enforced by the persisted
# last_run, so the check interval only bounds how soon after a due moment (or
# a restart) the run actually fires.
DEFAULT_CHECK_INTERVAL_SECONDS = 6 * 3600.0
_META_KEY_LAST_RUN_DEFAULT = "consolidation_dream.last_run"


class ConvergentDreamLoop:
    """Restart-safe cadence for the memory-rewriting dreams: the weekly
    convergent dream (#495) and the 12 h distill dream (#613 E).

    The convergent (consolidation) dream rewrites memory, so its cadence must
    survive restarts: a daemon bounce must neither reset the week nor
    double-run. So due-ness is decided against a persisted
    ``consolidation_dream.last_run`` in ``memory_meta`` — not an in-memory
    timer — mirroring ``dreaming.next_themed_domain``'s restart-safe rotation.

    The loop wakes on a short *check* interval and runs the dream only when
    ``interval_days`` have elapsed since ``last_run``. ``last_run`` is marked
    **only after a successful pass**, so a failed pass (e.g. LLM outage) retries
    on the next check rather than silently skipping the week.

    ``dream_fn`` is supplied by the daemon and decides read-only (P4a) vs
    ``execute=True`` (P4b) — this loop only owns *when*, never *what*.

    The distill dream (#613 E) runs on the same loop with its own
    ``meta_key`` and a 12 h interval, so the two cadences never share state.
    """

    # Defer the first due-check so daemon startup isn't competing for the DB.
    _FIRST_CHECK_DELAY_SECONDS = 300.0

    def __init__(
        self,
        dream_fn: Callable[[], Awaitable[Any]],
        db: "aiosqlite.Connection",
        abort: AbortController,
        interval_days: float = 7.0,
        *,
        meta_key: str = _META_KEY_LAST_RUN_DEFAULT,
        check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        label: str = "consolidation",
    ) -> None:
        self._dream_fn = dream_fn
        self._db = db
        self._abort = abort
        self._interval_seconds = max(60.0, interval_days * 86400.0)
        self._meta_key = meta_key
        self._check_interval_seconds = check_interval_seconds
        self._label = label

    async def _last_run(self) -> datetime | None:
        cursor = await self._db.execute(
            "SELECT value FROM memory_meta WHERE key = ?",
            (self._meta_key,),
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return None
        try:
            return datetime.fromisoformat(row[0])
        except (ValueError, TypeError):
            return None  # corrupt value → treat as never-run (re-runs, marks fresh)

    async def _mark_run(self, now: datetime) -> None:
        stamp = now.isoformat()
        await self._db.execute(
            "INSERT INTO memory_meta(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (self._meta_key, stamp, stamp),
        )
        await self._db.commit()

    async def maybe_run_once(self, now: datetime) -> bool:
        """Run one convergent dream iff ``interval_days`` have elapsed.

        Returns True if it ran. ``last_run`` is persisted only after the pass
        succeeds, so a raising ``dream_fn`` propagates and leaves the week still
        due (retry next check, never a silent skip).
        """
        last = await self._last_run()
        if last is not None and (now - last).total_seconds() < self._interval_seconds:
            return False
        await self._dream_fn()       # may raise → last_run left unmarked
        await self._mark_run(now)
        return True

    async def run_forever(self) -> None:
        """Check due-ness ``_FIRST_CHECK_DELAY_SECONDS`` after start, then every
        ``check_interval_seconds``. Returns when the abort signal fires."""
        first_iter = True
        while not self._abort.signal.is_set():
            timeout = (
                self._FIRST_CHECK_DELAY_SECONDS if first_iter
                else self._check_interval_seconds
            )
            first_iter = False
            try:
                await asyncio.wait_for(
                    wait_aborted(self._abort.signal),
                    timeout=timeout,
                )
                return  # abort signalled
            except asyncio.TimeoutError:
                pass  # check interval elapsed

            try:
                if await self.maybe_run_once(datetime.now(UTC)):
                    logger.info(
                        "[%s] dream ran (interval=%.2fd)",
                        self._label, self._interval_seconds / 86400.0,
                    )
            except Exception as exc:  # never let a dream crash the daemon
                logger.warning("[%s] cycle failed: %s", self._label, exc)
