"""
Discord Notifier — delivers notifications to a Discord channel via Webhook URL.

Setup (no bot token required):
  1. Discord server → channel settings → Integrations → Webhooks → New Webhook
  2. Copy the Webhook URL
  3. Add to loom.toml or .env:
       DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."

Usage in code:
    from loom.notify.adapters.discord import DiscordNotifier
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/...")
    router.register(notifier)

Confirm flow:
  Confirm notifications append a "Reply via REST API" footer.
  When the user approves or denies, call push_reply() via the Loom REST API:
    POST /webhook/reply  {"notification_id": "...", "result": "approved"}
  The REST API endpoint calls push_reply() on the shared notifier instance,
  which unblocks the autonomy daemon's wait_reply().

Embed colors by type:
  INFO    — Discord blurple  #5865F2
  CONFIRM — Yellow           #FEE75C
  REPORT  — Green            #57F287
  ALERT   — Red              #ED4245
"""

from __future__ import annotations

import asyncio
import json
from urllib import request as urllib_request
from urllib.error import URLError

from loom.notify.router import BaseNotifier
from loom.notify.types import ConfirmResult, Notification, NotificationType


# Discord embed colors (decimal, not hex — Discord API requirement)
_COLORS: dict[NotificationType, int] = {
    NotificationType.INFO:    0x5865F2,   # blurple
    NotificationType.CONFIRM: 0xFEE75C,   # yellow
    NotificationType.REPORT:  0x57F287,   # green
    NotificationType.ALERT:   0xED4245,   # red
    NotificationType.INPUT:   0xEB459E,   # fuchsia
}

_TYPE_LABELS: dict[NotificationType, str] = {
    NotificationType.INFO:    "ℹ️ Info",
    NotificationType.CONFIRM: "❓ Confirm Required",
    NotificationType.REPORT:  "📋 Report",
    NotificationType.ALERT:   "🚨 Alert",
    NotificationType.INPUT:   "✏️ Input Needed",
}


class DiscordNotifier(BaseNotifier):
    """
    Send Loom notifications to a Discord channel via an Incoming Webhook.

    Args:
        webhook_url:  Full Discord Webhook URL.
        username:     Display name for the bot message (default: "Loom Agent").
        avatar_url:   Optional avatar image URL for the bot.
        rest_api_url: Optional base URL of Loom REST API, used to build the
                      reply instruction footer (e.g. "http://localhost:8000").
    """

    channel = "discord"

    def __init__(
        self,
        webhook_url: str,
        username: str = "Loom Agent",
        avatar_url: str | None = None,
        rest_api_url: str | None = None,
    ) -> None:
        self._webhook_url = webhook_url
        self._username = username
        self._avatar_url = avatar_url
        self._rest_api_url = rest_api_url
        self._reply_queues: dict[str, asyncio.Queue[ConfirmResult]] = {}

    # ------------------------------------------------------------------
    # BaseNotifier interface
    # ------------------------------------------------------------------

    async def send(self, notification: Notification) -> None:
        payload = self._build_payload(notification)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._post, payload)
        # Pre-create reply queue for CONFIRM so push_reply() can find it
        if notification.type == NotificationType.CONFIRM:
            if notification.id not in self._reply_queues:
                self._reply_queues[notification.id] = asyncio.Queue(maxsize=1)

    async def wait_reply(self, notification: Notification) -> ConfirmResult:
        """
        Block until push_reply() is called with this notification's ID.

        Typically invoked by ConfirmFlow after send().  An external system
        (Discord bot, Loom REST API) calls push_reply() to unblock this.
        """
        queue = self._reply_queues.setdefault(
            notification.id, asyncio.Queue(maxsize=1)
        )
        return await queue.get()

    def push_reply(self, notification_id: str, result: ConfirmResult) -> None:
        """
        Push a confirm result into the matching queue.

        Called by the Loom REST API (/webhook/reply) or a Discord bot webhook
        when the user responds to a CONFIRM notification.
        """
        queue = self._reply_queues.get(notification_id)
        if queue and not queue.full():
            queue.put_nowait(result)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_payload(self, n: Notification) -> bytes:
        color = _COLORS.get(n.type, 0x5865F2)
        type_label = _TYPE_LABELS.get(n.type, n.type.value)

        description = n.body
        if n.type == NotificationType.CONFIRM:
            footer = self._confirm_footer(n)
            description += f"\n\n{footer}"

        embed: dict = {
            "title":       f"{type_label}: {n.title}",
            "description": description,
            "color":       color,
            "footer": {"text": f"Loom  |  trigger: {n.trigger_name or '—'}  |  id: {n.id}"},
        }

        payload: dict = {
            "username": self._username,
            "embeds":   [embed],
        }
        if self._avatar_url:
            payload["avatar_url"] = self._avatar_url

        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _confirm_footer(self, n: Notification) -> str:
        if self._rest_api_url:
            base = self._rest_api_url.rstrip("/")
            return (
                f"**Reply via Loom API** (timeout: {n.timeout_seconds}s)\n"
                f"```\n"
                f'curl -X POST {base}/webhook/reply \\\n'
                f'  -H "Content-Type: application/json" \\\n'
                f'  -d \'{{"notification_id":"{n.id}","result":"approved"}}\'\n'
                f"```"
            )
        return f"*(Reply via Loom REST API — notification id: `{n.id}`)*"

    def _post(self, payload: bytes) -> None:
        req = urllib_request.Request(
            self._webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=10):
                pass
        except URLError as exc:
            raise RuntimeError(f"Discord webhook POST failed: {exc}") from exc
