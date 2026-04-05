"""
Interactive Micro-Widgets for Phase 3.
Embeds HITL confirmations directly inside the MessageList.
"""
from __future__ import annotations

import asyncio
from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static, Input
from textual.widget import Widget

class InlineConfirmWidget(Widget):
    DEFAULT_CSS = """
    InlineConfirmWidget {
        height: auto;
        margin: 1 0;
        padding: 1 2;
        background: #242018;
        border-left: thick #c8924a;
    }
    InlineConfirmWidget.critical {
        border-left: thick #b87060;
    }
    .confirm-title {
        color: #c8924a;
        text-style: bold;
        margin-bottom: 1;
    }
    InlineConfirmWidget.critical .confirm-title {
        color: #b87060;
    }
    .confirm-content {
        color: #e0cfa0;
    }
    .confirm-args {
        color: #8a7a5e;
        margin-bottom: 1;
    }
    .confirm-buttons {
        height: 3;
    }
    .btn-allow {
        background: #7a9e78;
        color: #1c1814;
        border: solid #4a4038;
        min-width: 14;
        margin-right: 1;
    }
    .btn-allow:hover { background: #9abf98; }
    .btn-deny {
        background: #b87060;
        color: #1c1814;
        border: solid #4a4038;
        min-width: 14;
    }
    .btn-deny:hover { background: #d09080; }
    """

    def __init__(self, tool_name: str, trust_label: str, args_preview: str, future: asyncio.Future[bool]) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._trust_label = trust_label
        self._args_preview = args_preview
        self._future = future
        self._resolved = False

    def compose(self) -> ComposeResult:
        if self._trust_label == "CRITICAL":
            self.add_class("critical")
        
        colour = "#b87060" if self._trust_label == "CRITICAL" else "#c8924a"
        if self._trust_label == "SAFE": colour = "#7a9e78"

        yield Static("Action Required", classes="confirm-title")
        yield Static(f"[bold]{escape(self._tool_name)}[/bold] [bold {colour}]{self._trust_label}[/]", classes="confirm-content")
        yield Static(f"[dim]{escape(self._args_preview)}[/dim]", classes="confirm-args")
        with Horizontal(classes="confirm-buttons", id="btn-group"):
            yield Button("✓ Allow", id="btn-allow", classes="btn-allow")
            yield Button("✗ Deny", id="btn-deny", classes="btn-deny")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self._resolved: return
        self._resolved = True
        is_allow = event.button.id == "btn-allow"
        
        self.query_one("#btn-group").remove()
        status = "[#7a9e78]✓ Accepted[/]" if is_allow else "[#b87060]✗ Denied[/]"
        self.mount(Static(status))
        
        if not self._future.done():
            self._future.set_result(is_allow)

class InlinePauseWidget(Widget):
    DEFAULT_CSS = """
    InlinePauseWidget {
        height: auto;
        margin: 1 0;
        padding: 1 2;
        background: #242018;
        border-left: thick #8a7a5e;
    }
    .pause-title {
        color: #c8a464;
        text-style: bold;
        margin-bottom: 1;
    }
    .pause-body { color: #8a7a5e; margin-bottom: 1; }
    .pause-input {
        background: #1c1814;
        color: #e0cfa0;
        border: solid #4a4038;
        margin-bottom: 1;
    }
    .pause-input:focus { border: solid #c8a464; }
    .pause-buttons { height: 3; }
    .btn-resume {
        background: #3a4a38;
        color: #7a9e78;
        border: solid #4a4038;
        margin-right: 1;
    }
    .btn-resume:hover { background: #4a5a48; }
    .btn-cancel {
        background: #4a2a28;
        color: #b87060;
        border: solid #4a4038;
    }
    .btn-cancel:hover { background: #5a3a38; }
    """

    def __init__(self, tool_count: int, future: asyncio.Future[str | None]) -> None:
        super().__init__()
        self._tool_count = tool_count
        self._future = future
        self._resolved = False

    def compose(self) -> ComposeResult:
        yield Static("⏸ Agent Paused", classes="pause-title")
        yield Static(f"Completed {self._tool_count} tool call(s). Waiting for your input.", classes="pause-body")
        yield Input(placeholder="Redirect message (or leave empty to resume)...", classes="pause-input")
        with Horizontal(classes="pause-buttons", id="btn-group"):
            yield Button("▶ Resume", id="btn-resume", classes="btn-resume")
            yield Button("✕ Cancel (Stop)", id="btn-cancel", classes="btn-cancel")

    def on_mount(self) -> None:
        safe_focus = self.query_one(Input)
        safe_focus.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-resume":
            self._do_resume()
        elif event.button.id == "btn-cancel":
            self._resolve("__cancel__")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._do_resume()

    def _do_resume(self) -> None:
        msg = self.query_one(Input).value.strip()
        self._resolve(msg if msg else None)

    def _resolve(self, result: str | None) -> None:
        if self._resolved: return
        self._resolved = True
        
        self.query_one(Input).remove()
        self.query_one("#btn-group").remove()
        
        if result == "__cancel__":
            self.mount(Static("[#b87060]✕ Turn Cancelled[/]"))
        elif result:
            self.mount(Static(f"[#7a9e78]▶ Resumed with:[/] [dim]{escape(result)}[/]"))
        else:
            self.mount(Static("[#7a9e78]▶ Resumed[/]"))
            
        if not self._future.done():
            self._future.set_result(result)
