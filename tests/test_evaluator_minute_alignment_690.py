"""#690 follow-up — run_forever aligns its 60 s poll to minute boundaries.

A plain ``sleep(60)`` lands a little late every cycle.  The evaluation
second creeps forward until it wraps past :59 and one minute is never
evaluated, which the ledger then records as ``missed/evaluator_gap``.
"""

from __future__ import annotations

import random
import types
from datetime import datetime, timedelta, UTC

import pytest

from loom.autonomy import evaluator as evaluator_mod
from loom.autonomy.evaluator import TriggerEvaluator
from loom.autonomy.triggers import CronTrigger


class _Stop(Exception):
    pass


class _Clock:
    """Wall clock that only moves when the evaluator sleeps.

    Every sleep oversleeps by a seeded 0.1–0.5 s, the way a loaded host does.
    """

    def __init__(self, start: datetime, cycles: int) -> None:
        self.now = start
        self.sleeps: list[float] = []
        self._cycles = cycles
        self._rng = random.Random(690)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) >= self._cycles:
            raise _Stop
        self.now += timedelta(seconds=seconds + self._rng.uniform(0.1, 0.5))


def _install(monkeypatch, clock: _Clock) -> None:
    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now

    monkeypatch.setattr(evaluator_mod, "datetime", _FakeDatetime)
    monkeypatch.setattr(evaluator_mod, "asyncio", types.SimpleNamespace(sleep=clock.sleep))


async def _run(monkeypatch, start: datetime, cycles: int, poll_interval: float = 60.0):
    clock = _Clock(start, cycles)
    _install(monkeypatch, clock)
    fired: list[datetime] = []
    missed: list = []

    async def on_fire(trigger, context):
        fired.append(clock.now.replace(second=0, microsecond=0))

    async def on_missed(trigger, span):
        missed.append(span)

    ev = TriggerEvaluator(on_fire=on_fire, on_missed=on_missed)
    ev.register(CronTrigger(name="every", intent="x", cron="* * * * *"))
    with pytest.raises(_Stop):
        await ev.run_forever(poll_interval=poll_interval)
    return clock, fired, missed


class TestMinuteAlignment:
    async def test_long_run_evaluates_every_minute_exactly_once(self, monkeypatch):
        start = datetime(2026, 11, 23, 8, 0, 59, 500_000, tzinfo=UTC)
        cycles = 2000
        clock, fired, missed = await _run(monkeypatch, start, cycles)

        assert missed == []
        expected = [start.replace(second=0, microsecond=0) + timedelta(minutes=i)
                    for i in range(cycles)]
        assert fired == expected

    async def test_sleep_never_drops_below_one_second(self, monkeypatch):
        # Woken at :59.8 the remainder is 0.2 s; the floor keeps it at 1 s.
        start = datetime(2026, 11, 23, 8, 0, 59, 800_000, tzinfo=UTC)
        clock, _, _ = await _run(monkeypatch, start, cycles=2)
        assert clock.sleeps[0] == 1.0

    async def test_other_intervals_sleep_exactly_the_interval(self, monkeypatch):
        start = datetime(2026, 11, 23, 8, 0, 17, 250_000, tzinfo=UTC)
        clock, _, _ = await _run(monkeypatch, start, cycles=5, poll_interval=300.0)
        assert clock.sleeps == [300.0] * 5
