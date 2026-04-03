"""
ToolBlock component — tool execution with three visual states.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static
from textual.worker import Worker


class ToolState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


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
    Displays active and recent tool calls.

    States:
    - PENDING: tool called but not yet executing (shows args)
    - RUNNING: executing (shows animated spinner)
    - DONE: completed successfully (shows ok + duration)
    - FAILED: completed with error (shows !! + duration)
    """

    active_tools: reactive[list[ToolCall]] = reactive([])
    completed_tools: reactive[list[ToolCall]] = reactive([])
    verbose: reactive[bool] = reactive(False)

    class ToolDone(Message, bubble=True):
        """All tools for current turn are done."""

        def __init__(self, tools: list[ToolCall]) -> None:
            super().__init__()
            self.tools = tools

    def __init__(self) -> None:
        super().__init__()
        self._spinner_task: Worker | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="tool-content")

    def start_tool(self, name: str, args: dict[str, Any], call_id: str) -> None:
        """Begin tracking a new tool call."""
        tool = ToolCall(name=name, args=args, call_id=call_id, state=ToolState.PENDING)
        self.active_tools = [*self.active_tools, tool]
        self._render()
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
        self._render()

        if not self.active_tools:
            self.post_message(self.ToolDone(self.completed_tools[-len(completed) :]))

    def _start_spinner(self, call_id: str) -> None:
        """Start the spinner animation for a running tool."""
        self._spinner_task = self.run_worker(
            self._spin_loop(call_id), ignore_errors=True
        )

    async def _spin_loop(self, call_id: str) -> None:
        """Animate spinner for running tool."""
        try:
            while True:
                for tool in self.active_tools:
                    if tool.call_id == call_id:
                        tool.advance_spinner()
                        self._render()
                        break
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass

    def _stop_spinner(self) -> None:
        """Stop the spinner animation."""
        if self._spinner_task:
            self._spinner_task.cancel()
            self._spinner_task = None

    def clear(self) -> None:
        """Clear all tool history."""
        self._stop_spinner()
        self.active_tools = []
        self.completed_tools = []
        self._render()

    def toggle_verbose(self) -> None:
        """Toggle verbose mode."""
        self.verbose = not self.verbose
        self._render()

    def _render(self) -> None:
        """Render tool states to the Static widget."""
        content = self.query_one("#tool-content", Static)
        lines: list[str] = []

        # Active tools
        for tool in self.active_tools:
            if tool.state == ToolState.PENDING:
                args_preview = self._format_args(tool.args)
                lines.append(
                    f"  [dim][[/dim][yellow]-[/yellow][dim]][/dim] [yellow]{tool.name}[/yellow]({args_preview})"
                )
            elif tool.state == ToolState.RUNNING:
                lines.append(
                    f"  [dim][[/dim][yellow]{tool.spinner_frame}[/yellow][dim]][/dim] [dim]{tool.name} running...[/dim]"
                )

        # Completed tools (most recent last)
        for tool in self.completed_tools[-5:]:  # show last 5
            icon = (
                "[green]ok[/green]" if tool.state == ToolState.DONE else "[red]!![/red]"
            )
            state = "done" if tool.state == ToolState.DONE else "failed"
            lines.append(
                f"  [dim][[/dim]{icon}[dim]][/dim] [dim]{tool.name}[/dim]  "
                f"[{'green' if tool.state == ToolState.DONE else 'red'}]{tool.duration_ms:.0f}ms[/{'green' if tool.state == ToolState.DONE else 'red'}]  "
                f"[dim]{state}[/dim]"
            )

        content.update("\n".join(lines) if lines else "")

    def _format_args(self, args: dict[str, Any]) -> str:
        """Compact one-line preview of tool arguments."""
        if not args:
            return ""
        parts: list[str] = []
        for k, v in args.items():
            if isinstance(v, str):
                snippet = v[:40].replace("\n", "~")
                parts.append(f'{k}="{snippet}{"..." if len(v) > 40 else ""}"')
            elif isinstance(v, (dict, list)):
                parts.append(f"{k}={{...}}")
            else:
                parts.append(f"{k}={v!r}")
            if len(parts) >= 3:
                break
        return ", ".join(parts)
