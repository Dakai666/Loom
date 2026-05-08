"""AgentLedger — unified append-only event store (Quest B).

See doc/53-AgentLedger-設計.md for design.

Phase 2 Step 1: storage layer only — schema, dataclasses, emit API,
blob helper, resolve_memory_id helper, maintenance hook.
Layered emit (Step 2) and projection contract (Step 3-4) come later.
"""

from loom.core.ledger.correlation import (
    async_correlation_scope,
    async_turn_scope,
    correlation_scope,
    current_correlation,
    current_turn_id,
    new_correlation_id,
    reset_correlation,
    reset_turn_id,
    set_correlation,
    set_turn_id,
    turn_scope,
)
from loom.core.ledger.emitter import LedgerEmitter
from loom.core.ledger.schema import (
    DEFAULT_BRANCH,
    LEDGER_BLOB_SUBDIR,
    THOUGHT_EXTERNAL_THRESHOLD,
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

__all__ = [
    "DEFAULT_BRANCH",
    "LEDGER_BLOB_SUBDIR",
    "THOUGHT_EXTERNAL_THRESHOLD",
    "ArtifactEmitPayload",
    "EnvObservationPayload",
    "JudgeVerdictPayload",
    "LedgerEmitter",
    "LedgerEvent",
    "LedgerStore",
    "MemoryOpPayload",
    "ModelEventPayload",
    "PermissionDecisionPayload",
    "TaskMutationPayload",
    "ThoughtPayload",
    "ToolLifecyclePayload",
    "TurnEndPayload",
    "TurnStartPayload",
    "async_correlation_scope",
    "async_turn_scope",
    "correlation_scope",
    "current_correlation",
    "current_turn_id",
    "new_correlation_id",
    "reset_correlation",
    "reset_turn_id",
    "set_correlation",
    "set_turn_id",
    "turn_scope",
]
