"""
LoomApp — main Textual application.

Wires LoomSession.stream_turn() events to Textual components.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget

from .components import (
    Header,
    MessageList,
    ToolBlock,
    StatusBar,
    InputArea,
    ObservabilityPanel,
    WorkspacePanel,
    WorkspaceTab,
    ArtifactState,
)
from .components.message_list import Role
from .events import (
    StreamEvent,
    TurnStart,
    TextChunk,
    ToolBegin,
    ToolEnd,
    TurnDone,
    ClearScreen,
    ToggleVerbose,
    BudgetUpdate,
)

if TYPE_CHECKING:
    from loom.core.memory.store import SQLiteStore


class LoomApp(App):
    """
    Main Loom TUI application.

    Layout:
        Header          (top dock, 3 rows)
        Horizontal body (fills remaining height)
          Vertical conversation-pane (60%)
            MessageList   (grows to fill)
            ToolBlock     (auto height, max 5)
            InputArea     (bottom dock, 3 rows)
          WorkspacePanel  (40%)
        ObservabilityPanel (bottom dock, hidden by default)
        StatusBar          (bottom dock, 1 row)

    Bindings:
        Ctrl+L: clear screen
        Ctrl+O: toggle verbose
        Ctrl+W: toggle workspace tab
    """

    CSS = """
    Screen {
        background: $surface;
    }

    #header-bar {
        dock: top;
        height: 3;
        background: $surface;
        border-bottom: solid $border;
    }

    #body {
        height: 1fr;
    }

    #conversation-pane {
        width: 60%;
        border-right: solid $border;
    }

    #message-list {
        height: 1fr;
    }

    #tool-block {
        height: auto;
        max-height: 5;
        overflow-y: auto;
    }

    #input-area {
        height: 4;
        background: $surface;
    }

    #workspace-panel {
        width: 40%;
    }

    #obs-panel {
        dock: bottom;
        height: 1;
        background: $surface;
        border-top: solid $border;
        display: none;
    }

    #obs-panel.visible {
        display: block;
        height: 2;
    }

    #status-bar {
        dock: bottom;
        height: 1;
        background: $surface;
        border-top: solid $border;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=True, priority=True),
        Binding("ctrl+l", "clear_screen", "Clear", show=True),
        Binding("ctrl+o", "toggle_verbose", "Verbose", show=True),
        Binding("ctrl+w", "toggle_space", "Space", show=True),
    ]

    def __init__(
        self,
        model: str = "",
        db_path: str = "",
        verbose: bool = False,
    ) -> None:
        super().__init__()
        self._model = model
        self._db_path = db_path
        self._verbose = verbose

    def compose(self) -> ComposeResult:
        yield Header(id="header-bar", model=self._model, db_path=self._db_path)
        with Horizontal(id="body"):
            with Vertical(id="conversation-pane"):
                yield MessageList(id="message-list")
                yield ToolBlock(id="tool-block")
                yield InputArea(id="input-area")
            yield WorkspacePanel(id="workspace-panel")
        yield ObservabilityPanel(id="obs-panel")
        yield StatusBar(id="status-bar")

    def on_mount(self) -> None:
        """InputArea.on_mount() focuses the inner Input widget directly."""

    # ── Actions (called by bindings) ─────────────────────────────────────────

    def action_clear_screen(self) -> None:
        """Clear the message list (not the terminal)."""
        msg_list = self.query_one("#message-list", MessageList)
        msg_list.clear()
        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block.clear()
        self.notify("Screen cleared")

    def action_toggle_space(self) -> None:
        """Toggle between Artifacts and Knowledge Graph in workspace."""
        workspace = self.query_one("#workspace-panel", WorkspacePanel)
        workspace.toggle_tab()
        current = workspace.active_tab
        label = "Knowledge" if current == WorkspaceTab.KNOWLEDGE else "Artifacts"
        self.notify(f"Workspace: {label}")

    def action_toggle_verbose(self) -> None:
        """Toggle tool output verbosity."""
        self._verbose = not self._verbose
        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block.verbose = self._verbose
        state = "verbose" if self._verbose else "compact"
        self.notify(f"Tool output: {state}")

    # ── Event dispatch from LoomSession ───────────────────────────────────────

    async def dispatch_stream_event(self, event: StreamEvent) -> None:
        """
        Dispatch a stream event from LoomSession to the appropriate component.

        Bridge between the async LoomSession.stream_turn() generator
        and the Textual widget tree.
        """
        if isinstance(event, TurnStart):
            await self._on_turn_start(event)

        elif isinstance(event, TextChunk):
            self._on_text_chunk(event)

        elif isinstance(event, ToolBegin):
            self._on_tool_begin(event)

        elif isinstance(event, ToolEnd):
            self._on_tool_end(event)

        elif isinstance(event, TurnDone):
            await self._on_turn_done(event)

        elif isinstance(event, BudgetUpdate):
            self._on_budget_update(event)

    async def _on_turn_start(self, event: TurnStart) -> None:
        msg_list = self.query_one("#message-list", MessageList)
        msg_list.add_message(Role.USER, event.user_input)
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.update(fraction=event.context_pct)

    def _on_text_chunk(self, event: TextChunk) -> None:
        msg_list = self.query_one("#message-list", MessageList)
        msg_list.stream_text(event.text)

    def _on_tool_begin(self, event: ToolBegin) -> None:
        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block.start_tool(event.name, event.args, event.call_id)

    def _on_tool_end(self, event: ToolEnd) -> None:
        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block.complete_tool(
            event.call_id, event.success, event.output, event.duration_ms
        )

    async def _on_turn_done(self, event: TurnDone) -> None:
        msg_list = self.query_one("#message-list", MessageList)
        msg_list.finish_streaming()

        tool_block = self.query_one("#tool-block", ToolBlock)
        status_bar = self.query_one("#status-bar", StatusBar)

        status_bar.update(
            fraction=event.context_pct,
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
            elapsed_ms=event.elapsed_ms,
            tool_count=event.tool_count,
        )

        if event.tool_count > 0:
            obs_panel = self.query_one("#obs-panel", ObservabilityPanel)
            completed = tool_block.completed_tools[-event.tool_count :]
            from .components.observability_panel import ToolSummary

            obs_panel.show_tools(
                [
                    ToolSummary(
                        name=t.name,
                        duration_ms=t.duration_ms,
                        success=t.state.name == "DONE",
                    )
                    for t in completed
                ]
            )

        tool_block.clear()

    def _on_budget_update(self, event: BudgetUpdate) -> None:
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.context_fraction = event.fraction
        status_bar.input_tokens = event.input_tokens
        status_bar.output_tokens = event.output_tokens

    # ── Artifact and Knowledge Graph helpers ──────────────────────────────────

    def add_artifact(
        self,
        path: str,
        state: ArtifactState,
        diff_lines: list[str] | None = None,
        preview: str = "",
    ) -> None:
        workspace = self.query_one("#workspace-panel", WorkspacePanel)
        workspace.add_artifact(path, state, diff_lines, preview)

    def load_knowledge_graph(
        self,
        semantic_count: int = 0,
        procedural_count: int = 0,
        episodic_count: int = 0,
    ) -> None:
        workspace = self.query_one("#workspace-panel", WorkspacePanel)
        workspace.load_knowledge_graph(semantic_count, procedural_count, episodic_count)

    # ── Input submission ───────────────────────────────────────────────────────

    class UserMessage(Message, bubble=True):
        """User submitted a message — sent to main.py to drive LoomSession."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def on_input_area_submit(self, event: InputArea.Submit) -> None:
        """Relay InputArea.Submit up to the session handler via UserMessage."""
        self.post_message(self.UserMessage(event.text))
