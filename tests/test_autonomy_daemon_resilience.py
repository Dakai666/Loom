"""The autonomy daemon must not die silently.

The evaluator awaits each trigger's reaction inline, and ``daemon.start()``
is launched fire-and-forget by the Discord platform.  So one reaction that
raised — or that leaked a ``CancelledError`` into the evaluator's task (the
MCP cancel-scope leak during circadian nightly close) — used to end
``run_forever``, and with it every schedule, with nothing on the console.

Contracts:

* A trigger's reaction runs in its own task.  If *it* is cancelled or raises
  while the evaluator itself is not being cancelled, the error is logged and
  the loop carries on.  A genuine shutdown cancel still propagates.
* ``start()`` supervises its loops: any loop that ends without the daemon
  having been stopped is logged and restarted.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from loom.autonomy.daemon import AutonomyDaemon


def _daemon() -> AutonomyDaemon:
    return AutonomyDaemon(notify_router=None, confirm_flow=None, loom_session=None)


def _trigger(name: str = "circadian:nightly_close") -> SimpleNamespace:
    return SimpleNamespace(name=name)


class TestTriggerReactionIsolation:
    async def test_leaked_cancel_in_direct_handler_is_contained(self, caplog) -> None:
        daemon = _daemon()

        async def leaks_cancel(_t, _c):
            # What an anyio cancel scope exited out of order does: cancel
            # the task that is running the reaction.
            asyncio.current_task().cancel()
            await asyncio.sleep(0)

        daemon.register_direct_handler("circadian:nightly_close", leaks_cancel)

        with caplog.at_level(logging.ERROR, logger="loom.autonomy.daemon"):
            await daemon._on_trigger_fire(_trigger(), {})
            # Still alive: the evaluator's own task can keep awaiting.
            await asyncio.sleep(0)

        assert asyncio.current_task().cancelling() == 0
        assert any(
            "circadian:nightly_close" in r.getMessage() and "cancel" in r.getMessage()
            for r in caplog.records
        )

    async def test_planner_path_exception_is_contained(self, caplog, monkeypatch) -> None:
        daemon = _daemon()

        async def boom(_t, _c):
            raise RuntimeError("planner exploded")

        monkeypatch.setattr(daemon._planner, "handle", boom)

        with caplog.at_level(logging.ERROR, logger="loom.autonomy.daemon"):
            await daemon._on_trigger_fire(_trigger("morning_digest"), {})

        assert any("morning_digest" in r.getMessage() for r in caplog.records)

    async def test_direct_handler_exception_is_contained(self, caplog) -> None:
        daemon = _daemon()

        async def boom(_t, _c):
            raise RuntimeError("close failed")

        daemon.register_direct_handler("circadian:nightly_close", boom)
        with caplog.at_level(logging.ERROR, logger="loom.autonomy.daemon"):
            await daemon._on_trigger_fire(_trigger(), {})
        assert any("circadian:nightly_close" in r.getMessage() for r in caplog.records)

    async def test_shutdown_cancel_still_propagates(self) -> None:
        daemon = _daemon()
        entered = asyncio.Event()

        async def slow(_t, _c):
            entered.set()
            await asyncio.sleep(3600)

        daemon.register_direct_handler("circadian:nightly_close", slow)
        fire = asyncio.create_task(daemon._on_trigger_fire(_trigger(), {}))
        await entered.wait()
        fire.cancel()
        with pytest.raises(asyncio.CancelledError):
            await fire

    async def test_reaction_runs_under_callers_context(self) -> None:
        """Correlation scopes are contextvars — the reaction task must see them."""
        import contextvars

        var: contextvars.ContextVar[str] = contextvars.ContextVar("corr", default="-")
        seen: list[str] = []
        daemon = _daemon()

        async def handler(_t, _c):
            seen.append(var.get())

        daemon.register_direct_handler("circadian:nightly_close", handler)
        var.set("env-123")
        await daemon._on_trigger_fire(_trigger(), {})
        assert seen == ["env-123"]


class TestStartSupervision:
    async def test_evaluator_loop_restarted_after_unexpected_end(
        self, caplog, monkeypatch
    ) -> None:
        daemon = _daemon()
        monkeypatch.setattr(AutonomyDaemon, "_LOOP_RESTART_DELAY_S", 0.0)
        runs: list[int] = []

        async def run_forever(poll_interval: float = 60.0) -> None:
            runs.append(1)
            if len(runs) == 1:
                raise asyncio.CancelledError()   # the leaked-cancel death
            if len(runs) == 2:
                raise RuntimeError("evaluator crashed")
            daemon.stop()
            await asyncio.sleep(3600)

        monkeypatch.setattr(daemon._evaluator, "run_forever", run_forever)

        with caplog.at_level(logging.ERROR, logger="loom.autonomy.daemon"):
            await asyncio.wait_for(daemon.start(poll_interval=0.01), timeout=5)

        assert len(runs) == 3
        messages = [r.getMessage() for r in caplog.records]
        assert sum("evaluator" in m and "restart" in m for m in messages) == 2

    async def test_stop_ends_start_without_restart(self, monkeypatch) -> None:
        daemon = _daemon()
        runs: list[int] = []

        async def run_forever(poll_interval: float = 60.0) -> None:
            runs.append(1)
            await asyncio.sleep(3600)

        monkeypatch.setattr(daemon._evaluator, "run_forever", run_forever)
        task = asyncio.create_task(daemon.start(poll_interval=0.01))
        await asyncio.sleep(0.05)
        daemon.stop()
        await asyncio.wait_for(task, timeout=5)
        assert runs == [1]

    async def test_cancelling_start_tears_loops_down(self, monkeypatch) -> None:
        daemon = _daemon()
        cancelled = asyncio.Event()

        async def run_forever(poll_interval: float = 60.0) -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        monkeypatch.setattr(daemon._evaluator, "run_forever", run_forever)
        task = asyncio.create_task(daemon.start(poll_interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()


class TestPlatformTaskReporting:
    """The Discord platform runs the daemon fire-and-forget; its end must
    leave a trace on the console instead of vanishing."""

    async def test_crash_is_logged(self, caplog) -> None:
        from loom.platform.cli.main import _log_background_task_end

        async def boom():
            raise RuntimeError("circadian setup failed")

        task = asyncio.create_task(boom(), name="autonomy-daemon-bootstrap")
        await asyncio.wait({task})
        with caplog.at_level(logging.ERROR, logger="loom.platform.cli.main"):
            _log_background_task_end(task)
        [record] = caplog.records
        assert "autonomy-daemon-bootstrap" in record.getMessage()
        assert record.exc_info is not None

    async def test_clean_return_is_silent(self, caplog) -> None:
        from loom.platform.cli.main import _log_background_task_end

        async def ok():
            return None

        task = asyncio.create_task(ok(), name="autonomy-daemon")
        await asyncio.wait({task})
        with caplog.at_level(logging.DEBUG, logger="loom.platform.cli.main"):
            _log_background_task_end(task)
        assert caplog.records == []
