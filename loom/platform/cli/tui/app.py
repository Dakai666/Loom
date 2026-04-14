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
    InputArea,
    ObservabilityPanel,
    WorkspacePanel,
    WorkspaceTab,
    ArtifactState,
    ActivityEntry,
    ExecutionDashboard,
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
    ThinkCollapsed,
    BudgetUpdate,
    ActionStateChange,
    ActionRolledBack,
    EnvelopeStarted,
    EnvelopeUpdated,
    EnvelopeCompleted,
    GrantsUpdate,
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
            ("Switch to Execution Dashboard", lambda: self._focus_tab(WorkspaceTab.EXECUTION), "View envelope execution status"),
            ("Switch to Budget Tab", lambda: self._focus_tab(WorkspaceTab.BUDGET), "View context token usage"),
            ("Clear Conversation", app.action_clear_screen, "Clear the chat history"),

            ("Quit Loom", app.action_quit, "Exit the application"),
        ]

        for name, callback, help_text in commands:
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), callback, help=help_text)

    def _focus_tab(self, tab: WorkspaceTab):
        workspace = self.app.query_one("#workspace-panel", WorkspacePanel)
        workspace.active_tab = tab  # triggers watch_active_tab → _render_header + _update_visibility
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
        ObservabilityPanel   (dock bottom, hidden by default)

    Bindings:
        Escape:  interrupt current generation
        Ctrl+L:  clear screen
        F1:      command palette
        F2:      cycle workspace tab
        F4:      toggle sidebar
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
        height: auto;
        background: #1c1814;
        border-top: solid #4a4038;
        padding: 0 1;
    }

    #input-text {
        border: solid #4a4038;
        background: #1c1814;
    }
    
    #input-text:focus {
        border: solid #c8a464;
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
        # Primary global hotkeys (shown in footer)
        Binding("ctrl+c", "quit", "Quit", show=True, priority=True),
        Binding("escape", "interrupt", "Stop", show=True),
        Binding("ctrl+s", "session_picker", "Sessions", show=True),
        Binding("ctrl+l", "clear_screen", "Clear", show=True),
        # F-keys for functions (resilient in IDE terminals)
        Binding("f1", "command_palette", "Commands", show=True),
        Binding("f2", "toggle_space", "Workspace", show=True),
        Binding("f4", "toggle_sidebar", "Sidebar", show=True),
        Binding("f5", "time_travel", "Time-Travel", show=True),
        # Fallback VS Code style hotkeys (might be intercepted by IDEs)
        Binding("ctrl+b", "toggle_sidebar", "Sidebar", show=False),
        Binding("ctrl+k", "command_palette", "Commands", show=False),
        Binding("ctrl+p", "command_palette", "Commands", show=False),
    ]

    def __init__(
        self,
        model: str = "",
        db_path: str = "",
    ) -> None:
        super().__init__()
        self._model = model
        self._db_path = db_path
        
        try:
            from textual.theme import Theme
            loom_theme = Theme(
                name="loom",
                primary="#c8a464",
                secondary="#8a7a5e",
                warning="#c8924a",
                error="#b87060",
                success="#7a9e78",
                background="#1c1814",
                surface="#2a241e",
                panel="#242018",
                dark=True,
            )
            self.register_theme(loom_theme)
        except Exception:
            pass

    def compose(self) -> ComposeResult:
        yield Header(id="header-bar", model=self._model, db_path=self._db_path)
        with Horizontal(id="body"):
            with Vertical(id="conversation-pane"):
                yield MessageList(id="message-list")
                yield ToolBlock(id="tool-block")
                yield InputArea(id="input-area")
            yield WorkspacePanel(id="workspace-panel")
        yield ObservabilityPanel(id="obs-panel")

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
            WorkspaceTab.EXECUTION: "Execution",
            WorkspaceTab.BUDGET:    "Budget",
        }
        self.notify(f"Workspace: {labels[workspace.active_tab]}", timeout=1)

    # ── Stream event dispatch ─────────────────────────────────────────────────

    async def dispatch_stream_event(self, event: StreamEvent) -> None:
        """Bridge between LoomSession.stream_turn() and the widget tree."""
        if isinstance(event, TurnStart):
            await self._on_turn_start(event)
        elif isinstance(event, TextChunk):
            self._on_text_chunk(event)
        elif isinstance(event, ThinkCollapsed):
            self._on_think_collapsed(event)
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
        elif isinstance(event, ActionStateChange):
            self._on_action_state_change(event)
        elif isinstance(event, ActionRolledBack):
            self._on_action_rolled_back(event)
        elif isinstance(event, EnvelopeStarted):
            self._on_envelope_started(event)
        elif isinstance(event, EnvelopeUpdated):
            self._on_envelope_updated(event)
        elif isinstance(event, EnvelopeCompleted):
            self._on_envelope_completed(event)
        elif isinstance(event, GrantsUpdate):
            self._on_grants_update(event)

    async def _on_turn_start(self, event: TurnStart) -> None:
        msg_list = self.query_one("#message-list", MessageList)
        msg_list.add_message(Role.USER, event.user_input)

        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block.start_turn()

    def _on_text_chunk(self, event: TextChunk) -> None:
        msg_list = self.query_one("#message-list", MessageList)
        msg_list.stream_text(event.text)

    def _on_think_collapsed(self, event: ThinkCollapsed) -> None:
        """Inline think block: insert a clickable summary into the streaming bubble."""
        msg_list = self.query_one("#message-list", MessageList)
        msg_list.stream_think(event.summary, event.full)

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

    def _on_action_state_change(self, event: ActionStateChange) -> None:
        """Update ToolBlock to reflect lifecycle state changes (Issue #42)."""
        tool_block = self.query_one("#tool-block", ToolBlock)
        tool_block.update_tool_lifecycle(
            event.call_id, event.old_state, event.new_state, event.reason
        )

    def _on_action_rolled_back(self, event: ActionRolledBack) -> None:
        """Show rollback notification inline in the conversation (Issue #42)."""
        msg_list = self.query_one("#message-list", MessageList)
        icon = "✓" if event.rollback_success else "✗"
        msg_list.stream_text(
            f"\n  ↩ {icon} {event.tool_name} rolled back"
            + (f" — {event.message}" if event.message else "")
        )

    # ── Envelope event handlers (Issue #106/#107) ────────────────────────────

    def _on_envelope_started(self, event: EnvelopeStarted) -> None:
        workspace = self.query_one("#workspace-panel", WorkspacePanel)
        workspace.on_envelope_started(event.envelope)

    def _on_envelope_updated(self, event: EnvelopeUpdated) -> None:
        workspace = self.query_one("#workspace-panel", WorkspacePanel)
        workspace.on_envelope_updated(event.envelope)

    def _on_envelope_completed(self, event: EnvelopeCompleted) -> None:
        workspace = self.query_one("#workspace-panel", WorkspacePanel)
        workspace.on_envelope_completed(event.envelope)

    def _on_grants_update(self, event: GrantsUpdate) -> None:
        workspace = self.query_one("#workspace-panel", WorkspacePanel)
        workspace.update_grants(
            event.active_count,
            event.next_expiry_secs,
            event.grants,
        )

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
        tool_block.end_turn()

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

    def on_execution_dashboard_scroll_to_confirm(
        self, event: ExecutionDashboard.ScrollToConfirm,
    ) -> None:
        """When ⏳ node is activated, scroll MessageList to the matching confirm widget (#109)."""
        from .components.interactive_widgets import InlineConfirmWidget
        try:
            msg_list = self.query_one("#message-list", MessageList)
        except Exception:
            return
        for widget in msg_list.query(InlineConfirmWidget):
            if getattr(widget, "_call_id", "") == event.call_id:
                widget.scroll_visible(animate=True)
                return
