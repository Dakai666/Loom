"""
DiscordBotNotifier — delivers Loom notifications via a Discord bot client.

Unlike DiscordNotifier (webhook-only, one-way), this notifier uses a live
discord.py Client, allowing interactive CONFIRM dialogs with Allow/Deny buttons.

Intended for the Autonomy Daemon:
    notifier = DiscordBotNotifier(client=bot_client, channel_id=123456789)
    router.register(notifier)

For CONFIRM / INPUT notifications, the bot posts a message with buttons.
The user clicks Allow/Deny; the button callback calls push_reply() which
unblocks the ConfirmFlow's wait_reply().
"""

from __future__ import annotations

import asyncio

try:
    import discord
    from discord import ButtonStyle, Interaction
    from discord.ui import Button, View
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "DiscordBotNotifier requires discord.py.\n"
        "Install with:  pip install 'loom[discord]'"
    ) from exc

from loom.notify.router import BaseNotifier
from loom.notify.types import ConfirmResult, Notification, NotificationType


_COLORS: dict[NotificationType, int] = {
    NotificationType.INFO:    0x5865F2,
    NotificationType.CONFIRM: 0xFEE75C,
    NotificationType.REPORT:  0x57F287,
    NotificationType.ALERT:   0xED4245,
    NotificationType.INPUT:   0xEB459E,
}

_LABELS: dict[NotificationType, str] = {
    NotificationType.INFO:    "ℹ️ Info",
    NotificationType.CONFIRM: "❓ Confirm Required",
    NotificationType.REPORT:  "📋 Report",
    NotificationType.ALERT:   "🚨 Alert",
    NotificationType.INPUT:   "✏️ Input Needed",
}


# ---------------------------------------------------------------------------
# Confirm / Input view
# ---------------------------------------------------------------------------

class _ReplyView(View):
    """Allow / Deny buttons that push a result to DiscordBotNotifier."""

    def __init__(
        self,
        notification_id: str,
        notifier: "DiscordBotNotifier",
        timeout: float = 60.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._nid = notification_id
        self._notifier = notifier

    @discord.ui.button(label="Allow", style=ButtonStyle.green, emoji="✅")
    async def allow(self, interaction: Interaction, button: Button) -> None:
        self._notifier.push_reply(self._nid, ConfirmResult.APPROVED)
        await interaction.response.edit_message(
            content="✅ **Approved**", view=None
        )

    @discord.ui.button(label="Deny", style=ButtonStyle.red, emoji="❌")
    async def deny(self, interaction: Interaction, button: Button) -> None:
        self._notifier.push_reply(self._nid, ConfirmResult.DENIED)
        await interaction.response.edit_message(
            content="❌ **Denied**", view=None
        )

    async def on_timeout(self) -> None:
        self._notifier.push_reply(self._nid, ConfirmResult.TIMEOUT)


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------

class DiscordBotNotifier(BaseNotifier):
    """
    Send Loom notifications to a Discord channel via a bot client.

    Args:
        client:     A connected discord.Client (or subclass) instance.
        channel_id: ID of the Discord channel to post notifications into.
    """

    channel = "discord"

    def __init__(self, client: discord.Client, channel_id: int) -> None:
        self._client = client
        self._channel_id = channel_id
        self._reply_queues: dict[str, asyncio.Queue[ConfirmResult]] = {}

    # ------------------------------------------------------------------
    # BaseNotifier interface
    # ------------------------------------------------------------------

    async def send(self, notification: Notification) -> None:
        ch = self._client.get_channel(self._channel_id)
        if ch is None:
            return

        embed = self._build_embed(notification)

        if notification.type in (NotificationType.CONFIRM, NotificationType.INPUT):
            if notification.id not in self._reply_queues:
                self._reply_queues[notification.id] = asyncio.Queue(maxsize=1)
            view = _ReplyView(
                notification_id=notification.id,
                notifier=self,
                timeout=float(notification.timeout_seconds),
            )
            await ch.send(embed=embed, view=view)
        else:
            await ch.send(embed=embed)

    async def wait_reply(self, notification: Notification) -> ConfirmResult:
        queue = self._reply_queues.setdefault(
            notification.id, asyncio.Queue(maxsize=1)
        )
        return await queue.get()

    def push_reply(self, notification_id: str, result: ConfirmResult) -> None:
        queue = self._reply_queues.get(notification_id)
        if queue and not queue.full():
            queue.put_nowait(result)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_embed(self, n: Notification) -> discord.Embed:
        color = _COLORS.get(n.type, 0x5865F2)
        label = _LABELS.get(n.type, n.type.value)

        embed = discord.Embed(
            title=f"{label}: {n.title}",
            description=n.body,
            color=color,
        )
        embed.set_footer(
            text=f"Loom  |  trigger: {n.trigger_name or '—'}  |  id: {n.id}"
        )
        return embed
