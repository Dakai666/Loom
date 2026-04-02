"""
LoomApp — main Textual application.

Wires LoomSession.stream_turn() events to Textual components.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
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

    Layout (dock):
        Header (top, 3 rows)
        MessageList (center, flex)
        InputArea (bottom, 3 rows)
        StatusBar (bottom, 1 row)
        ObservabilityPanel (bottom dock, hidden by default)

    Bindings:
        Ctrl+L: clear screen
        Ctrl+O: toggle verbose
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

    #conversation-pane {
        dock: left;
        width: 60%;
        border-right: solid $border;
    }

    #workspace-pane {
        dock: left;
        width: 40%;
    }

    #message-list {
        dock: top;
        height: 1fr;
    }

    #tool-block {
        dock: top;
        height: auto;
        max-height: 5;
        overflow-y: auto;
    }

    #status-bar {
        dock: bottom;
        height: 1;
        background: $surface;
        border-top: solid $border;
    }

    #input-area {
        dock: bottom;
        height: 3;
        background: $surface;
        border-top: solid $border;
    }

    #obs-panel {
        dock: bottom;
        height: 3;
        background: $surface;
        border-top: solid $border;
        display: none;
    }

    #obs-panel.visible {
        display: block;
    }

    #workspace-panel {
        dock: top;
        height: 1fr;
        overflow-y: auto;
    }
    """

    BINDINGS = [
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
        yield Header(model=self._model, db_path=self._db_path)
        with Static(id="conversation-pane"):
            yield MessageList(id="message-list")
            yield ToolBlock(id="tool-block")
        yield WorkspacePanel(id="workspace-panel")
        yield ObservabilityPanel(id="obs-panel")
        yield StatusBar(id="status-bar")
        yield InputArea(id="input-area")

    def on_mount(self) -> None:
        """Set focus to input area on startup."""
        input_area = self.query_one("#input-area", InputArea)
        self.set_focus(input_area)

    # ── Actions (called by bindings) ─────────────────────────────────────────

    def action_clear_screen(self) -> None:
        """Clear the message list (not the terminal)."""
        msg_list = self.query_one("#message-list", MessageList)
        msg_list.clear()
        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block.clear()
        self.notify("[dim]Screen cleared[/dim]")

    def action_toggle_space(self) -> None:
        """Toggle between Conversation and Workspace space."""
        workspace = self.query_one("#workspace-panel", WorkspacePanel)
        workspace.toggle_tab()
        current = workspace.active_tab
        label = "Knowledge" if current == WorkspaceTab.KNOWLEDGE else "Artifacts"
        self.notify(f"[dim]Switched to [bold]{label}[/bold][/dim]")

    def action_toggle_verbose(self) -> None:
        """Toggle tool output verbosity."""
        self._verbose = not self._verbose
        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block.verbose = self._verbose
        state = (
            "[green]verbose[/green]" if self._verbose else "[yellow]compact[/yellow]"
        )
        self.notify(f"[dim]Tool output: {state}[/dim]")

    # ── Event dispatch from LoomSession ───────────────────────────────────────

    async def dispatch_stream_event(self, event: StreamEvent) -> None:
        """
        Dispatch a stream event from LoomSession to the appropriate component.

        This is the bridge between the async LoomSession.stream_turn() generator
        and the Textual message bus.
        """
        from textual.message import Circle as Msg

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
        """Handle turn start."""
        msg_list = self.query_one("#message-list", MessageList)
        msg_list.add_message(Role.USER, event.user_input)
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.update(fraction=event.context_pct)

    def _on_text_chunk(self, event: TextChunk) -> None:
        """Handle streaming text chunk."""
        msg_list = self.query_one("#message-list", MessageList)
        msg_list.stream_text(event.text)

    def _on_tool_begin(self, event: ToolBegin) -> None:
        """Handle tool begin."""
        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block.start_tool(event.name, event.args, event.call_id)

    def _on_tool_end(self, event: ToolEnd) -> None:
        """Handle tool end."""
        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block.complete_tool(
            event.call_id, event.success, event.output, event.duration_ms
        )

    async def _on_turn_done(self, event: TurnDone) -> None:
        """Handle turn done."""
        msg_list = self.query_one("#message-list", MessageList)
        msg_list.finish_streaming()

        tool_block = self.query_one("#tool-block", ToolBlock)
        status_bar = self.query_one("#status-bar", StatusBar)

        # Update status bar
        status_bar.update(
            fraction=event.context_pct,
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
            elapsed_ms=event.elapsed_ms,
            tool_count=event.tool_count,
        )

        # Show observability panel
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

        # Clear tool block for next turn
        tool_block.clear()

    def _on_budget_update(self, event: BudgetUpdate) -> None:
        """Handle budget update."""
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
        """Add an artifact to the workspace (called by main.py)."""
        workspace = self.query_one("#workspace-panel", WorkspacePanel)
        workspace.add_artifact(path, state, diff_lines, preview)

    def load_knowledge_graph(
        self,
        semantic_count: int = 0,
        procedural_count: int = 0,
        episodic_count: int = 0,
    ) -> None:
        """Load knowledge graph from session memory stats."""
        workspace = self.query_one("#workspace-panel", WorkspacePanel)
        workspace.load_knowledge_graph(semantic_count, procedural_count, episodic_count)

    # ── Input submission ───────────────────────────────────────────────────────

    def on_input_area_submit(self, event: InputArea.Submit) -> None:
        """Handle message submission from InputArea."""
        self.post_message(self.UserMessage(event.text))
        """Handle message submission from InputArea."""
        # This will be connected to LoomSession in main.py
        # The app emits a custom message that main.py listens to
        self.post_message(self.UserMessage(event.text))


class UserMessage(Message, bubble=True):
    """User submitted a message — sent to main.py to drive LoomSession."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text
