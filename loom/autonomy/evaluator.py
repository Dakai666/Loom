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
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .fire_record import DAEMON_TURN_ID
from .history import TriggerHistory
from .triggers import (
    TriggerDefinition, TriggerKind,
    CronTrigger, EventTrigger, ConditionTrigger,
)

if TYPE_CHECKING:
    from loom.core.ledger import LedgerEmitter

_log = logging.getLogger(__name__)

FireCallback = Callable[[TriggerDefinition, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class MissedSpan:
    """The due minutes of one cron trigger inside one evaluation gap."""

    first: datetime
    last: datetime
    count: int


MissedCallback = Callable[[CronTrigger, MissedSpan], Awaitable[None]]

# How far back a late evaluation looks for cron minutes it skipped (#690 S5a).
_MAX_GAP = timedelta(hours=24)


def _minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


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
        on_missed: MissedCallback | None = None,
    ) -> None:
        self._triggers: dict[str, TriggerDefinition] = {}
        self._on_fire = on_fire
        self._history = history
        self._fired_this_minute: set[str] = set()
        self._last_minute: datetime | None = None
        # Called once per cron trigger per evaluation gap, with the span of
        # its due minutes that passed while nothing evaluated them: a
        # reaction held the loop, or the host slept.
        self._on_missed = on_missed
        # A gap means a miss only when polling is meant to see every minute;
        # run_forever turns this off for a poll interval above 60 s.
        self._detect_gaps = True
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

        minute = _minute(dt)
        gap_since: datetime | None = None
        if minute != self._last_minute:
            previous, self._last_minute = self._last_minute, minute
            self._fired_this_minute = set()
            if previous is not None and minute - previous > timedelta(minutes=1):
                gap_since = previous
            # Restore from DB: any trigger that last fired in this exact minute
            # was already handled before the restart — skip it again.
            if self._history is not None:
                try:
                    for entry in await self._history.get_all():
                        last_dt = datetime.fromisoformat(entry["last_fire_iso"])
                        if _minute(last_dt) == minute:
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

        # Report the gap only after this minute's fires, so a long gap can't
        # push them into the next minute.
        if gap_since is not None:
            await self._report_missed(gap_since, minute)
        return fired

    async def _report_missed(self, previous: datetime, current: datetime) -> None:
        """Report cron minutes strictly between two evaluations, one span
        per trigger.

        Catch-up is not this layer's job (#668); this only makes the loss
        visible.
        """
        if self._on_missed is None or not self._detect_gaps:
            return
        spans: dict[str, tuple[CronTrigger, MissedSpan]] = {}
        triggers = [t for t in self.list() if isinstance(t, CronTrigger)]
        due = max(previous, current - _MAX_GAP) + timedelta(minutes=1)
        while due < current:
            for trigger in triggers:
                if trigger.should_fire(due):
                    seen = spans.get(trigger.name)
                    first, count = (seen[1].first, seen[1].count) if seen else (due, 0)
                    spans[trigger.name] = (trigger, MissedSpan(first, due, count + 1))
            due += timedelta(minutes=1)
        for trigger, span in spans.values():
            try:
                await self._on_missed(trigger, span)
            except Exception:  # noqa: BLE001
                _log.exception("missed-fire report for %s failed", trigger.name)

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
        # #690 S3b: every reaction learns when its trigger fired (UTC
        # epoch), so a turn that runs later still records the fire time.
        context = {**context, "fired_at": datetime.now(UTC).timestamp()}
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
                turn_id=DAEMON_TURN_ID,
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
        self._detect_gaps = poll_interval <= 60
        while True:
            now = datetime.now(UTC)
            await self.evaluate_cron(now)
            await self.poll_conditions()
            await asyncio.sleep(poll_interval)
