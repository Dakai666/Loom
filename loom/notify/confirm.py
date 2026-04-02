"""
ConfirmFlow — handles confirmation requests with timeout degradation.

When an action requires user approval (GUARDED trust level), the flow:
  1. Sends a CONFIRM notification via all registered channels.
  2. Waits up to `timeout_seconds` for a response.
  3. If no response arrives, returns ConfirmResult.TIMEOUT.
     The caller decides what to do on timeout (skip, downgrade, etc.).

The reply mechanism is channel-specific:
  - CLI: waits for stdin input in the terminal.
  - Webhook: the caller polls a reply queue (see WebhookNotifier).
  - Telegram/Discord: handled by bot reply handler (Phase 4).
"""

from __future__ import annotations

import asyncio
from typing import Callable, Awaitable

from .types import ConfirmResult, Notification, NotificationType


ReplyWaiter = Callable[[Notification], Awaitable[ConfirmResult]]


class ConfirmFlow:
    """
    Coordinates sending a CONFIRM notification and waiting for a reply.

    Usage:
        flow = ConfirmFlow(send_fn=router.send, wait_fn=cli_notifier.wait_reply)
        result = await flow.ask(notification)
    """

    def __init__(
        self,
        send_fn: Callable[[Notification], Awaitable[None]],
        wait_fn: ReplyWaiter | None = None,
        default_on_timeout: ConfirmResult = ConfirmResult.TIMEOUT,
    ) -> None:
        self._send = send_fn
        self._wait = wait_fn
        self._default_on_timeout = default_on_timeout

    async def ask(self, notification: Notification) -> ConfirmResult:
        """
        Send the notification and wait for a reply within the timeout window.
        Returns APPROVED, DENIED, or TIMEOUT.
        """
        assert notification.type == NotificationType.CONFIRM, (
            "ConfirmFlow.ask() requires NotificationType.CONFIRM"
        )

        await self._send(notification)

        if self._wait is None:
            # No reply mechanism configured — treat as auto-approved (SAFE contexts).
            return ConfirmResult.APPROVED

        try:
            result = await asyncio.wait_for(
                self._wait(notification),
                timeout=notification.timeout_seconds,
            )
            return result
        except asyncio.TimeoutError:
            return self._default_on_timeout
