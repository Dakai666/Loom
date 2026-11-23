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


UNKNOWN_ORIGIN = "unknown"
"""Origin of a row written before #690 S3b, or by a caller that did not say."""


def origin_of(payload: dict[str, Any]) -> str:
    """The origin a turn_start or permission_decision row records.

    Rows from before #690 S3b carry no ``origin`` key; they read as
    ``"unknown"``, never as a guessed value.
    """
    return payload.get("origin") or UNKNOWN_ORIGIN


@dataclass(kw_only=True)
class TurnStartPayload:
    """The event timestamp is the handling start: the turn emits this once
    it holds the session's turn lock and begins work."""

    prompt_stack_hash: str
    prompt_stack_components: dict[str, Any]
    full_text: str | None = None
    # Set by the runtime entry point, never parsed from turn text (#690 S3b).
    # "discord" / "cli" / "chime" / "autonomy" / ... — see stream_turn.
    origin: str = UNKNOWN_ORIGIN
    # UTC epoch of the triggering event: the Discord message, the CLI
    # submit, the trigger fire. None when the entry point does not know it.
    fired_at: float | None = None
    schema_version: int = CURRENT_SCHEMA_VERSION


@dataclass(kw_only=True)
class TurnEndPayload:
    outcome: str  # "clean" / "retry" / "abandoned" / "error"
    duration_ms: int
    token_usage: dict[str, int]
    # Session total of ledger writes that failed and whose ledger_gap event
    # also failed (#690 S3b). Cumulative, so a lost turn_end loses nothing.
    unrecorded_ledger_gaps: int = 0
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
    # END only (#690 S3b): how the call ended. The memorialize reason
    # ("committed", "denied", "timed_out", "reverted", ...) or, when
    # process() never reached memorialize, "cancelled" / "exception".
    # Rows from before S3b have no outcome; read their state_history.
    outcome: str | None = None
    schema_version: int = CURRENT_SCHEMA_VERSION


@dataclass(kw_only=True)
class PermissionDecisionPayload:
    # "grant" / "deny" / "scope", or "skip" (#690 S7): an unattended call
    # held back because no grant covered it and nobody was there to ask.
    # Its reason reads "unattended-skip (...)". Rows before S7 wrote such
    # skips as "deny"; rows before S3c have an "unattended-deny" reason.
    decision: str
    tool_call_id: str
    trust_level: str
    scope_grant: dict | None = None
    reason: str | None = None
    # The turn origin of the call (#690 S3b), so a grant confirmed by a
    # human in an attended turn reads apart from a decision in an
    # unattended one. Rows from before S3b read as unknown (origin_of).
    origin: str = UNKNOWN_ORIGIN
    schema_version: int = CURRENT_SCHEMA_VERSION


@dataclass(kw_only=True)
class LedgerGapPayload:
    """A ledger write that failed (#690 S3b).

    Records only what is known about the missing event; it never stands in
    for the event itself.
    """

    missing_event_type: str
    missing_event_id: str
    error: str
    tool_name: str | None = None
    tool_call_id: str | None = None
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


@dataclass(kw_only=True)
class TriggerFirePayload:
    """One fire of a cron schedule, rhythm anchor or memory loop (#690 S5a).

    Times are UTC epochs. ``started_at`` / ``ended_at`` are None when the
    fire never ran (``outcome="missed"``). ``reason`` explains missed,
    skipped and failed outcomes. An ``evaluator_gap`` miss covers a span:
    ``fired_at`` is its first due minute, ``missed_through`` its last, and
    ``missed_count`` how many due minutes it holds.
    """

    source: str          # cron | event | condition | memory_loop
    name: str            # trigger name, or the memory loop's label
    fired_at: float
    started_at: float | None
    ended_at: float | None
    outcome: str         # loom.autonomy.fire_record.FireOutcome
    reason: str | None = None
    missed_through: float | None = None
    missed_count: int | None = None
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
    "CREATE INDEX IF NOT EXISTS idx_skill          ON events (branch_id, skill_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_parent         ON events (parent_event_id)",
    "CREATE INDEX IF NOT EXISTS idx_predecessor    ON events (predecessor_memory_id)",
]
