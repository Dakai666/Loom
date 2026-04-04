"""
Loom Discord Bot — full Discord frontend for a LoomSession.

Each Discord channel gets its own persistent LoomSession.
Users interact by @mentioning the bot (in a server) or sending DMs.

Setup
-----
1. Create a Discord application & bot at https://discord.com/developers/applications
2. Enable "Message Content Intent" under Bot → Privileged Gateway Intents
3. Copy the bot token
4. Add to .env:
       DISCORD_BOT_TOKEN = "your-token-here"
       DISCORD_CHANNEL_ID = "123456789"   # optional: restrict to one channel

Usage
-----
    loom discord start --token $DISCORD_BOT_TOKEN [--channel 123456789]

Or programmatically:
    from loom.platform.discord.bot import LoomDiscordBot
    bot = LoomDiscordBot(model="MiniMax-M2.7", db_path="~/.loom/memory.db")
    bot.run(token="...")

Streaming strategy
------------------
- On message received: send "◌ Thinking..." placeholder message
- During TextChunk events: accumulate text, edit placeholder every ~0.8s
  (Discord rate-limit: ~5 edits/5s per message)
- On ToolBegin: append status line to placeholder
- On TurnDone: edit to final text (Discord 2000-char split if needed)

Confirm flow
------------
BlastRadiusMiddleware.confirm_fn is patched to send a Discord message with
Allow / Deny buttons and await the button interaction (60s timeout → deny).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING

try:
    import discord
    from discord import ButtonStyle, Interaction
    from discord.ui import Button, View
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Loom Discord bot requires discord.py.\n"
        "Install with:  pip install 'loom[discord]'"
    ) from exc

from loom.platform.cli.ui import TextChunk, ToolBegin, ToolEnd, TurnDone

if TYPE_CHECKING:
    from loom.platform.cli.main import LoomSession
    from loom.core.harness.middleware import ToolCall


# ---------------------------------------------------------------------------
# Confirm view (Allow / Deny buttons)
# ---------------------------------------------------------------------------

class _ConfirmView(View):
    """Discord UI view with Allow / Deny buttons for tool confirmation."""

    def __init__(self, timeout: float = 60.0) -> None:
        super().__init__(timeout=timeout)
        self._approved: bool | None = None
        self._done = asyncio.Event()

    @discord.ui.button(label="Allow", style=ButtonStyle.green, emoji="✅")
    async def allow_button(self, interaction: Interaction, button: Button) -> None:
        self._approved = True
        self._done.set()
        await interaction.response.edit_message(
            content="✅ **Allowed** — executing tool…", view=None
        )

    @discord.ui.button(label="Deny", style=ButtonStyle.red, emoji="❌")
    async def deny_button(self, interaction: Interaction, button: Button) -> None:
        self._approved = False
        self._done.set()
        await interaction.response.edit_message(
            content="❌ **Denied** — tool call blocked.", view=None
        )

    async def on_timeout(self) -> None:
        self._approved = False
        self._done.set()

    async def wait_decision(self) -> bool:
        await self._done.wait()
        return bool(self._approved)


# ---------------------------------------------------------------------------
# LoomDiscordBot
# ---------------------------------------------------------------------------

_EDIT_INTERVAL = 0.8   # seconds between message edits while streaming
_MAX_CHARS     = 2000  # Discord per-message limit


class LoomDiscordBot:
    """
    Runs a LoomSession behind a Discord bot.

    Args:
        model:       LLM model name (e.g. "MiniMax-M2.7").
        db_path:     Path to the SQLite memory database.
        channel_ids: If given, only respond in these channel IDs.
                     If empty, respond in any channel where the bot is @mentioned
                     or in DMs.
    """

    def __init__(
        self,
        model: str,
        db_path: str,
        channel_ids: list[int] | None = None,
    ) -> None:
        self._model = model
        self._db_path = str(Path(db_path).expanduser())
        self._allowed_channels: set[int] = set(channel_ids or [])

        # channel_id → LoomSession (started lazily)
        self._sessions: dict[int, "LoomSession"] = {}

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        self._setup_events()

    # ------------------------------------------------------------------
    # Discord event handlers
    # ------------------------------------------------------------------

    def _setup_events(self) -> None:
        client = self._client
        bot = self

        @client.event
        async def on_ready() -> None:
            assert client.user is not None
            print(f"[Loom Discord] Ready — logged in as {client.user} (id={client.user.id})")
            if bot._allowed_channels:
                print(f"[Loom Discord] Listening on channels: {bot._allowed_channels}")
            else:
                print("[Loom Discord] Listening for @mentions in any channel + DMs")

        @client.event
        async def on_message(message: discord.Message) -> None:
            if message.author.bot:
                return
            assert client.user is not None

            is_dm = isinstance(message.channel, discord.DMChannel)
            is_mentioned = client.user.mentioned_in(message)
            is_allowed_channel = message.channel.id in bot._allowed_channels

            if not (is_dm or is_mentioned or is_allowed_channel):
                return

            # Strip bot @mention from content
            content = message.content
            mention_str = f"<@{client.user.id}>"
            content = content.replace(mention_str, "").strip()
            if not content:
                return

            asyncio.ensure_future(bot._handle_message(message, content))

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _handle_message(
        self,
        message: discord.Message,
        content: str,
    ) -> None:
        session = await self._get_or_start_session(message.channel.id)

        # Handle /new and /sessions pseudo-commands
        if content.strip() == "/new":
            session = await self._new_session(message.channel.id)
            await message.channel.send("✨ Started a fresh session.")
            return

        # Post thinking placeholder
        status_msg = await message.channel.send("◌ Thinking…")

        text_buf = ""
        last_edit = 0.0

        try:
            async for event in session.stream_turn(content):
                if isinstance(event, TextChunk):
                    text_buf += event.text
                    now = time.monotonic()
                    if now - last_edit >= _EDIT_INTERVAL:
                        await _safe_edit(status_msg, text_buf + " ▌")
                        last_edit = now

                elif isinstance(event, ToolBegin):
                    args_str = ", ".join(
                        f"{k}={str(v)[:30]}" for k, v in event.args.items()
                    )[:80]
                    preview = f"\n`⟳ {event.name}({args_str})`"
                    await _safe_edit(status_msg, (text_buf or "◌ Thinking…") + preview)

                elif isinstance(event, TurnDone):
                    pass  # final edit happens below

        except Exception as exc:
            await _safe_edit(status_msg, f"❌ Error: {exc}")
            return

        # Final edit — render complete response (split if over 2000 chars)
        final = text_buf.strip() or "*(no response)*"
        if len(final) <= _MAX_CHARS:
            await _safe_edit(status_msg, final)
        else:
            await _safe_edit(status_msg, final[:_MAX_CHARS])
            remaining = final[_MAX_CHARS:]
            while remaining:
                chunk, remaining = remaining[:_MAX_CHARS], remaining[_MAX_CHARS:]
                await message.channel.send(chunk)

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def _get_or_start_session(self, channel_id: int) -> "LoomSession":
        if channel_id not in self._sessions:
            await self._start_session(channel_id)
        return self._sessions[channel_id]

    async def _start_session(self, channel_id: int) -> "LoomSession":
        from loom.platform.cli.main import LoomSession
        from loom.core.harness.middleware import BlastRadiusMiddleware

        session = LoomSession(model=self._model, db_path=self._db_path)
        await session.start()

        # Patch confirm_fn to use Discord buttons
        confirm_fn = self._make_confirm_fn(channel_id)
        for mw in session._pipeline._middlewares:
            if isinstance(mw, BlastRadiusMiddleware):
                mw._confirm = confirm_fn
                break

        self._sessions[channel_id] = session
        return session

    async def _new_session(self, channel_id: int) -> "LoomSession":
        old = self._sessions.pop(channel_id, None)
        if old is not None:
            await old.stop()
        return await self._start_session(channel_id)

    # ------------------------------------------------------------------
    # Tool confirm via Discord buttons
    # ------------------------------------------------------------------

    def _make_confirm_fn(self, channel_id: int):
        client = self._client

        async def _confirm(call: "ToolCall") -> bool:
            channel = client.get_channel(channel_id)
            if channel is None:
                return False  # can't confirm — deny

            args_preview = "  ".join(
                f"{k}={str(v)[:40]}" for k, v in call.args.items()
            )[:120]

            trust = call.trust_level.plain   # "GUARDED" or "CRITICAL"
            color = "🟡" if trust == "GUARDED" else "🔴"
            view = _ConfirmView(timeout=60.0)

            await channel.send(
                f"{color} **{trust}** — tool confirmation required\n"
                f"**`{call.tool_name}`**\n"
                f"```\n{args_preview}\n```\n"
                f"*Timeout in 60s → auto-deny*",
                view=view,
            )
            return await view.wait_decision()

        return _confirm

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self, token: str) -> None:
        """Blocking entry-point — starts the bot event loop."""
        self._client.run(token)

    async def run_async(self, token: str) -> None:
        """Async entry-point for embedding in an existing event loop."""
        async with self._client:
            await self._client.start(token)

    async def close(self) -> None:
        """Stop all sessions and disconnect the bot."""
        for session in list(self._sessions.values()):
            try:
                await session.stop()
            except Exception:
                pass
        self._sessions.clear()
        await self._client.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _safe_edit(message: discord.Message, content: str) -> None:
    """Edit a Discord message, silently ignoring rate-limit / unknown errors."""
    try:
        await message.edit(content=content[:_MAX_CHARS])
    except discord.HTTPException:
        pass
