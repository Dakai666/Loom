"""
LoomApp — main Textual application.

Wires LoomSession.stream_turn() events to Textual components.

Parchment theme palette:
  #1c1814  screen background (very dark warm brown)
  #242018  widget surface
  #e0cfa0  primary text (warm cream)
  #8a7a5e  muted text
  #c8a464  accent (amber gold)
  #7a9e78  success (sage green)
  #c8924a  warning (ochre)
  #b87060  error (terracotta)
  #4a4038  border
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Provider, Hit
from textual.containers import Horizontal, Vertical
from textual.widget import Widget

from .components import (
    Header,
    MessageList,
    ToolBlock,
    AgentState,
    StatusBar,
    InputArea,
    ObservabilityPanel,
    WorkspacePanel,
    WorkspaceTab,
    ArtifactState,
    ActivityEntry,
)
from .components.message_list import Role
from .events import (
    StreamEvent,
    TurnStart,
    TextChunk,
    ToolBegin,
    ToolEnd,
    TurnDone,
    TurnPaused,
    BudgetUpdate,
)

if TYPE_CHECKING:
    from loom.core.memory.store import SQLiteStore


class LoomCommandProvider(Provider):
    async def search(self, query: str):
        matcher = self.matcher(query)
        app = self.app
        commands = [
            ("Toggle Workspace Sidebar", app.action_toggle_sidebar, "Hide/show the right sidebar"),
            ("Switch to Artifacts Tab", lambda: self._focus_tab(WorkspaceTab.ARTIFACTS), "View created artifacts"),
            ("Switch to Swarm Dashboard", lambda: self._focus_tab(WorkspaceTab.SWARM), "View active agents and history"),
            ("Switch to Budget Tab", lambda: self._focus_tab(WorkspaceTab.BUDGET), "View context token usage"),
            ("Clear Conversation", app.action_clear_screen, "Clear the chat history"),
            ("Toggle Verbose Tools", app.action_toggle_verbose, "Switch between compact and verbose tool logs"),
            ("Quit Loom", app.action_quit, "Exit the application"),
        ]

        for name, callback, help_text in commands:
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), callback, help=help_text)

    def _focus_tab(self, tab: WorkspaceTab):
        workspace = self.app.query_one("#workspace-panel", WorkspacePanel)
        workspace.active_tab = tab
        workspace._update_tab_headers()
        workspace.query_one("#tab-content").update(workspace._render_tab_content())
        self.app.notify(f"Switched to {tab.name.title()} tab", timeout=1)


class LoomApp(App):
    """
    Main Loom TUI application — Parchment theme.

    Layout:
        Header             (dock top, 3 rows)
        Horizontal body
          Vertical conversation-pane (75%)
            MessageList      (fills)
            ToolBlock        (auto, max 6)
            InputArea        (4 rows)
          WorkspacePanel     (25%)
        ObservabilityPanel   (dock bottom, hidden by default)
        StatusBar            (dock bottom, 1 row)

    Bindings:
        Escape:  interrupt current generation
        Ctrl+L:  clear screen
        F1:      toggle verbose tool output
        F2:      cycle workspace tab
        Ctrl+C:  quit
    """

    # ── Parchment CSS ─────────────────────────────────────────────────────────

    CSS = """
    Screen {
        background: #1c1814;
    }

    #header-bar {
        dock: top;
        height: 3;
    }

    #body {
        height: 1fr;
    }

    #conversation-pane {
        width: 75%;
        border-right: solid #4a4038;
    }

    .sidebar-hidden #conversation-pane {
        width: 100%;
        border-right: none;
    }

    #message-list {
        height: 1fr;
        background: #1c1814;
        border: none;
    }

    #tool-block {
        height: auto;
        max-height: 2;
        background: #1c1814;
        border-top: solid #4a4038;
        padding: 0 1;
    }

    #input-area {
        height: 4;
        background: #242018;
        border-top: solid #4a4038;
    }

    #workspace-panel {
        width: 25%;
        background: #1c1814;
    }

    .sidebar-hidden #workspace-panel {
        display: none;
    }

    #obs-panel {
        dock: bottom;
        height: 1;
        background: #1c1814;
        border-top: solid #4a4038;
        display: none;
    }

    #obs-panel.visible {
        display: block;
        height: 2;
    }

    #status-bar {
        dock: bottom;
        height: 1;
        background: #1c1814;
        border-top: solid #4a4038;
    }

    /* MessageBubble spacing */
    MessageBubble {
        padding: 0 1;
    }

    /* Input widget warm colours */
    Input {
        background: #242018;
        color: #e0cfa0;
        border: solid #4a4038;
    }
    Input:focus {
        border: solid #c8a464;
    }

    /* ── Scrollbar — parchment palette ──────────────────────────────────────
       In Textual 8, scrollbar colours are CSS properties set on the scrollable
       widget (or Screen for a global default), not on the ScrollBar widget.    */
    Screen {
        scrollbar-background: #1c1814;
        scrollbar-color: #4a4038;
        scrollbar-color-hover: #c8a464;
        scrollbar-color-active: #c8a464;
        scrollbar-background-hover: #1c1814;
        scrollbar-background-active: #1c1814;
        scrollbar-corner-color: #1c1814;
    }

    #message-list {
        scrollbar-background: #1c1814;
        scrollbar-color: #4a4038;
        scrollbar-color-hover: #c8a464;
    }
    """

    COMMANDS = App.COMMANDS | {LoomCommandProvider}

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=True, priority=True),
        Binding("ctrl+k", "command_palette", "Cmd Palette", show=True),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar", show=True),
        Binding("escape", "interrupt", "Interrupt", show=True),
        Binding("ctrl+l", "clear_screen", "Clear", show=True),
        Binding("f1", "toggle_verbose", "Verbose", show=True),
        Binding("f2", "toggle_space", "Workspace", show=False),
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

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_interrupt(self) -> None:
        """Interrupt the current agent turn (cancels the worker)."""
        # The exclusive worker in main.py handles CancelledError gracefully.
        self.workers.cancel_all()
        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block.end_turn()
        # Finalize any open streaming bubble so it renders as Markdown
        # instead of staying in cream-text streaming state indefinitely.
        msg_list = self.query_one("#message-list", MessageList)
        msg_list.finish_streaming()
        self.notify("Interrupted.", severity="warning", timeout=2)

    def action_clear_screen(self) -> None:
        msg_list = self.query_one("#message-list", MessageList)
        msg_list.clear()
        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block.clear()
        self.notify("Screen cleared.")

    def action_toggle_sidebar(self) -> None:
        self.toggle_class("sidebar-hidden")
        has_sidebar = not self.has_class("sidebar-hidden")
        self.notify(f"Sidebar {'shown' if has_sidebar else 'hidden'}", timeout=1)

    def action_toggle_space(self) -> None:
        workspace = self.query_one("#workspace-panel", WorkspacePanel)
        workspace.toggle_tab()
        labels = {
            WorkspaceTab.ARTIFACTS: "Artifacts",
            WorkspaceTab.SWARM:     "Swarm",
            WorkspaceTab.BUDGET:    "Budget",
        }
        self.notify(f"Workspace: {labels[workspace.active_tab]}", timeout=1)

    def action_toggle_verbose(self) -> None:
        self._verbose = not self._verbose
        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block.verbose = self._verbose
        self.notify(f"Tool output: {'verbose' if self._verbose else 'compact'}", timeout=1)

    # ── Stream event dispatch ─────────────────────────────────────────────────

    async def dispatch_stream_event(self, event: StreamEvent) -> None:
        """Bridge between LoomSession.stream_turn() and the widget tree."""
        if isinstance(event, TurnStart):
            await self._on_turn_start(event)
        elif isinstance(event, TextChunk):
            self._on_text_chunk(event)
        elif isinstance(event, ToolBegin):
            self._on_tool_begin(event)
        elif isinstance(event, ToolEnd):
            self._on_tool_end(event)
        elif isinstance(event, TurnPaused):
            await self._on_turn_paused(event)
        elif isinstance(event, TurnDone):
            await self._on_turn_done(event)
        elif isinstance(event, BudgetUpdate):
            self._on_budget_update(event)

    async def _on_turn_start(self, event: TurnStart) -> None:
        msg_list = self.query_one("#message-list", MessageList)
        msg_list.add_message(Role.USER, event.user_input)

        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block.start_turn()

        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.update(fraction=event.context_pct)

    def _on_text_chunk(self, event: TextChunk) -> None:
        msg_list = self.query_one("#message-list", MessageList)
        msg_list.stream_text(event.text)

    def _on_tool_begin(self, event: ToolBegin) -> None:
        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block.start_tool(event.name, event.args, event.call_id)

        # Append tool call inline to the message bubble so the full action
        # history is visible in the conversation (not erased after the turn).
        msg_list = self.query_one("#message-list", MessageList)
        primary = ""
        if event.args:
            first_val = next(iter(event.args.values()), "")
            if isinstance(first_val, str):
                primary = first_val.replace("\n", "↵")
        label = f"\n⟳ {event.name}" + (f' — "{primary}"' if primary else "")
        msg_list.stream_text(label)

    def _on_tool_end(self, event: ToolEnd) -> None:
        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block.complete_tool(
            event.call_id, event.success, event.output, event.duration_ms
        )

        # Append result line inline to the message bubble.
        msg_list = self.query_one("#message-list", MessageList)
        if event.success:
            msg_list.stream_text(f"\n  ✓ {event.duration_ms:.0f}ms")
        else:
            err = event.output[:100].replace("\n", " ") if event.output else "failed"
            msg_list.stream_text(f"\n  ✗ {err} ({event.duration_ms:.0f}ms)")

        # Forward to Activity Log
        workspace = self.query_one("#workspace-panel", WorkspacePanel)
        error_snippet = ""
        if not event.success:
            first_line = event.output.split("\n")[0] if event.output else ""
            error_snippet = first_line[:80]
        args_preview = getattr(event, "_args_preview", "")
        workspace.append_activity(ActivityEntry(
            name=event.name,
            args_preview=args_preview,
            success=event.success,
            duration_ms=event.duration_ms,
            error_snippet=error_snippet,
            expanded=not event.success,
        ))

    async def _on_turn_paused(self, event: TurnPaused) -> None:
        from .components.interactive_widgets import InlinePauseWidget
        import asyncio

        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block.end_turn()

        msg_list = self.query_one("#message-list", MessageList)
        future = asyncio.Future()
        widget = InlinePauseWidget(tool_count=event.tool_count_so_far, future=future)
        msg_list.mount(widget)
        msg_list.scroll_end(animate=False)

        result = await future

        # result is injected into the app; but we can't call session methods directly
        # here (app.py has no session reference). We post a UserMessage with a
        # special prefix that main.py's _run_turn intercepts.
        if result == "__cancel__":
            self.post_message(self.HitlDecision("__cancel__"))
        elif result:
            self.post_message(self.HitlDecision(result))
        else:
            self.post_message(self.HitlDecision(None))

    async def _on_turn_done(self, event: TurnDone) -> None:
        msg_list = self.query_one("#message-list", MessageList)
        msg_list.finish_streaming()
        if event.think_text:
            msg_list.set_last_think(event.think_text)

        tool_block = self.query_one("#tool-block", ToolBlock)
        status_bar = self.query_one("#status-bar", StatusBar)

        tool_block.end_turn()

        status_bar.update(
            fraction=event.context_pct,
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
            elapsed_ms=event.elapsed_ms,
            tool_count=event.tool_count,
        )

        # Update Budget panel
        if event.max_tokens > 0:
            workspace = self.query_one("#workspace-panel", WorkspacePanel)
            workspace.update_budget(
                fraction=event.context_pct,
                used_tokens=event.used_tokens,
                max_tokens=event.max_tokens,
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
            )

        if event.tool_count > 0:
            obs_panel = self.query_one("#obs-panel", ObservabilityPanel)
            completed = tool_block.completed_tools[-event.tool_count:]
            from .components.observability_panel import ToolSummary
            obs_panel.show_tools([
                ToolSummary(
                    name=t.name,
                    duration_ms=t.duration_ms,
                    success=t.state.name == "DONE",
                )
                for t in completed
            ])

        tool_block.clear()

    def _on_budget_update(self, event: BudgetUpdate) -> None:
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.context_fraction = event.fraction
        status_bar.input_tokens = event.input_tokens
        status_bar.output_tokens = event.output_tokens

        if event.max_tokens > 0:
            workspace = self.query_one("#workspace-panel", WorkspacePanel)
            workspace.update_budget(
                fraction=event.fraction,
                used_tokens=event.used_tokens,
                max_tokens=event.max_tokens,
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
            )

    # ── Artifact helpers ──────────────────────────────────────────────────────

    def add_artifact(
        self,
        path: str,
        state: ArtifactState,
        diff_lines: list[str] | None = None,
        preview: str = "",
    ) -> None:
        workspace = self.query_one("#workspace-panel", WorkspacePanel)
        workspace.add_artifact(path, state, diff_lines, preview)

    # ── Kept for backward compat (called from main.py on_mount) ──────────────

    def load_knowledge_graph(self, **_kwargs) -> None:
        """No-op — KnowledgeGraph has been replaced by ActivityLog + BudgetPanel."""
        pass

    # ── Input relay ───────────────────────────────────────────────────────────

    from textual.message import Message as _Msg

    class UserMessage(_Msg, bubble=True):
        """User submitted a message."""
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class HitlDecision(_Msg, bubble=True):
        """User's decision from the PauseModal."""
        def __init__(self, decision: str | None) -> None:
            super().__init__()
            self.decision = decision  # None=resume, "__cancel__"=cancel, str=redirect msg

    def on_input_area_submit(self, event: InputArea.Submit) -> None:
        self.post_message(self.UserMessage(event.text))
