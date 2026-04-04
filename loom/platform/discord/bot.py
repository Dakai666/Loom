"""
Loom Discord Bot — full Discord frontend for a LoomSession.

Architecture
------------
Each conversation lives in its own Discord **thread**.  The main channel is
the lobby — sending a message there (or @mentioning the bot) automatically
creates a new thread and starts a fresh LoomSession inside it.

  Main channel (lobby)
  ├─ 🧵 "Help me analyse this code…"   ← LoomSession A
  ├─ 🧵 "Today's work plan"             ← LoomSession B
  └─ 🧵 "Architecture review"           ← LoomSession C  (current)

Each LoomSession is keyed to its thread ID, so multiple conversations can
run in parallel without interfering.

Security
--------
``allowed_user_ids``  — if set, the bot silently ignores every other user.
``allowed_channel_ids`` — if set, the bot only operates in those channels
                          (and threads that belong to them).

Setup
-----
1. Create a Discord application & bot at https://discord.com/developers/applications
2. Enable "Message Content Intent" under Bot → Privileged Gateway Intents
3. Copy the bot token and IDs to .env:
       DISCORD_BOT_TOKEN   = "..."
       DISCORD_CHANNEL_ID  = "123456789"
       DISCORD_USER_ID     = "987654321"   # optional: restrict to one user

Usage
-----
    loom discord start --token $DISCORD_BOT_TOKEN --channel $DISCORD_CHANNEL_ID

Streaming strategy
------------------
- On message received: create / find thread, send "◌ Thinking..." placeholder
- During TextChunk events: accumulate text, edit placeholder every ~0.8s
  (Discord rate-limit: ~5 edits/5s per message)
- On ToolBegin: append status line to placeholder
- On TurnDone: edit to final text (split at 2000 chars if needed)

Confirm flow
------------
BlastRadiusMiddleware.confirm_fn is patched to send an Allow / Deny button
message in the thread and await the button interaction (60s timeout → deny).
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

from loom.platform.cli.ui import TextChunk, ToolBegin, ToolEnd, TurnDone, TurnPaused

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
_THREAD_ARCHIVE_MINUTES = 1440  # 24h auto-archive for threads


class LoomDiscordBot:
    """
    Runs LoomSessions behind a Discord bot, one session per thread.

    Args:
        model:             LLM model name (e.g. "MiniMax-M2.7").
        db_path:           Path to the SQLite memory database.
        channel_ids:       If given, only operate in these channel IDs
                           (and threads that belong to them).
        allowed_user_ids:  If given, silently ignore all other users.
    """

    def __init__(
        self,
        model: str,
        db_path: str,
        channel_ids: list[int] | None = None,
        allowed_user_ids: list[int] | None = None,
    ) -> None:
        self._model = model
        self._db_path = str(Path(db_path).expanduser())
        self._allowed_channels: set[int] = set(channel_ids or [])
        self._allowed_users: set[int] = set(allowed_user_ids or [])

        # thread_id → LoomSession
        self._sessions: dict[int, "LoomSession"] = {}
        # thread_id → currently running turn Task
        self._running_turns: dict[int, asyncio.Task] = {}

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
            if bot._allowed_users:
                print(f"[Loom Discord] Accepting messages from user IDs: {bot._allowed_users}")
            if bot._allowed_channels:
                print(f"[Loom Discord] Operating in channels: {bot._allowed_channels}")

        @client.event
        async def on_message(message: discord.Message) -> None:
            if message.author.bot:
                return
            assert client.user is not None

            # ── User ID gate ──────────────────────────────────────────
            if bot._allowed_users and message.author.id not in bot._allowed_users:
                return

            # ── Determine if this is a thread or main channel message ─
            is_thread = isinstance(
                message.channel,
                (discord.Thread, discord.DMChannel),
            )
            is_dm = isinstance(message.channel, discord.DMChannel)
            is_mentioned = client.user.mentioned_in(message)

            # Resolve the "parent" channel ID for channel gating
            if is_thread and not is_dm:
                parent_id = message.channel.parent_id  # type: ignore[union-attr]
            else:
                parent_id = message.channel.id

            # ── Channel ID gate ───────────────────────────────────────
            if bot._allowed_channels:
                if not is_dm and parent_id not in bot._allowed_channels:
                    return
            elif not (is_dm or is_mentioned):
                # No channel restriction: only respond to @mentions or DMs
                return

            # Strip bot @mention from content
            content = message.content.replace(f"<@{client.user.id}>", "").strip()
            if not content:
                return

            task = asyncio.ensure_future(bot._handle_message(message, content, is_thread))
            # Track running turn by thread/channel id
            key = message.channel.id
            bot._running_turns[key] = task
            task.add_done_callback(lambda _t, k=key: bot._running_turns.pop(k, None))

    # ------------------------------------------------------------------
    # Routing — main channel vs. thread
    # ------------------------------------------------------------------

    async def _handle_message(
        self,
        message: discord.Message,
        content: str,
        is_thread: bool,
    ) -> None:
        if content.startswith("/"):
            # Slash commands work in both contexts
            if is_thread:
                session = await self._get_thread_session(message.channel)  # type: ignore[arg-type]
            else:
                session = None  # some commands don't need a session
            await self._handle_slash(message, content.strip(), session, is_thread)
            return

        if is_thread:
            # Message inside an existing thread → continue that session
            session = await self._get_thread_session(message.channel)  # type: ignore[arg-type]
            await self._run_turn(message, content, session)
        else:
            # Message in main channel → create a new thread and start there
            thread = await self._create_session_thread(message, content)
            session = await self._start_session(thread.id)
            # Re-route to thread (message is already the first user turn)
            fake_msg = await thread.send(f"> {content[:100]}")  # echo starter
            await self._run_turn(fake_msg, content, session)

    # ------------------------------------------------------------------
    # Thread / session helpers
    # ------------------------------------------------------------------

    async def _create_session_thread(
        self,
        message: discord.Message,
        first_content: str,
    ) -> discord.Thread:
        """Create a new thread from a main-channel message."""
        thread_name = first_content[:50].strip() or "Loom session"
        thread = await message.create_thread(
            name=thread_name,
            auto_archive_duration=_THREAD_ARCHIVE_MINUTES,
        )
        return thread

    async def _get_thread_session(self, thread: discord.Thread) -> "LoomSession":
        """Get or lazily start a session for an existing thread."""
        if thread.id not in self._sessions:
            await self._start_session(thread.id)
        return self._sessions[thread.id]

    async def _start_session(self, thread_id: int) -> "LoomSession":
        from loom.platform.cli.main import LoomSession
        from loom.core.harness.middleware import BlastRadiusMiddleware

        session = LoomSession(model=self._model, db_path=self._db_path)
        await session.start()

        confirm_fn = self._make_confirm_fn(thread_id)
        for mw in session._pipeline._middlewares:
            if isinstance(mw, BlastRadiusMiddleware):
                mw._confirm = confirm_fn
                break

        self._sessions[thread_id] = session
        return session

    async def _close_session(self, thread_id: int) -> None:
        session = self._sessions.pop(thread_id, None)
        if session:
            try:
                await session.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    async def _handle_slash(
        self,
        message: discord.Message,
        cmd: str,
        session: "LoomSession | None",
        is_thread: bool,
    ) -> None:
        parts = cmd.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        # Commands that require being in a thread
        _needs_session = {"/think", "/compact", "/verbose", "/pause", "/stop", "/budget"}
        if command in _needs_session and not is_thread:
            await message.channel.send(
                f"`{command}` must be used inside a session thread.  "
                "Start one by sending a message here."
            )
            return

        if command == "/new":
            if is_thread:
                # Create a new sibling thread from the parent channel
                parent = message.channel.parent  # type: ignore[union-attr]
                if parent is None:
                    await message.channel.send("Cannot create a new thread here.")
                    return
                new_thread = await parent.create_thread(
                    name="New session",
                    auto_archive_duration=_THREAD_ARCHIVE_MINUTES,
                    type=discord.ChannelType.public_thread,
                )
                await self._start_session(new_thread.id)
                await new_thread.send("✨ New session started. Send your first message here.")
                await message.channel.send(f"✨ Opened new session → {new_thread.mention}")
            else:
                await message.channel.send(
                    "Send any message here to start a new session thread."
                )

        elif command == "/sessions":
            assert session is not None
            from loom.core.memory.session_log import SessionLog as _SL
            async with session._store.connect() as conn:
                rows = await _SL(conn).list_sessions(limit=10)
            if not rows:
                await message.channel.send("*(no saved sessions)*")
                return
            lines = ["**Recent sessions:**\n"]
            for i, r in enumerate(rows, 1):
                title = r.get("title") or "(untitled)"
                sid = r["session_id"][:8]
                active = " ◀ current" if r["session_id"] == session.session_id else ""
                lines.append(f"`{i}.` `{sid}` — {title}{active}")
            await message.channel.send("\n".join(lines))

        elif command == "/think":
            assert session is not None
            think = session._last_think
            if think:
                body = think[:1800] + ("\n*(truncated)*" if len(think) > 1800 else "")
                await message.channel.send(f"**Reasoning chain:**\n```\n{body}\n```")
            else:
                await message.channel.send("*(no reasoning chain captured for the last turn)*")

        elif command == "/compact":
            assert session is not None
            pct = session.budget.usage_fraction * 100
            msg = await message.channel.send(f"⏳ Compacting context ({pct:.1f}% used)…")
            await session._smart_compact()
            await _safe_edit(msg, "✅ Context compacted.")

        elif command == "/personality":
            assert session is not None
            if not arg:
                p = session.current_personality
                avail = session._stack.available_personalities()
                await message.channel.send(
                    f"Active: **{p or '(none)'}**  |  "
                    f"Available: `{'`, `'.join(avail) or '(none)'}`"
                )
            elif arg == "off":
                session.switch_personality("off")
                await message.channel.send("Personality cleared.")
            else:
                ok = session.switch_personality(arg)
                if ok:
                    await message.channel.send(f"Personality → **{arg}**")
                else:
                    avail = session._stack.available_personalities()
                    await message.channel.send(
                        f"❌ Unknown personality `{arg}`. "
                        f"Available: `{'`, `'.join(avail) or '(none)'}`"
                    )

        elif command == "/verbose":
            assert session is not None
            session._discord_verbose = not getattr(session, "_discord_verbose", False)
            state = "on" if session._discord_verbose else "off"
            await message.channel.send(f"Tool output verbosity: **{state}**")

        elif command == "/pause":
            assert session is not None
            session.hitl_mode = not session.hitl_mode
            state = "on" if session.hitl_mode else "off"
            extra = (
                "\nAgent will pause after each tool batch — reply `r` to resume, "
                "`c` to cancel, or send a redirect message."
                if session.hitl_mode else ""
            )
            await message.channel.send(f"HITL pause mode: **{state}**{extra}")

        elif command == "/stop":
            task = self._running_turns.get(message.channel.id)
            if task and not task.done():
                task.cancel()
                await message.channel.send("🛑 Stopped.")
            else:
                await message.channel.send("*(nothing is running)*")

        elif command == "/budget":
            assert session is not None
            pct = session.budget.usage_fraction * 100
            used = session.budget.used_tokens
            total = session.budget.total_tokens
            bar_filled = int(pct / 5)
            bar = "█" * bar_filled + "░" * (20 - bar_filled)
            await message.channel.send(
                f"**Context Budget**\n"
                f"`{bar}` {pct:.1f}%\n"
                f"`{used:,}` / `{total:,}` tokens"
            )

        elif command == "/help":
            await message.channel.send(
                "**Loom commands**\n\n"
                "`/new` — Open a new session thread\n"
                "`/sessions` — List recent sessions\n"
                "`/personality [name]` — Switch cognitive persona\n"
                "`/personality off` — Remove active persona\n"
                "`/think` — View last turn's reasoning chain\n"
                "`/compact` — Compress older context\n"
                "`/verbose` — Toggle tool output verbosity\n"
                "`/pause` — Toggle HITL auto-pause after each tool batch\n"
                "`/stop` — Immediately cancel the current running turn\n"
                "`/budget` — Show context token usage\n"
                "`/help` — Show this message\n\n"
                "Personalities: `adversarial` · `minimalist` · `architect` · `researcher` · `operator`\n\n"
                "*Send any message in the main channel to start a new session thread.*"
            )

        else:
            await message.channel.send(
                f"Unknown command `{command}`. Type `/help` for the command list."
            )

    # ------------------------------------------------------------------
    # Agent turn
    # ------------------------------------------------------------------

    async def _run_turn(
        self,
        message: discord.Message,
        content: str,
        session: "LoomSession",
    ) -> None:
        status_msg = await message.channel.send("◌ Thinking…")

        text_buf = ""
        last_edit = 0.0
        verbose = getattr(session, "_discord_verbose", False)

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

                elif isinstance(event, ToolEnd):
                    if verbose:
                        status = "✓" if event.success else "✗"
                        line = f"\n`{status} {event.name} ({event.duration_ms:.0f}ms)`"
                        if event.output:
                            line += f"\n```\n{event.output[:200]}\n```"
                        await _safe_edit(status_msg, (text_buf or "…") + line)

                elif isinstance(event, TurnPaused):
                    await _safe_edit(
                        status_msg,
                        f"{text_buf or '…'}\n\n"
                        f"⏸ **Paused** after {event.tool_count_so_far} tool call(s).\n"
                        "Reply `r` to resume · `c` to cancel · or send a redirect message",
                    )

                    def _pause_check(m: discord.Message) -> bool:
                        return (
                            m.channel.id == message.channel.id
                            and (
                                not self._allowed_users
                                or m.author.id in self._allowed_users
                            )
                            and not m.author.bot
                        )

                    try:
                        reply = await self._client.wait_for(
                            "message", check=_pause_check, timeout=120.0
                        )
                        raw = reply.content.strip()
                        if raw.lower() in ("c", "cancel"):
                            session.cancel()
                        elif raw.lower() in ("r", "resume", ""):
                            session.resume()
                        else:
                            session.resume_with(raw)
                            text_buf += f"\n*(redirected: {raw[:60]})*"
                    except asyncio.TimeoutError:
                        session.cancel()
                        await message.channel.send("*(pause timed out — turn cancelled)*")

                elif isinstance(event, TurnDone):
                    pass

        except asyncio.CancelledError:
            final_so_far = text_buf.strip()
            if final_so_far:
                await _safe_edit(status_msg, final_so_far + "\n\n🛑 *(stopped)*")
            else:
                await _safe_edit(status_msg, "🛑 *(stopped)*")
            raise

        except Exception as exc:
            await _safe_edit(status_msg, f"❌ Error: {exc}")
            return

        # Final edit
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
    # Tool confirm via Discord buttons
    # ------------------------------------------------------------------

    def _make_confirm_fn(self, thread_id: int):
        client = self._client

        async def _confirm(call: "ToolCall") -> bool:
            channel = client.get_channel(thread_id)
            if channel is None:
                return False

            args_preview = "  ".join(
                f"{k}={str(v)[:40]}" for k, v in call.args.items()
            )[:120]
            trust = call.trust_level.plain
            color = "🟡" if trust == "GUARDED" else "🔴"
            view = _ConfirmView(timeout=60.0)

            await channel.send(
                f"{color} **{trust}** — tool confirmation required\n"
                f"**`{call.tool_name}`**\n"
                f"```\n{args_preview}\n```\n"
                f"*Timeout 60s → auto-deny*",
                view=view,
            )
            return await view.wait_decision()

        return _confirm

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self, token: str) -> None:
        """Blocking entry-point."""
        self._client.run(token)

    async def run_async(self, token: str) -> None:
        """Async entry-point for embedding in an existing event loop."""
        async with self._client:
            await self._client.start(token)

    async def close(self) -> None:
        """Stop all sessions and disconnect."""
        for tid in list(self._sessions):
            await self._close_session(tid)
        await self._client.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _safe_edit(message: discord.Message, content: str) -> None:
    try:
        await message.edit(content=content[:_MAX_CHARS])
    except discord.HTTPException:
        pass
