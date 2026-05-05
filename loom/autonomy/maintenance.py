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
from typing import TYPE_CHECKING

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
