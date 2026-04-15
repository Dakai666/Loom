"""Session stream event types.

``LoomSession.stream_turn()`` is an async generator that yields a sequence of
these typed events.  All platform consumers (CLI, TUI, Discord) branch on the
event type to drive their respective UIs.

Keeping event types in ``loom.core`` lets every platform import them without
depending on ``loom.platform.cli``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TextChunk:
    """A fragment of streaming LLM text."""

    text: str


@dataclass
class ToolBegin:
    """The agent is about to call a tool."""

    name: str
    args: dict[str, Any]
    call_id: str


@dataclass
class ToolEnd:
    """A tool call finished."""

    name: str
    success: bool
    output: str
    duration_ms: float
    call_id: str


@dataclass
class TurnPaused:
    """
    The agent turn has been paused — all current tool calls are done but the
    loop is suspended, waiting for human input before continuing.

    The consumer should prompt the user and then call either:
        session.resume()         — continue the turn as-is
        session.resume_with(msg) — inject a message and continue
        session.cancel()         — abandon the rest of this turn
    """
    tool_count_so_far: int = 0


@dataclass
class CompressDone:
    """Episodic memory was compressed to semantic facts mid-session."""
    fact_count: int


@dataclass
class TurnDone:
    """The complete agent turn (including all tool loops) is done."""

    tool_count: int
    input_tokens: int
    output_tokens: int
    elapsed_ms: float
    stop_reason: str = "complete"  # "complete" | "cancelled"


@dataclass
class TurnDropped:
    """The agent turn was dropped mid-stream due to an unexpected stop.

    This happens when:
    - The LLM API returns ``stop_reason`` that is neither ``end_turn`` nor
      ``tool_use`` (e.g. ``max_tokens``, provider-specific error codes).
    - The streaming response object is ``None`` (connection dropped before
      any final message arrived).

    ``stop_reason`` — the raw stop_reason string from the provider, or
                      ``"stream_none"`` when response was None.
    ``retry_count``  — how many automatic retries have already been attempted.
    ``tool_count``   — number of tools called before the drop.
    """

    stop_reason: str
    retry_count: int = 0
    tool_count: int = 0
    exhausted: bool = False


@dataclass
class ActionStateChange:
    """An action transitioned to a new lifecycle state (Issue #42)."""
    action_id: str
    tool_name: str
    call_id: str
    old_state: str
    new_state: str
    reason: str | None = None


@dataclass
class ActionRolledBack:
    """An action was rolled back after post-validation failure (Issue #42)."""
    action_id: str
    tool_name: str
    call_id: str
    rollback_success: bool
    message: str = ""


@dataclass
class ThinkCollapsed:
    """A <think>…</think> block closed during streaming.

    Replaces the old ``TextChunk("▸ thinking…\\n")`` placeholder so each
    platform can render reasoning content in its own style.

    ``summary`` — first ~120 chars of the reasoning block, single line.
    ``full``    — complete think content for the detail view.
    """

    summary: str
    full: str


# ---------------------------------------------------------------------------
# Issue #106: ExecutionEnvelope ViewModel & stream events
# ---------------------------------------------------------------------------

@dataclass
class ExecutionNodeView:
    """Single action node view for UI consumption.

    Maps 1:1 to an ``ActionRecord`` but carries only the fields the
    presentation layer needs — no mutable state, no references to
    middleware internals.
    """
    node_id: str           # ActionRecord.id
    call_id: str           # ToolCall.id
    action_id: str | None  # same as node_id (for API clarity)
    tool_name: str
    level: int             # parallel level (0 = all current dispatch)
    state: str             # ActionState.value
    trust_level: str       # SAFE / GUARDED / CRITICAL
    capabilities: list[str] = field(default_factory=list)
    args_preview: str = ""
    duration_ms: float = 0.0
    error_snippet: str = ""
    depends_on: list[str] = field(default_factory=list)
    # ── Detail fields (Issue #108) ──────────────────────────────────
    full_args: dict[str, Any] = field(default_factory=dict)
    state_history: list[dict[str, Any]] = field(default_factory=list)
    auth_decision: str = ""      # "once" / "scope" / "auto" / "deny" / ""
    auth_expires: float = 0.0    # time.time() expiry; 0 = permanent/N/A
    auth_selector: str = ""      # scope selector (e.g. "/workspace/doc/")
    output_preview: str = ""     # first ~200 chars of tool output


@dataclass
class ExecutionEnvelopeView:
    """Aggregate view for one tool-use batch — the primary UI unit.

    Built by ``LoomSession._build_envelope_view()`` (projection layer)
    and yielded as part of ``EnvelopeStarted / Updated / Completed``
    stream events.  TUI and Discord both consume this same structure.
    """
    envelope_id: str       # human-readable, e.g. "e1", "e2"
    session_id: str
    turn_index: int
    status: str            # "running" / "completed" / "failed"
    node_count: int
    parallel_groups: int   # number of distinct levels
    elapsed_ms: float = 0.0
    levels: list[list[str]] = field(default_factory=list)
    nodes: list[ExecutionNodeView] = field(default_factory=list)


@dataclass
class EnvelopeStarted:
    """A new tool-use batch (envelope) has been created and dispatch begins."""
    envelope: ExecutionEnvelopeView


@dataclass
class EnvelopeUpdated:
    """A node inside the current envelope changed state (e.g. tool finished)."""
    envelope: ExecutionEnvelopeView


@dataclass
class EnvelopeCompleted:
    """All nodes in the envelope have reached terminal states."""
    envelope: ExecutionEnvelopeView


@dataclass
class GrantSummary:
    """One active scope grant — lightweight UI projection (#112)."""
    grant_id: str          # unique identifier for tracking expiry transitions
    tool_name: str         # e.g. "write_file"
    selector: str          # e.g. "/workspace/doc/"
    source: str            # "lease" / "auto" / "manual_confirm"
    expires_at: float      # absolute time.time(); 0 = permanent


@dataclass
class GrantsSnapshot:
    """Current state of active scope grants for UI display (#108, #112)."""
    active_count: int
    next_expiry_secs: float = 0.0  # seconds until nearest expiry; 0 = none
    grants: list[GrantSummary] = field(default_factory=list)


__all__ = [
    "ActionRolledBack",
    "ActionStateChange",
    "CompressDone",
    "EnvelopeCompleted",
    "EnvelopeStarted",
    "EnvelopeUpdated",
    "ExecutionEnvelopeView",
    "ExecutionNodeView",
    "GrantSummary",
    "GrantsSnapshot",
    "TextChunk",
    "ThinkCollapsed",
    "ToolBegin",
    "ToolEnd",
    "TurnDone",
    "TurnDropped",
    "TurnPaused",
]
