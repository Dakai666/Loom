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
class TurnDone(StreamEvent):
    """Full turn complete (all tools done)."""

    tool_count: int
    input_tokens: int
    output_tokens: int
    elapsed_ms: float
    context_pct: float


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
    """Token budget changed."""

    fraction: float
    input_tokens: int
    output_tokens: int


@dataclass
class SetPersonality(StreamEvent):
    """Personality changed."""

    name: str | None


@dataclass
class ErrorOccurred(StreamEvent):
    """An error occurred during streaming."""

    message: str
