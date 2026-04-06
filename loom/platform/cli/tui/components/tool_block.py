"""
ToolBlock component — tool execution status with agent state awareness.

Agent states (shown above tool list):
  IDLE      — nothing shown (height collapses)
  THINKING  — "◌ Thinking..."  animated dots
  RUNNING   — "⟳ <tool_name> — <primary_arg>"
  DONE      — "✓ Done"  (shown briefly, then clears to IDLE)

Tool rows beneath the agent state line show the current turn's history.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from rich.markup import escape as markup_escape
from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class AgentState(Enum):
    IDLE = "idle"
    THINKING = "thinking"
    RUNNING = "running"
    DONE = "done"


class ToolState(Enum):
    PENDING    = "pending"
    AUTHORIZED = "authorized"    # Issue #42: passed permission check
    PREPARED   = "prepared"      # Issue #42: preconditions verified
    RUNNING    = "running"
    OBSERVED   = "observed"      # Issue #42: executor returned, awaiting validation
    VALIDATED  = "validated"     # Issue #42: post-validator passed
    DONE       = "done"
    REVERTING  = "reverting"     # Issue #42: rollback in progress
    REVERTED   = "reverted"      # Issue #42: rollback completed
    FAILED     = "failed"
    DENIED     = "denied"        # Issue #42: permission denied

    @property
    def icon(self) -> str:
        """Lifecycle state icon for TUI display."""
        return _STATE_ICONS.get(self, "?")

    @property
    def style(self) -> str:
        """Rich markup colour for the state."""
        return _STATE_STYLES.get(self, "dim")


_STATE_ICONS: dict[ToolState, str] = {
    ToolState.PENDING:    "◌",
    ToolState.AUTHORIZED: "🔓",
    ToolState.PREPARED:   "📋",
    ToolState.RUNNING:    "⟳",
    ToolState.OBSERVED:   "👁",
    ToolState.VALIDATED:  "✓",
    ToolState.DONE:       "✓",
    ToolState.REVERTING:  "↩",
    ToolState.REVERTED:   "↩✗",
    ToolState.FAILED:     "✗",
    ToolState.DENIED:     "🚫",
}

_STATE_STYLES: dict[ToolState, str] = {
    ToolState.PENDING:    "dim",
    ToolState.AUTHORIZED: "#c8a464",
    ToolState.PREPARED:   "#6a9fcc",
    ToolState.RUNNING:    "yellow",
    ToolState.OBSERVED:   "dim",
    ToolState.VALIDATED:  "#7a9e78",
    ToolState.DONE:       "#7a9e78",
    ToolState.REVERTING:  "#c8924a",
    ToolState.REVERTED:   "#b87060",
    ToolState.FAILED:     "#b87060",
    ToolState.DENIED:     "#b87060",
}


@dataclass
class ToolCall:
    """A single tool invocation."""

    name: str
    args: dict[str, Any]
    call_id: str
    state: ToolState = ToolState.PENDING
    duration_ms: float = 0.0
    output: str = ""
    _spinner_frame: int = 0

    @property
    def spinner_frame(self) -> str:
        frames = ["-", "\\", "|", "/"]
        return frames[self._spinner_frame % len(frames)]

    def advance_spinner(self) -> None:
        self._spinner_frame = (self._spinner_frame + 1) % 4


class ToolBlock(Widget):
    """
    Displays overall agent activity status and active/recent tool calls.

    Layout (when active):
      ◌ Thinking...               ← agent state line
      ✓ read_file  45ms           ← completed tools (last 5)
      ⟳ run_bash running...       ← active tools with spinner
    """

    active_tools: reactive[list[ToolCall]] = reactive([])
    completed_tools: reactive[list[ToolCall]] = reactive([])
    verbose: reactive[bool] = reactive(False)
    agent_state: reactive[AgentState] = reactive(AgentState.IDLE)

    class ToolDone(Message, bubble=True):
        """All tools for current turn are done."""

        def __init__(self, tools: list[ToolCall]) -> None:
            super().__init__()
            self.tools = tools

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._spinner_task: asyncio.Task | None = None
        self._think_task: asyncio.Task | None = None
        self._done_task: asyncio.Task | None = None
        self._think_frame: int = 0
        self._active_tool_label: str = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="tool-content")

    # ── Agent lifecycle ────────────────────────────────────────────────────────

    def start_turn(self) -> None:
        """Call when TurnStart fires — enter THINKING state."""
        self._cancel_done_task()
        self.agent_state = AgentState.THINKING
        self._start_think_animation()
        self._update_display()

    def end_turn(self) -> None:
        """Call when TurnDone fires — show DONE briefly then go IDLE."""
        self._stop_think_animation()
        self._stop_spinner()
        self.agent_state = AgentState.DONE
        self._update_display()
        self._done_task = asyncio.create_task(self._clear_done_after_delay())

    # ── Tool lifecycle ─────────────────────────────────────────────────────────

    def start_tool(self, name: str, args: dict[str, Any], call_id: str) -> None:
        """Begin tracking a new tool call — enter RUNNING state."""
        self._stop_think_animation()
        self._cancel_done_task()

        # Build a compact label: "tool_name — primary_arg"
        primary = ""
        if args:
            first_val = next(iter(args.values()), "")
            if isinstance(first_val, str):
                primary = first_val[:80].replace("\n", "↵")
        self._active_tool_label = (
            f"{markup_escape(name)} — \"{markup_escape(primary)}\""
            if primary
            else markup_escape(name)
        )

        self.agent_state = AgentState.RUNNING
        tool = ToolCall(name=name, args=args, call_id=call_id, state=ToolState.PENDING)
        self.active_tools = [*self.active_tools, tool]
        self._update_display()
        self._start_spinner(call_id)

    def complete_tool(
        self, call_id: str, success: bool, output: str, duration_ms: float
    ) -> None:
        """Mark a tool call as complete."""
        for tool in self.active_tools:
            if tool.call_id == call_id:
                tool.state = ToolState.DONE if success else ToolState.FAILED
                tool.output = output[:200]
                tool.duration_ms = duration_ms
                break
        self._stop_spinner()
        completed = [t for t in self.active_tools if t.call_id == call_id]
        self.completed_tools = [*self.completed_tools, *completed]
        self.active_tools = [t for t in self.active_tools if t.call_id != call_id]

        if not self.active_tools:
            # All tools done — back to THINKING (LLM will process results)
            self.agent_state = AgentState.THINKING
            self._start_think_animation()
            self.post_message(self.ToolDone(self.completed_tools[-len(completed):]))

        self._update_display()

    def update_tool_lifecycle(
        self, call_id: str, old_state: str, new_state: str, reason: str | None = None
    ) -> None:
        """
        Update a tool's displayed state from a lifecycle state change event (Issue #42).

        Maps ActionState values to ToolState enum members for visualization.
        """
        _MAP = {
            "authorized": ToolState.AUTHORIZED,
            "prepared":   ToolState.PREPARED,
            "executing":  ToolState.RUNNING,
            "observed":   ToolState.OBSERVED,
            "validated":  ToolState.VALIDATED,
            "committed":  ToolState.DONE,
            "reverting":  ToolState.REVERTING,
            "reverted":   ToolState.REVERTED,
            "denied":     ToolState.DENIED,
            "aborted":    ToolState.FAILED,
            "timed_out":  ToolState.FAILED,
        }
        ts = _MAP.get(new_state)
        if ts is None:
            return
        for tool in self.active_tools:
            if tool.call_id == call_id:
                tool.state = ts
                break
        else:
            # Tool might already be in completed list
            for tool in self.completed_tools:
                if tool.call_id == call_id:
                    tool.state = ts
                    break
        self._update_display()

    def clear(self) -> None:
        """Clear all tool history (called on TurnDone after obs panel update)."""
        self._stop_spinner()
        self._stop_think_animation()
        self._cancel_done_task()
        self.active_tools = []
        self.completed_tools = []
        self._update_display()

    def toggle_verbose(self) -> None:
        self.verbose = not self.verbose
        self._update_display()

    # ── Animations ────────────────────────────────────────────────────────────

    def _start_think_animation(self) -> None:
        self._stop_think_animation()
        self._think_frame = 0
        self._think_task = asyncio.create_task(self._think_loop())

    def _stop_think_animation(self) -> None:
        if self._think_task:
            self._think_task.cancel()
            self._think_task = None

    async def _think_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.4)
                self._think_frame = (self._think_frame + 1) % 4
                self._update_display()
        except asyncio.CancelledError:
            pass

    def _start_spinner(self, call_id: str) -> None:
        self._spinner_task = asyncio.create_task(self._spin_loop(call_id))

    async def _spin_loop(self, call_id: str) -> None:
        try:
            while True:
                for tool in self.active_tools:
                    if tool.call_id == call_id:
                        tool.advance_spinner()
                        self._update_display()
                        break
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass

    def _stop_spinner(self) -> None:
        if self._spinner_task:
            self._spinner_task.cancel()
            self._spinner_task = None

    def _cancel_done_task(self) -> None:
        if self._done_task:
            self._done_task.cancel()
            self._done_task = None

    async def _clear_done_after_delay(self) -> None:
        try:
            await asyncio.sleep(1.5)
            self.agent_state = AgentState.IDLE
            self._update_display()
        except asyncio.CancelledError:
            pass

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _update_display(self) -> None:
        from textual.css.query import NoMatches

        try:
            content = self.query_one("#tool-content", Static)
        except NoMatches:
            return

        # Single-line agent state indicator only.
        # Tool history is rendered inline in the message bubble.
        state = self.agent_state
        if state == AgentState.THINKING:
            dots = "." * self._think_frame
            line = f"  [dim]◌ Thinking{dots}[/dim]"
        elif state == AgentState.RUNNING:
            line = f"  [#c8a464]⟳[/#c8a464] [dim]{self._active_tool_label}[/dim]"
        elif state == AgentState.DONE:
            line = "  [#7a9e78]✓ Done[/#7a9e78]"
        else:
            line = ""  # IDLE → empty

        content.update(line)

    def _format_args(self, args: dict[str, Any]) -> str:
        if not args:
            return ""
        parts: list[str] = []
        for k, v in args.items():
            if isinstance(v, str):
                snippet = markup_escape(v[:40].replace("\n", "~"))
                parts.append(f'{k}="{snippet}{"..." if len(v) > 40 else ""}"')
            elif isinstance(v, (dict, list)):
                parts.append(f"{k}={{...}}")
            else:
                parts.append(f"{k}={v!r}")
            if len(parts) >= 3:
                break
        return ", ".join(parts)
