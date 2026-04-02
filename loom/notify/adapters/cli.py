"""
CLI Notifier — delivers notifications to the terminal using Rich.

For CONFIRM notifications it also provides a `wait_reply()` coroutine
that reads stdin, making it suitable as the `wait_fn` in ConfirmFlow.
"""

from __future__ import annotations

import asyncio
import sys

from rich.console import Console
from rich.panel import Panel

from loom.notify.router import BaseNotifier
from loom.notify.types import (
    ConfirmResult, Notification, NotificationType,
)

_ICON = {
    NotificationType.INFO:    "[cyan]ℹ[/cyan]",
    NotificationType.CONFIRM: "[yellow]⚠[/yellow]",
    NotificationType.INPUT:   "[blue]✎[/blue]",
    NotificationType.ALERT:   "[red]🚨[/red]",
    NotificationType.REPORT:  "[green]📋[/green]",
}

_BORDER = {
    NotificationType.INFO:    "cyan",
    NotificationType.CONFIRM: "yellow",
    NotificationType.INPUT:   "blue",
    NotificationType.ALERT:   "red",
    NotificationType.REPORT:  "green",
}


class CLINotifier(BaseNotifier):
    channel = "cli"

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()
        # reply queues keyed by notification id
        self._reply_queues: dict[str, asyncio.Queue[ConfirmResult]] = {}

    async def send(self, notification: Notification) -> None:
        icon  = _ICON.get(notification.type, "")
        border = _BORDER.get(notification.type, "white")

        body = notification.body
        if notification.type == NotificationType.CONFIRM:
            body += f"\n\n[dim]Reply [bold]y[/bold]/[bold]n[/bold] · timeout {notification.timeout_seconds}s[/dim]"

        self._console.print(Panel(
            f"{icon}  {body}",
            title=f"[bold]{notification.title}[/bold]",
            border_style=border,
            expand=False,
        ))

    async def wait_reply(self, notification: Notification) -> ConfirmResult:
        """
        Block waiting for a y/n reply from stdin.
        Called by ConfirmFlow as the `wait_fn`.
        """
        loop = asyncio.get_event_loop()
        answer = await loop.run_in_executor(
            None, self._read_confirm
        )
        if answer in ("y", "yes"):
            return ConfirmResult.APPROVED
        return ConfirmResult.DENIED

    @staticmethod
    def _read_confirm() -> str:
        try:
            return input("[loom confirm] y/n: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "n"
