"""
Internal TUI messages (Elm-style events).

These are distinct from ui.py's event types (TextChunk, ToolBegin, etc.)
These are Textual messages that components use to communicate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StreamEvent:
    """Base for any stream event from LoomSession."""

    pass


@dataclass
class TurnStart(StreamEvent):
    """A new agent turn is beginning."""

    user_input: str
    context_pct: float = 0.0


@dataclass
class TextChunk(StreamEvent):
    """Partial LLM text streamed in."""

    text: str


@dataclass
class ToolBegin(StreamEvent):
    """Tool call started."""

    name: str
    args: dict[str, Any]
    call_id: str


@dataclass
class ToolEnd(StreamEvent):
    """Tool call finished."""

    name: str
    success: bool
    output: str
    duration_ms: float
    call_id: str


@dataclass
class TurnPaused(StreamEvent):
    """Agent loop suspended at a tool boundary — waiting for human input."""

    tool_count_so_far: int = 0


@dataclass
class TurnDone(StreamEvent):
    """Full turn complete (all tools done)."""

    tool_count: int
    input_tokens: int
    output_tokens: int
    elapsed_ms: float
    context_pct: float
    used_tokens: int = 0    # absolute tokens used this turn
    max_tokens: int = 0     # model context window size
    think_text: str = ""    # full <think>…</think> content, if any


@dataclass
class ClearScreen(StreamEvent):
    """Request to clear the terminal."""

    pass


@dataclass
class ToggleVerbose(StreamEvent):
    """Toggle tool output verbosity."""

    pass


@dataclass
class BudgetUpdate(StreamEvent):
    """Token budget changed (mid-turn update)."""

    fraction: float
    input_tokens: int
    output_tokens: int
    used_tokens: int = 0
    max_tokens: int = 0


@dataclass
class SetPersonality(StreamEvent):
    """Personality changed."""

    name: str | None


@dataclass
class ErrorOccurred(StreamEvent):
    """An error occurred during streaming."""

    message: str


@dataclass
class ActionStateChange(StreamEvent):
    """An action transitioned to a new lifecycle state (Issue #42)."""
    action_id: str
    tool_name: str
    call_id: str
    old_state: str
    new_state: str
    reason: str | None = None


@dataclass
class ActionRolledBack(StreamEvent):
    """An action was rolled back after post-validation failure (Issue #42)."""
    action_id: str
    tool_name: str
    call_id: str
    rollback_success: bool
    message: str = ""
