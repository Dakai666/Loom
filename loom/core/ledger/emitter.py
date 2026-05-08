"""LedgerEmitter — thin sugar layer on top of LedgerStore.

Why this exists:
    Subsystem code (middleware / session / memory / scheduler / autonomy)
    should not have to:
      - mint event_ids
      - timestamp every event
      - thread session_id through every call
      - look up the active correlation_id from the contextvar

    The emitter binds session_id once at LoomSession.start() time and
    auto-fills the rest. Subsystems just call:

        await emitter.emit_tool_lifecycle(turn_id=..., payload=...)

doc/53 references:
    §4.1   five-ID identity model
    §4.2   correlation_id auto-inherit
    §11.1  layered emit responsibilities
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from loom.core.ledger.correlation import (
    current_correlation,
    current_turn_id,
    new_correlation_id,
)
from loom.core.ledger.schema import (
    DEFAULT_BRANCH,
    ArtifactEmitPayload,
    EnvObservationPayload,
    JudgeVerdictPayload,
    LedgerEvent,
    MemoryOpPayload,
    ModelEventPayload,
    PermissionDecisionPayload,
    TaskMutationPayload,
    ThoughtPayload,
    ToolLifecyclePayload,
    TurnEndPayload,
    TurnStartPayload,
)
from loom.core.ledger.store import LedgerStore


def _new_event_id(prefix: str = "evt") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class LedgerEmitter:
    """Session-scoped emit helper. One per LoomSession."""

    def __init__(
        self,
        store: LedgerStore,
        *,
        session_id: str,
        branch_id: str = DEFAULT_BRANCH,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.branch_id = branch_id

    # -- generic emit ------------------------------------------------------

    async def emit(
        self,
        event_type: str,
        payload: Any,
        *,
        turn_id: str | None = None,
        correlation_id: str | None = None,
        parent_event_id: str | None = None,
        event_id: str | None = None,
        timestamp: float | None = None,
    ) -> str:
        """Append an event. Returns the event_id (auto-generated if not given).

        ``turn_id`` resolution: explicit arg → contextvar → RuntimeError.
        Emit outside any turn is a programmer error; ledger consumers index
        by turn_id and a missing value would silently break replay.

        ``correlation_id`` resolution: explicit arg → contextvar → fresh
        ``new_correlation_id("orphan")`` fallback (logged via name prefix
        rather than raising — orphans are recoverable, missing turns aren't).
        """
        if turn_id is None:
            turn_id = current_turn_id()
        if turn_id is None:
            raise RuntimeError(
                f"emit({event_type!r}) called with no turn_id and no active "
                "turn_scope; wrap the call site in turn_scope() or pass turn_id explicitly"
            )
        if correlation_id is None:
            correlation_id = current_correlation() or new_correlation_id("orphan")
        eid = event_id or _new_event_id()
        evt = LedgerEvent(
            event_id=eid,
            session_id=self.session_id,
            turn_id=turn_id,
            parent_event_id=parent_event_id,
            correlation_id=correlation_id,
            branch_id=self.branch_id,
            event_type=event_type,
            timestamp=timestamp if timestamp is not None else time.time(),
            payload=payload,
        )
        await self.store.emit(evt)
        return eid

    # -- typed shortcuts (one per event_type in §3.1) ---------------------

    async def emit_turn_start(
        self, *, turn_id: str | None = None, payload: TurnStartPayload, **kwargs: Any
    ) -> str:
        return await self.emit("turn_start", payload, turn_id=turn_id, **kwargs)

    async def emit_turn_end(
        self, *, turn_id: str | None = None, payload: TurnEndPayload, **kwargs: Any
    ) -> str:
        return await self.emit("turn_end", payload, turn_id=turn_id, **kwargs)

    async def emit_thought(
        self, *, turn_id: str | None = None, payload: ThoughtPayload, **kwargs: Any
    ) -> str:
        return await self.emit("thought", payload, turn_id=turn_id, **kwargs)

    async def emit_model_event(
        self, *, turn_id: str | None = None, payload: ModelEventPayload, **kwargs: Any
    ) -> str:
        return await self.emit("model_event", payload, turn_id=turn_id, **kwargs)

    async def emit_tool_lifecycle(
        self, *, turn_id: str | None = None, payload: ToolLifecyclePayload, **kwargs: Any
    ) -> str:
        return await self.emit("tool_lifecycle", payload, turn_id=turn_id, **kwargs)

    async def emit_permission_decision(
        self, *, turn_id: str | None = None, payload: PermissionDecisionPayload, **kwargs: Any
    ) -> str:
        return await self.emit(
            "permission_decision", payload, turn_id=turn_id, **kwargs
        )

    async def emit_memory_op(
        self, *, turn_id: str | None = None, payload: MemoryOpPayload, **kwargs: Any
    ) -> str:
        return await self.emit("memory_op", payload, turn_id=turn_id, **kwargs)

    async def emit_task_mutation(
        self, *, turn_id: str | None = None, payload: TaskMutationPayload, **kwargs: Any
    ) -> str:
        return await self.emit("task_mutation", payload, turn_id=turn_id, **kwargs)

    async def emit_judge_verdict(
        self, *, turn_id: str | None = None, payload: JudgeVerdictPayload, **kwargs: Any
    ) -> str:
        return await self.emit("judge_verdict", payload, turn_id=turn_id, **kwargs)

    async def emit_artifact_emit(
        self, *, turn_id: str | None = None, payload: ArtifactEmitPayload, **kwargs: Any
    ) -> str:
        return await self.emit("artifact_emit", payload, turn_id=turn_id, **kwargs)

    async def emit_env_observation(
        self, *, turn_id: str | None = None, payload: EnvObservationPayload, **kwargs: Any
    ) -> str:
        return await self.emit("env_observation", payload, turn_id=turn_id, **kwargs)
