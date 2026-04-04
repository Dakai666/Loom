"""
Trigger definitions — the three sources that can wake the Autonomy Engine.

CronTrigger      — fires on a cron schedule (e.g. "0 9 * * 1-5")
EventTrigger     — fires when a named event is emitted
ConditionTrigger — fires when a callable condition returns True

All triggers carry an `intent` field: a plain-language description of what
the agent should do when this trigger fires.  The Action Planner reads this
to decide on a course of action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from typing import Any, Callable


class TriggerKind(Enum):
    CRON      = "cron"
    EVENT     = "event"
    CONDITION = "condition"


@dataclass
class TriggerDefinition:
    """Base fields shared by all trigger types."""
    name: str
    intent: str                       # what the agent should do
    trust_level: str = "guarded"      # "safe" | "guarded" | "critical"
    notify: bool = True               # push notification before acting?
    enabled: bool = True
    notify_thread_id: int = 0         # Discord thread ID for result delivery (0 = default channel)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> TriggerKind:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Cron trigger
# ---------------------------------------------------------------------------

# Minimal cron field validator (digit, *, ranges, lists)
_CRON_FIELD = re.compile(r'^(\*|\d+(-\d+)?(,\d+(-\d+)?)*)(/\d+)?$')

# (min, max) inclusive for each cron position: minute hour dom month dow
_CRON_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


def _validate_cron_field_range(field: str, lo: int, hi: int, field_name: str, expr: str) -> None:
    """Validate that every literal number in a cron field is within [lo, hi]."""
    # Strip step suffix before checking values
    base = field.split("/")[0] if "/" in field else field
    if base == "*":
        return
    for part in base.split(","):
        bounds = part.split("-")
        for token in bounds:
            n = int(token)
            if not (lo <= n <= hi):
                raise ValueError(
                    f"Cron field {field_name}={field!r} value {n} out of range "
                    f"[{lo},{hi}] in {expr!r}"
                )


def _validate_cron(expr: str) -> None:
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Cron expression must have 5 fields, got: {expr!r}")
    field_names = ("minute", "hour", "dom", "month", "dow")
    for part, name, (lo, hi) in zip(parts, field_names, _CRON_RANGES):
        if not _CRON_FIELD.match(part):
            raise ValueError(f"Invalid cron field {part!r} in {expr!r}")
        _validate_cron_field_range(part, lo, hi, name, expr)


@dataclass
class CronTrigger(TriggerDefinition):
    """Fires on a 5-field cron schedule (minute hour dom month dow)."""
    cron: str = "0 9 * * *"
    timezone: str = "UTC"

    def __post_init__(self):
        _validate_cron(self.cron)

    @property
    def kind(self) -> TriggerKind:
        return TriggerKind.CRON

    def should_fire(self, dt: datetime) -> bool:
        """
        Check whether the given datetime matches this cron expression.
        Supports: exact values, '*', lists (1,2,3), ranges (1-5), step (*/5).
        """
        minute, hour, dom, month, dow = self.cron.strip().split()
        # Standard cron: 0=Sun, 1=Mon … 6=Sat
        # Python weekday(): 0=Mon … 6=Sun → convert: (weekday+1) % 7
        cron_dow = (dt.weekday() + 1) % 7
        return (
            _cron_matches(minute, dt.minute)
            and _cron_matches(hour, dt.hour)
            and _cron_matches(dom, dt.day)
            and _cron_matches(month, dt.month)
            and _cron_matches(dow, cron_dow)
        )


def _cron_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    # Step syntax: */n or base/n
    if "/" in field:
        base, step_s = field.split("/", 1)
        step = int(step_s)
        start = 0 if base == "*" else int(base)
        return (value - start) % step == 0 and value >= start
    # List of ranges/values
    for part in field.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            if int(lo) <= value <= int(hi):
                return True
        else:
            if int(part) == value:
                return True
    return False


# ---------------------------------------------------------------------------
# Event trigger
# ---------------------------------------------------------------------------

@dataclass
class EventTrigger(TriggerDefinition):
    """Fires when `event_name` is emitted via TriggerEvaluator.emit()."""
    event_name: str = ""

    @property
    def kind(self) -> TriggerKind:
        return TriggerKind.EVENT


# ---------------------------------------------------------------------------
# Condition trigger
# ---------------------------------------------------------------------------

@dataclass
class ConditionTrigger(TriggerDefinition):
    """
    Fires when `condition_fn()` returns True.
    Polled periodically by TriggerEvaluator.
    """
    condition_fn: Callable[[], bool] | None = None

    @property
    def kind(self) -> TriggerKind:
        return TriggerKind.CONDITION

    def evaluate(self) -> bool:
        if self.condition_fn is None:
            return False
        try:
            return bool(self.condition_fn())
        except Exception:
            return False
