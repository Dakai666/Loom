"""LedgerEvent + per-event-type payload dataclasses + SQL DDL.

Reference: doc/53-AgentLedger-設計.md §5 (storage), §7 (dataclasses).

Field shapes are the contract. Adding fields is fine; renaming or
re-typing existing fields is a breaking change subject to §9.4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

THOUGHT_EXTERNAL_THRESHOLD = 50_000
"""Bytes. thought.full_text above this goes to blob storage (§3.3)."""

LEDGER_BLOB_SUBDIR = "ledger_blobs"
"""Sub-directory under the ledger root for thought blobs."""

DEFAULT_BRANCH = "main"
"""v1 branch_id is always 'main'. v2 fork primitive will assign others (§4.4)."""

CURRENT_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Base event (§7.1)
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class LedgerEvent:
    """Base envelope for every ledger event.

    `payload` carries event-type-specific fields (see *Payload classes below)
    and must include "schema_version" (§9.1).
    """

    event_id: str
    session_id: str
    turn_id: str
    correlation_id: str
    event_type: str
    timestamp: float  # UTC Unix epoch (REAL) — §9.5
    payload: dict[str, Any]
    parent_event_id: str | None = None
    branch_id: str = DEFAULT_BRANCH


# ---------------------------------------------------------------------------
# Per-event-type payloads (§7.2). schema_version defaults to 1.
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class TurnStartPayload:
    prompt_stack_hash: str
    prompt_stack_components: dict[str, Any]
    full_text: str | None = None
    schema_version: int = CURRENT_SCHEMA_VERSION


@dataclass(kw_only=True)
class TurnEndPayload:
    outcome: str  # "clean" / "retry" / "abandoned" / "error"
    duration_ms: int
    token_usage: dict[str, int]
    schema_version: int = CURRENT_SCHEMA_VERSION


@dataclass(kw_only=True)
class ThoughtPayload:
    digest: str
    duration_ms: int
    produced_tool_calls: int
    full_text: str | None = None
    external_ref: str | None = None
    schema_version: int = CURRENT_SCHEMA_VERSION


@dataclass(kw_only=True)
class ModelEventPayload:
    model: str
    tier: int
    token_usage: dict[str, int]
    schema_version: int = CURRENT_SCHEMA_VERSION


@dataclass(kw_only=True)
class ToolLifecyclePayload:
    phase: str  # "BEGIN" / "STATE_CHANGE" / "END" / "ROLLBACK"
    tool_name: str
    tool_call_id: str
    args_digest: str
    args: dict[str, Any] | None = None
    result_digest: str | None = None
    result_summary: str | None = None
    state_history: list[dict] = field(default_factory=list)
    rolled_back: bool = False
    error: str | None = None
    # Populated for skill-scoped tools (load_skill / unload_skill) so the
    # `skill_id` virtual column (doc/54 §5 P0-1) can index per-skill usage.
    # NULL for all other tools.
    skill_id: str | None = None
    schema_version: int = CURRENT_SCHEMA_VERSION


@dataclass(kw_only=True)
class PermissionDecisionPayload:
    decision: str  # "grant" / "deny" / "scope"
    tool_call_id: str
    trust_level: str
    scope_grant: dict | None = None
    reason: str | None = None
    schema_version: int = CURRENT_SCHEMA_VERSION


@dataclass(kw_only=True)
class MemoryOpPayload:
    operation: str  # "read" / "write" / "compact" / "batch_read"
    memory_id: str | None = None
    memory_ids: list[str] | None = None
    predecessor_memory_id: str | None = None
    successor_memory_id: str | None = None
    type_summary: str | None = None
    trust_tier: str | None = None
    content_digest: str | None = None
    trigger: str | None = None
    schema_version: int = CURRENT_SCHEMA_VERSION


@dataclass(kw_only=True)
class TaskMutationPayload:
    operation: str  # "write" / "done" / "modify" / "abandon"
    task_id: str
    task_state: dict[str, Any] | None = None
    schema_version: int = CURRENT_SCHEMA_VERSION


@dataclass(kw_only=True)
class JudgeVerdictPayload:
    verdict: str  # "PASS" / "FAIL" / "ERROR" / "CONCERN"
    confidence: float
    reason: str
    judged_subject: str
    schema_version: int = CURRENT_SCHEMA_VERSION


@dataclass(kw_only=True)
class ArtifactEmitPayload:
    artifact_type: str
    size_bytes: int
    digest: str
    location: str | None = None
    schema_version: int = CURRENT_SCHEMA_VERSION


@dataclass(kw_only=True)
class EnvObservationPayload:
    observation_type: str
    source: str
    detail: dict[str, Any]
    schema_version: int = CURRENT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# SQL DDL — §5.2 / §5.3
# ---------------------------------------------------------------------------

CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    turn_id         TEXT NOT NULL,
    parent_event_id TEXT,
    correlation_id  TEXT NOT NULL,
    branch_id       TEXT NOT NULL DEFAULT 'main',
    event_type      TEXT NOT NULL,
    timestamp       REAL NOT NULL,
    payload         TEXT NOT NULL,

    tool_name              TEXT GENERATED ALWAYS AS (json_extract(payload, '$.tool_name')) VIRTUAL,
    verdict                TEXT GENERATED ALWAYS AS (json_extract(payload, '$.verdict')) VIRTUAL,
    skill_id               TEXT GENERATED ALWAYS AS (json_extract(payload, '$.skill_id')) VIRTUAL,
    predecessor_memory_id  TEXT GENERATED ALWAYS AS (json_extract(payload, '$.predecessor_memory_id')) VIRTUAL
)
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_turn           ON events (branch_id, turn_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_correlation    ON events (branch_id, correlation_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_session_recent ON events (branch_id, session_id, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tool           ON events (branch_id, tool_name, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_verdict        ON events (branch_id, verdict, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_parent         ON events (parent_event_id)",
    "CREATE INDEX IF NOT EXISTS idx_predecessor    ON events (predecessor_memory_id)",
]
