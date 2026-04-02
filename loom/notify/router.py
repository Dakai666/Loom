"""
NotificationRouter — fan-out to all registered notifiers.

Notifiers are registered by channel name.  When `send()` is called,
the router dispatches to all enabled notifiers concurrently.
Errors in individual notifiers are caught and logged; they do not
prevent other notifiers from delivering the message.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .types import Notification


class BaseNotifier:
    """Abstract base for all notification adapters."""
    channel: str

    async def send(self, notification: Notification) -> None:
        raise NotImplementedError


class NotificationRouter:
    """
    Routes notifications to one or more registered notifiers.

    Usage:
        router = NotificationRouter()
        router.register(CLINotifier())
        router.register(WebhookNotifier(url="https://..."))
        await router.send(notification)
    """

    def __init__(self) -> None:
        self._notifiers: dict[str, BaseNotifier] = {}

    def register(self, notifier: BaseNotifier) -> "NotificationRouter":
        self._notifiers[notifier.channel] = notifier
        return self

    def get(self, channel: str) -> BaseNotifier | None:
        return self._notifiers.get(channel)

    @property
    def channels(self) -> list[str]:
        return list(self._notifiers)

    async def send(self, notification: Notification) -> dict[str, bool]:
        """
        Deliver notification to all registered notifiers concurrently.
        Returns {channel: success} dict.
        """
        results: dict[str, bool] = {}
        tasks = {
            channel: notifier.send(notification)
            for channel, notifier in self._notifiers.items()
        }
        outcomes = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for channel, outcome in zip(tasks.keys(), outcomes):
            results[channel] = not isinstance(outcome, Exception)
        return results

    async def send_to(
        self, channel: str, notification: Notification
    ) -> bool:
        """Send to a specific channel only."""
        notifier = self._notifiers.get(channel)
        if notifier is None:
            return False
        try:
            await notifier.send(notification)
            return True
        except Exception:
            return False
