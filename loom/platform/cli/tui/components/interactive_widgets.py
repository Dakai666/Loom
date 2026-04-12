"""
Interactive Micro-Widgets for Phase 3.
Embeds HITL confirmations directly inside the MessageList.
"""
from __future__ import annotations

import asyncio
from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Static, Input
from textual.widget import Widget

from loom.core.harness.scope import ConfirmDecision


class InlineConfirmWidget(Widget):
    DEFAULT_CSS = """
    InlineConfirmWidget {
        height: auto;
        padding: 1;
        margin: 1 0;
        background: #242018;
        border-left: thick #c8924a;
        layout: vertical;
    }
    InlineConfirmWidget.critical { border-left: thick #b87060; }
    .confirm-content { margin-bottom: 1; width: 1fr; }
    .confirm-buttons { height: auto; width: auto; align: left middle; }
    .btn-allow  { background: #7a9e78; color: #1c1814; border: none; min-width: 12; margin-right: 1; min-height: 1; padding: 0 1; }
    .btn-allow:hover  { background: #9abf98; }
    .btn-lease  { background: #6a7a9e; color: #e0d8c0; border: none; min-width: 12; margin-right: 1; min-height: 1; padding: 0 1; }
    .btn-lease:hover  { background: #8a9abe; }
    .btn-auto   { background: #7a6a9e; color: #e0d8c0; border: none; min-width: 12; margin-right: 1; min-height: 1; padding: 0 1; }
    .btn-auto:hover   { background: #9a8abe; }
    .btn-deny   { background: #b87060; color: #1c1814; border: none; min-width: 12; min-height: 1; padding: 0 1; }
    .btn-deny:hover   { background: #d09080; }
    """

    def __init__(
        self,
        tool_name: str,
        trust_label: str,
        args_preview: str,
        future: "asyncio.Future[ConfirmDecision]",
    ) -> None:
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
        if self._trust_label == "SAFE":
            colour = "#7a9e78"

        yield Static(
            f"[bold {colour}]Action Required ({self._trust_label})[/]\n"
            f"[bold]{escape(self._tool_name)}[/] [dim]{escape(self._args_preview)}[/dim]\n"
            f"[dim]y=approve once  s=scope lease (30 min)  a=auto-approve  N=deny[/dim]",
            classes="confirm-content",
        )
        with Horizontal(classes="confirm-buttons"):
            yield Button("✓ Allow [y]", id="btn-allow", classes="btn-allow")
            yield Button("⏱ Lease [s]", id="btn-lease", classes="btn-lease")
            yield Button("⚡ Auto  [a]", id="btn-auto", classes="btn-auto")
            yield Button("✗ Deny  [N]", id="btn-deny", classes="btn-deny")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self._resolved:
            return
        self._resolved = True
        decision_map = {
            "btn-allow": ConfirmDecision.ONCE,
            "btn-lease": ConfirmDecision.SCOPE,
            "btn-auto":  ConfirmDecision.AUTO,
            "btn-deny":  ConfirmDecision.DENY,
        }
        decision = decision_map.get(event.button.id, ConfirmDecision.DENY)
        if not self._future.done():
            self._future.set_result(decision)
        self.remove()

class InlinePauseWidget(Widget):
    DEFAULT_CSS = """
    InlinePauseWidget {
        height: auto;
        padding: 1;
        margin: 1 0;
        background: #242018;
        border-left: thick #8a7a5e;
    }
    .pause-content { margin-bottom: 1; color: #8a7a5e; }
    .pause-input { background: #1c1814; color: #e0cfa0; border: none; height: auto; min-height: 1; padding: 0 1; width: 1fr; margin-right: 1; }
    .pause-input:focus { border: none; background: #2a241e; }
    .pause-buttons { height: auto; width: auto; align: right middle; }
    .btn-resume { background: #3a4a38; color: #7a9e78; border: none; min-height: 1; padding: 0 1; margin-right: 1; }
    .btn-resume:hover { background: #4a5a48; }
    .btn-cancel { background: #4a2a28; color: #b87060; border: none; min-height: 1; padding: 0 1; }
    .btn-cancel:hover { background: #5a3a38; }
    """

    def __init__(self, tool_count: int, future: asyncio.Future[str | None]) -> None:
        super().__init__()
        self._tool_count = tool_count
        self._future = future
        self._resolved = False

    def compose(self) -> ComposeResult:
        yield Static(f"[bold #c8a464]⏸ Paused[/] [dim]({self._tool_count} tool calls so far)[/]", classes="pause-content")
        with Horizontal():
            yield Input(placeholder="Redirect message (or empty to resume)...", classes="pause-input")
        with Horizontal(classes="pause-buttons"):
            yield Button("▶ Resume", id="btn-resume", classes="btn-resume")
            yield Button("✕ Stop", id="btn-cancel", classes="btn-cancel")

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
        
        if not self._future.done():
            self._future.set_result(result)
            
        self.remove()
