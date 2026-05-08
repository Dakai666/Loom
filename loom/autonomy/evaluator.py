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
import logging
from datetime import datetime, UTC
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .history import TriggerHistory
from .triggers import (
    TriggerDefinition, TriggerKind,
    CronTrigger, EventTrigger, ConditionTrigger,
)

if TYPE_CHECKING:
    from loom.core.ledger import LedgerEmitter

_log = logging.getLogger(__name__)

FireCallback = Callable[[TriggerDefinition, dict[str, Any]], Awaitable[None]]


# doc/53 §3.1 — env_observation subtypes mapped from autonomy trigger kinds.
_KIND_TO_OBSERVATION = {
    TriggerKind.CRON: "timer",
    TriggerKind.EVENT: "external",
    TriggerKind.CONDITION: "anomaly",
}


class TriggerEvaluator:
    """
    Manages the lifecycle of all registered triggers.

    Usage:
        evaluator = TriggerEvaluator(on_fire=planner.handle)
        evaluator.register(CronTrigger(name="daily", cron="0 9 * * 1-5", intent="..."))
        await evaluator.evaluate_cron(datetime.now(UTC))
    """

    def __init__(
        self,
        on_fire: FireCallback | None = None,
        history: TriggerHistory | None = None,
        ledger_emitter: "LedgerEmitter | None" = None,
    ) -> None:
        self._triggers: dict[str, TriggerDefinition] = {}
        self._on_fire = on_fire
        self._history = history
        self._fired_this_minute: set[str] = set()
        self._last_minute: int | None = None
        # When wired, every trigger fire emits one env_observation event
        # under a fresh correlation_id (doc/53 §4.2 — env_observation is
        # one of the three event types that does not inherit the parent
        # correlation; it is the root of a new reaction chain).
        self._ledger_emitter = ledger_emitter

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
        Restores dedup state from persistent history on minute boundary (survives restarts).
        """
        if dt is None:
            dt = datetime.now(UTC)

        minute_key = dt.year * 100000 + dt.month * 10000 + dt.day * 100 + dt.hour * 60 + dt.minute
        if minute_key != self._last_minute:
            self._fired_this_minute = set()
            self._last_minute = minute_key
            # Restore from DB: any trigger that last fired in this exact minute
            # was already handled before the restart — skip it again.
            if self._history is not None:
                try:
                    for entry in await self._history.get_all():
                        last_iso = entry["last_fire_iso"]
                        last_dt = datetime.fromisoformat(last_iso)
                        lk = (
                            last_dt.year * 100000
                            + last_dt.month * 10000
                            + last_dt.day * 100
                            + last_dt.hour * 60
                            + last_dt.minute
                        )
                        if lk == minute_key:
                            self._fired_this_minute.add(entry["trigger_name"])
                except Exception:
                    pass  # History read failure must never block cron evaluation

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
                if self._history is not None:
                    try:
                        await self._history.record_fire(trigger.name, dt)
                    except Exception:
                        pass
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

        now = datetime.now(UTC)
        for trigger in self.list():
            if trigger.kind != TriggerKind.EVENT:
                continue
            assert isinstance(trigger, EventTrigger)
            if trigger.event_name == event_name:
                fired.append(trigger.name)
                if self._history is not None:
                    try:
                        await self._history.record_fire(trigger.name, now)
                    except Exception:
                        pass
                await self._fire(trigger, ctx)

        return fired

    # ------------------------------------------------------------------
    # Condition polling
    # ------------------------------------------------------------------

    async def poll_conditions(self) -> list[str]:
        """Evaluate all ConditionTriggers right now. Returns fired trigger names."""
        now = datetime.now(UTC)
        fired: list[str] = []
        for trigger in self.list():
            if trigger.kind != TriggerKind.CONDITION:
                continue
            assert isinstance(trigger, ConditionTrigger)
            if trigger.evaluate():
                fired.append(trigger.name)
                if self._history is not None:
                    try:
                        await self._history.record_fire(trigger.name, now)
                    except Exception:
                        pass
                await self._fire(trigger, {"source": "condition"})
        return fired

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _fire(
        self, trigger: TriggerDefinition, context: dict[str, Any]
    ) -> None:
        if self._ledger_emitter is None:
            if self._on_fire is not None:
                await self._on_fire(trigger, context)
            return

        from loom.core.ledger import (
            EnvObservationPayload,
            async_correlation_scope,
            new_correlation_id,
        )

        new_corr = new_correlation_id("env")
        observation_type = _KIND_TO_OBSERVATION.get(trigger.kind, "external")

        try:
            await self._ledger_emitter.emit_env_observation(
                turn_id="system",
                correlation_id=new_corr,
                payload=EnvObservationPayload(
                    observation_type=observation_type,
                    source="autonomy_daemon",
                    detail={
                        "trigger_name": trigger.name,
                        "trigger_kind": trigger.kind.value,
                        "context": context,
                    },
                ),
            )
        except Exception:  # noqa: BLE001
            _log.exception("ledger env_observation emit failed; continuing")

        # The reaction chain — planner.handle and any deeper emits — runs
        # under the fresh correlation, so future ledger queries can pull
        # the entire post-trigger chain by correlation_id.
        async with async_correlation_scope(new_corr):
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
