"""
Action Lifecycle — state machine for tool-call lifecycle management.

Upgrades the binary ToolCall/ToolResult paradigm into a comprehensive
state machine (ActionRecord / ExecutionEnvelope) that tracks the entire
lifecycle of an action:

    Declared → Authorized → Prepared → Executing → Observed →
    Validated → Committed | Reverting → Reverted → Memorialized

Terminal failure states: Denied, Aborted, TimedOut.

Design: ActionRecord *wraps* ToolCall/ToolResult (composition, not
inheritance), so the existing API is fully backward-compatible.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)
from datetime import datetime, UTC
from enum import Enum
from typing import Any

from .middleware import ToolCall, ToolResult


# ---------------------------------------------------------------------------
# Lifecycle states
# ---------------------------------------------------------------------------

class ActionState(Enum):
    """
    State machine states for an action's lifecycle.

    Happy path:
        DECLARED → AUTHORIZED → PREPARED → EXECUTING → OBSERVED →
        VALIDATED → COMMITTED → MEMORIALIZED

    Rollback path:
        VALIDATED (fail) → REVERTING → REVERTED → MEMORIALIZED

    Terminal failure states (bypass normal flow):
        DENIED      — permission denied by user or policy
        ABORTED     — precondition failed or abort signal fired
        TIMED_OUT   — execution exceeded time limit
    """
    # --- Normal lifecycle ---
    DECLARED      = "declared"       # ToolCall created from LLM output
    AUTHORIZED    = "authorized"     # Passed blast-radius / permission check
    PREPARED      = "prepared"       # Preconditions verified
    EXECUTING     = "executing"      # Tool executor invoked (in flight)
    OBSERVED      = "observed"       # Executor returned; raw result captured
    VALIDATED     = "validated"      # Post-execution validator passed
    COMMITTED     = "committed"      # Effect persisted, accepted as final
    REVERTING     = "reverting"      # Rollback in progress
    REVERTED      = "reverted"       # Rollback completed
    MEMORIALIZED  = "memorialized"   # Trace written to episodic / audit

    # --- Terminal failure states ---
    DENIED        = "denied"         # User or policy denied execution
    ABORTED       = "aborted"        # Precondition failed / abort signal
    TIMED_OUT     = "timed_out"      # Execution timed out

    @property
    def is_terminal(self) -> bool:
        """Return True if this state is final (no further transitions)."""
        return self in _TERMINAL_STATES

    @property
    def is_failure(self) -> bool:
        """Return True if this state represents a failure outcome."""
        return self in _FAILURE_STATES


_TERMINAL_STATES = {
    ActionState.MEMORIALIZED,
    ActionState.DENIED,
    ActionState.ABORTED,
    ActionState.TIMED_OUT,
}

_FAILURE_STATES = {
    ActionState.DENIED,
    ActionState.ABORTED,
    ActionState.TIMED_OUT,
    ActionState.REVERTED,
}

# Valid state transitions — key can transition to any value in the set.
_VALID_TRANSITIONS: dict[ActionState, set[ActionState]] = {
    ActionState.DECLARED:     {ActionState.AUTHORIZED, ActionState.DENIED},
    ActionState.AUTHORIZED:   {ActionState.PREPARED, ActionState.ABORTED},
    ActionState.PREPARED:     {ActionState.EXECUTING, ActionState.ABORTED},
    ActionState.EXECUTING:    {ActionState.OBSERVED, ActionState.TIMED_OUT, ActionState.ABORTED},
    ActionState.OBSERVED:     {ActionState.VALIDATED, ActionState.COMMITTED, ActionState.MEMORIALIZED},
    ActionState.VALIDATED:    {ActionState.COMMITTED, ActionState.REVERTING},
    ActionState.COMMITTED:    {ActionState.MEMORIALIZED},
    ActionState.REVERTING:    {ActionState.REVERTED},
    ActionState.REVERTED:     {ActionState.MEMORIALIZED},
    # Terminal states have no outgoing transitions
    ActionState.MEMORIALIZED: set(),
    ActionState.DENIED:       {ActionState.MEMORIALIZED},
    ActionState.ABORTED:      {ActionState.MEMORIALIZED},
    ActionState.TIMED_OUT:    {ActionState.MEMORIALIZED},
}


# ---------------------------------------------------------------------------
# Intent description
# ---------------------------------------------------------------------------

@dataclass
class ActionIntent:
    """
    Declarative description of what an action intends to do.

    Populated from tool metadata and/or LLM-provided context.
    Used for audit, observability, and post-execution validation.
    """
    intent_summary: str = ""
    """Natural-language summary (e.g. "Write test results to output.json")."""

    scope: str = "general"
    """Impact scope classification: filesystem, network, memory, shell, general."""

    expected_effect: str | None = None
    """Expected observable effect (e.g. "file output.json contains JSON data")."""

    preconditions: list[str] = field(default_factory=list)
    """List of precondition descriptions from the ToolDefinition."""


# ---------------------------------------------------------------------------
# LifecycleContext — shared state for real-time lifecycle coordination
# ---------------------------------------------------------------------------

@dataclass
class LifecycleContext:
    """
    Shared context injected into ``ToolCall.metadata["_lifecycle_ctx"]``.

    Purpose: enable **two** middleware layers plus downstream middleware
    (BlastRadiusMiddleware) to coordinate lifecycle state transitions in
    real time through a single shared object.

    Participants:
        LifecycleMiddleware (outer)
            Creates this context, attaches it to the call.
            After the inner pipeline returns, drives post-OBSERVED states
            (VALIDATED, COMMITTED, REVERTING, REVERTED, MEMORIALIZED).

        LifecycleGateMiddleware (inner, just before handler)
            Reads authorization_result from BlastRadius.
            Fires AUTHORIZED → PREPARED → EXECUTING → OBSERVED as real
            control gates.

        BlastRadiusMiddleware
            Writes authorization_result / reason at the moment the
            auth decision is made.

    The ``transition()`` and ``memorialize()`` helpers centralize all
    state-change bookkeeping so every call site is a single line.
    """
    record: ActionRecord = field(repr=False)
    """The ActionRecord being tracked (reference, not copy)."""

    authorization_result: bool | None = None
    """
    Set by BlastRadiusMiddleware:
      True  → tool was authorized (pre-auth, exec_auto, or user confirmed)
      False → user denied execution
      None  → not yet decided (default)
    """

    authorization_reason: str | None = None
    """Human-readable reason for the authorization decision."""

    # --- Internal callbacks (set by LifecycleMiddleware, used by all) ---
    _on_state_change: Any = field(default=None, repr=False)
    """Callable[[ActionRecord, str, str], Awaitable[None]] | None"""

    _on_lifecycle: Any = field(default=None, repr=False)
    """Callable[[ActionRecord], Awaitable[None]] | None"""

    async def transition(
        self, new_state: ActionState, reason: str | None = None,
    ) -> None:
        """
        Transition the record to *new_state* and fire the UI callback.

        One-liner replacement for the old three-line pattern of
        saving old state, calling record.transition(), and firing
        the state change callback.
        """
        old = self.record.state.value
        self.record.transition(new_state, reason=reason)
        if self._on_state_change is not None:
            try:
                await self._on_state_change(self.record, old, self.record.state.value)
            except Exception as exc:
                _log.warning(
                    "on_state_change callback raised (suppressed): %s", exc, exc_info=True,
                )

    async def memorialize(self, reason: str) -> None:
        """Transition to MEMORIALIZED and fire the on_lifecycle callback."""
        await self.transition(ActionState.MEMORIALIZED, reason=reason)
        if self._on_lifecycle is not None:
            try:
                await self._on_lifecycle(self.record)
            except Exception as exc:
                _log.warning(
                    "on_lifecycle callback raised (suppressed): %s", exc, exc_info=True,
                )


# Metadata key for LifecycleContext in ToolCall.metadata
LIFECYCLE_CTX_KEY = "_lifecycle_ctx"


# ---------------------------------------------------------------------------
# ActionRecord — single tool call lifecycle
# ---------------------------------------------------------------------------

@dataclass
class StateTransition:
    """One state transition in the lifecycle history."""
    from_state: ActionState
    to_state: ActionState
    timestamp: datetime
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.from_state.value,
            "to": self.to_state.value,
            "ts": self.timestamp.isoformat(),
            "reason": self.reason,
        }


@dataclass
class ActionRecord:
    """
    Complete lifecycle record for a single tool invocation.

    Wraps ToolCall and ToolResult via composition — the underlying
    harness types are preserved unchanged for backward compatibility.
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: ActionState = ActionState.DECLARED
    call: ToolCall | None = None
    result: ToolResult | None = None
    intent: ActionIntent = field(default_factory=ActionIntent)
    state_history: list[StateTransition] = field(default_factory=list)
    observed_effect: str | None = None
    rollback_result: ToolResult | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def transition(self, new_state: ActionState, reason: str | None = None) -> None:
        """
        Transition to a new state, recording the change in history.

        Raises ValueError if the transition is not valid according to the
        state machine rules.
        """
        valid = _VALID_TRANSITIONS.get(self.state, set())
        if new_state not in valid:
            raise ValueError(
                f"Invalid transition: {self.state.value} → {new_state.value}. "
                f"Valid targets: {', '.join(s.value for s in valid) or '(none)'}"
            )
        now = datetime.now(UTC)
        self.state_history.append(
            StateTransition(
                from_state=self.state,
                to_state=new_state,
                timestamp=now,
                reason=reason,
            )
        )
        self.state = new_state

    @property
    def is_terminal(self) -> bool:
        """True if the action has reached a final state."""
        return self.state.is_terminal

    @property
    def is_failure(self) -> bool:
        """True if the action ended in a failure state."""
        return self.state.is_failure

    @property
    def elapsed_ms(self) -> float:
        """
        Wall-clock ms from DECLARED to the current (or last) state.

        If the action is still in progress, returns elapsed so far.
        """
        if not self.state_history:
            return 0.0
        last_ts = self.state_history[-1].timestamp
        return (last_ts - self.created_at).total_seconds() * 1000.0

    @property
    def tool_name(self) -> str:
        """Convenience accessor for the underlying tool name."""
        return self.call.tool_name if self.call else "(unknown)"

    @property
    def final_state(self) -> str:
        """String value of the current state — for DB storage."""
        return self.state.value

    def summary(self) -> str:
        """One-line human-readable summary of this action."""
        status = self.state.value
        dur = f"{self.elapsed_ms:.0f}ms" if self.elapsed_ms > 0 else "—"
        return f"{self.tool_name}: {status} ({dur})"

    def history_dicts(self) -> list[dict[str, Any]]:
        """Serialize state_history for JSON storage."""
        return [t.to_dict() for t in self.state_history]


# ---------------------------------------------------------------------------
# ExecutionEnvelope — batch of related ActionRecords
# ---------------------------------------------------------------------------

@dataclass
class ExecutionEnvelope:
    """
    Groups related ActionRecords from a single tool-use batch.

    One LLM response may request multiple parallel tool calls — these are
    wrapped in a single envelope for tracking and observability.
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    turn_index: int = 0
    records: list[ActionRecord] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    def add(self, record: ActionRecord) -> None:
        """Add an ActionRecord to this envelope."""
        self.records.append(record)

    def complete(self) -> None:
        """Mark the envelope as completed."""
        self.completed_at = datetime.now(UTC)

    @property
    def all_terminal(self) -> bool:
        """True if every record in the envelope has reached a terminal state."""
        return all(r.is_terminal for r in self.records) if self.records else False

    def summary(self) -> str:
        """One-line summary of all records in this envelope."""
        if not self.records:
            return "empty envelope"
        # Count by final state
        counts: dict[str, int] = {}
        for r in self.records:
            key = r.state.value
            counts[key] = counts.get(key, 0) + 1
        parts = [f"{v} {k}" for k, v in counts.items()]
        n = len(self.records)
        return f"{n} action{'s' if n != 1 else ''}: {', '.join(parts)}"
