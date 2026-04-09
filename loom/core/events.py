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


__all__ = [
    "ActionRolledBack",
    "ActionStateChange",
    "CompressDone",
    "TextChunk",
    "ThinkCollapsed",
    "ToolBegin",
    "ToolEnd",
    "TurnDone",
    "TurnDropped",
    "TurnPaused",
]
