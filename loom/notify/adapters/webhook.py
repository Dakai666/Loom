"""
Webhook Notifier — delivers notifications to an HTTP endpoint via POST.

Payload format (JSON):
    {
        "id": "...",
        "type": "confirm",
        "title": "...",
        "body": "...",
        "trigger_name": "...",
        "timeout_seconds": 60,
        "created_at": "2026-04-02T...",
        "metadata": {}
    }

For CONFIRM notifications the caller is expected to POST a reply to
a separate endpoint.  `wait_reply()` polls an in-process asyncio Queue
that an external handler can push into via `push_reply()`.

This design keeps the notifier stateless with respect to HTTP while
still supporting the ConfirmFlow protocol.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any
from urllib import request as urllib_request
from urllib.error import URLError

from loom.notify.router import BaseNotifier
from loom.notify.types import ConfirmResult, Notification


class WebhookNotifier(BaseNotifier):
    channel = "webhook"

    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.headers: dict[str, str] = {"Content-Type": "application/json"}
        if headers:
            self.headers.update(headers)
        self._reply_queues: dict[str, asyncio.Queue[ConfirmResult]] = {}

    async def send(self, notification: Notification) -> None:
        payload = self._serialize(notification)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._post, payload)
        # Pre-create reply queue so push_reply() can always find it
        if notification.id not in self._reply_queues:
            self._reply_queues[notification.id] = asyncio.Queue(maxsize=1)

    async def wait_reply(self, notification: Notification) -> ConfirmResult:
        """Wait for an external system to push a reply via push_reply()."""
        queue = self._reply_queues.setdefault(
            notification.id, asyncio.Queue(maxsize=1)
        )
        return await queue.get()

    def push_reply(self, notification_id: str, result: ConfirmResult) -> None:
        """
        Called by an external HTTP handler when the user replies.
        Puts the result into the matching queue so wait_reply() unblocks.
        """
        queue = self._reply_queues.get(notification_id)
        if queue and not queue.full():
            queue.put_nowait(result)

    # ------------------------------------------------------------------

    @staticmethod
    def _serialize(n: Notification) -> bytes:
        data: dict[str, Any] = {
            "id":              n.id,
            "type":            n.type.value,
            "title":           n.title,
            "body":            n.body,
            "trigger_name":    n.trigger_name,
            "timeout_seconds": n.timeout_seconds,
            "created_at":      n.created_at.isoformat(),
            "metadata":        n.metadata,
            "attachments":     [str(p) for p in n.attachments],
            "inline_image":    str(n.inline_image) if n.inline_image else None,
        }
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    def _post(self, payload: bytes) -> None:
        req = urllib_request.Request(
            self.url,
            data=payload,
            headers=self.headers,
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=10):
                pass
        except URLError as exc:
            raise RuntimeError(f"Webhook POST failed: {exc}") from exc


class TelegramNotifier(BaseNotifier):
    """
    Telegram Bot notifier.

    Sends a message to a specific chat_id via the Telegram Bot API.
    For CONFIRM notifications, the user can reply to the bot with
    /yes or /no — handled by an external bot reply webhook that
    calls push_reply().
    """
    channel = "telegram"

    SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._reply_queues: dict[str, asyncio.Queue[ConfirmResult]] = {}

    async def send(self, notification: Notification) -> None:
        text = f"*{notification.title}*\n\n{notification.body}"
        if notification.type.value == "confirm":
            text += f"\n\n_Reply /yes or /no within {notification.timeout_seconds}s_"

        payload = json.dumps({
            "chat_id":    self._chat_id,
            "text":       text,
            "parse_mode": "Markdown",
        }).encode("utf-8")

        url = self.SEND_URL.format(token=self._token)
        req = urllib_request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._do_request, req)
        if notification.id not in self._reply_queues:
            self._reply_queues[notification.id] = asyncio.Queue(maxsize=1)

    async def wait_reply(self, notification: Notification) -> ConfirmResult:
        queue = self._reply_queues.setdefault(
            notification.id, asyncio.Queue(maxsize=1)
        )
        return await queue.get()

    def push_reply(self, notification_id: str, result: ConfirmResult) -> None:
        queue = self._reply_queues.get(notification_id)
        if queue and not queue.full():
            queue.put_nowait(result)

    @staticmethod
    def _do_request(req: urllib_request.Request) -> None:
        try:
            with urllib_request.urlopen(req, timeout=10):
                pass
        except URLError as exc:
            raise RuntimeError(f"Telegram send failed: {exc}") from exc
