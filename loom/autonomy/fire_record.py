"""Trigger fire records (#690 S5a).

Cron schedules, rhythm anchors and the memory loops each keep their own
scheduling. They share one record of what happened when they came due: one
``trigger_fire`` ledger event per fire, with when it fired, when its run
started and ended, and how it ended.

``trigger_history`` stays the restart-dedup table. This record is the
outcome log, which that table never was.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from loom.core.ledger.schema import TriggerFirePayload

if TYPE_CHECKING:
    from loom.core.ledger import LedgerEmitter

_log = logging.getLogger(__name__)

# Synthetic turn_id for daemon-originated events (doc/53 §5.2 requires
# turn_id NOT NULL): the evaluator's env_observation roots and the fire
# records. The trigger name lives in the payload. When the planner spawns a
# real LoomSession in reaction to a fire, that turn carries its own turn_id.
DAEMON_TURN_ID = "system:autonomy"


class FireOutcome(StrEnum):
    COMPLETED = "completed"   # the reaction ran to its end
    FAILED = "failed"         # the reaction raised; reason is the exception type
    CANCELLED = "cancelled"   # cancelled from inside the reaction
    SKIPPED = "skipped"       # ran and chose not to act; reason says why
    SPAWNED = "spawned"       # circadian dawn: today's session was created
    DELIVERED = "delivered"   # chime accepted by its target session's queue
    MISSED = "missed"         # due, but never reached the agent; reason says why


@dataclass(frozen=True)
class RunResult:
    """How a reaction ended, when it knows better than "completed"."""

    outcome: FireOutcome
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.outcome in (FireOutcome.MISSED, FireOutcome.SKIPPED) and not self.reason:
            raise ValueError(f"{self.outcome} needs a reason")


COMPLETED = RunResult(FireOutcome.COMPLETED)


def _epoch(dt: datetime | None) -> float | None:
    return dt.timestamp() if dt is not None else None


class FireLog:
    """Writes fire records to the ledger. Never raises: a ledger failure
    must not stop a schedule. No emitter (ledger off, or no session) makes
    every call a no-op."""

    def __init__(self, emitter: "LedgerEmitter | None") -> None:
        self._emitter = emitter

    async def record(
        self,
        *,
        source: str,
        name: str,
        fired_at: datetime,
        result: RunResult,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        missed_through: datetime | None = None,
        missed_count: int | None = None,
    ) -> None:
        if self._emitter is None:
            return
        from loom.core.ledger import current_correlation, new_correlation_id

        try:
            await self._emitter.emit(
                "trigger_fire",
                TriggerFirePayload(
                    source=source,
                    name=name,
                    fired_at=fired_at.timestamp(),
                    started_at=_epoch(started_at),
                    ended_at=_epoch(ended_at),
                    outcome=result.outcome.value,
                    reason=result.reason,
                    missed_through=_epoch(missed_through),
                    missed_count=missed_count,
                ),
                turn_id=DAEMON_TURN_ID,
                # A trigger reaction runs under its fire's env_observation
                # correlation; a memory loop or a missed minute starts its own.
                correlation_id=current_correlation() or new_correlation_id("fire"),
            )
        except Exception:  # noqa: BLE001
            _log.exception("[autonomy] fire record for %s not written", name)
