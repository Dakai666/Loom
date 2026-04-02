"""
Trigger Evaluator — the runtime that watches all registered triggers
and fires callbacks when they match.

  evaluate_cron(dt)   — call once per minute with the current time
  emit(event_name)    — fire all matching EventTriggers
  poll_conditions()   — check all ConditionTriggers right now

When a trigger fires, the evaluator calls the registered `on_fire` callback
with (trigger, context_dict).  The Action Planner is wired in as that callback.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, UTC
from typing import Any, Awaitable, Callable

from .triggers import (
    TriggerDefinition, TriggerKind,
    CronTrigger, EventTrigger, ConditionTrigger,
)

FireCallback = Callable[[TriggerDefinition, dict[str, Any]], Awaitable[None]]


class TriggerEvaluator:
    """
    Manages the lifecycle of all registered triggers.

    Usage:
        evaluator = TriggerEvaluator(on_fire=planner.handle)
        evaluator.register(CronTrigger(name="daily", cron="0 9 * * 1-5", intent="..."))
        await evaluator.evaluate_cron(datetime.now(UTC))
    """

    def __init__(self, on_fire: FireCallback | None = None) -> None:
        self._triggers: dict[str, TriggerDefinition] = {}
        self._on_fire = on_fire
        self._fired_this_minute: set[str] = set()
        self._last_minute: int | None = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, trigger: TriggerDefinition) -> None:
        self._triggers[trigger.name] = trigger

    def unregister(self, name: str) -> None:
        self._triggers.pop(name, None)

    def list(self) -> list[TriggerDefinition]:
        return [t for t in self._triggers.values() if t.enabled]

    # ------------------------------------------------------------------
    # Cron evaluation (call once per minute)
    # ------------------------------------------------------------------

    async def evaluate_cron(self, dt: datetime | None = None) -> list[str]:
        """
        Check all CronTriggers against `dt`.
        Returns names of triggers that fired.
        Deduplicates: a cron trigger fires at most once per minute.
        """
        if dt is None:
            dt = datetime.now(UTC)

        # Reset dedup set on new minute
        minute_key = dt.year * 100000 + dt.month * 10000 + dt.day * 100 + dt.hour * 60 + dt.minute
        if minute_key != self._last_minute:
            self._fired_this_minute = set()
            self._last_minute = minute_key

        fired: list[str] = []
        for trigger in self.list():
            if trigger.kind != TriggerKind.CRON:
                continue
            assert isinstance(trigger, CronTrigger)
            if trigger.name in self._fired_this_minute:
                continue
            if trigger.should_fire(dt):
                self._fired_this_minute.add(trigger.name)
                fired.append(trigger.name)
                await self._fire(trigger, {"triggered_at": dt.isoformat(), "source": "cron"})

        return fired

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    async def emit(self, event_name: str, context: dict[str, Any] | None = None) -> list[str]:
        """Fire all EventTriggers matching `event_name`. Returns fired trigger names."""
        fired: list[str] = []
        ctx = context or {}
        ctx["event_name"] = event_name

        for trigger in self.list():
            if trigger.kind != TriggerKind.EVENT:
                continue
            assert isinstance(trigger, EventTrigger)
            if trigger.event_name == event_name:
                fired.append(trigger.name)
                await self._fire(trigger, ctx)

        return fired

    # ------------------------------------------------------------------
    # Condition polling
    # ------------------------------------------------------------------

    async def poll_conditions(self) -> list[str]:
        """Evaluate all ConditionTriggers right now. Returns fired trigger names."""
        fired: list[str] = []
        for trigger in self.list():
            if trigger.kind != TriggerKind.CONDITION:
                continue
            assert isinstance(trigger, ConditionTrigger)
            if trigger.evaluate():
                fired.append(trigger.name)
                await self._fire(trigger, {"source": "condition"})
        return fired

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _fire(
        self, trigger: TriggerDefinition, context: dict[str, Any]
    ) -> None:
        if self._on_fire is not None:
            await self._on_fire(trigger, context)

    # ------------------------------------------------------------------
    # Background runner
    # ------------------------------------------------------------------

    async def run_forever(self, poll_interval: float = 60.0) -> None:
        """
        Run the evaluator as a background task.
        Checks cron triggers every `poll_interval` seconds (default 60s).
        Condition triggers are also polled each cycle.
        """
        while True:
            now = datetime.now(UTC)
            await self.evaluate_cron(now)
            await self.poll_conditions()
            await asyncio.sleep(poll_interval)
