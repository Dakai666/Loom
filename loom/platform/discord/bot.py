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
BlastRadiusMiddleware.confirm_fn is patched to send a four-button message
(Allow / Lease / Auto / Deny) in the thread and await the button interaction
(60s timeout → deny).  Lease and Auto decisions post a follow-up message
showing the TTL or grant scope.
"""

from __future__ import annotations

import asyncio
import json
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

from loom.core.harness.scope import ConfirmDecision
from loom.core.events import (
    EnvelopeStarted, EnvelopeUpdated, EnvelopeCompleted,
    ExecutionEnvelopeView,
)
from loom.platform.cli.ui import (
    ActionRolledBack, ActionStateChange,
    CompressDone, TextChunk, ThinkCollapsed, ToolBegin, ToolEnd,
    TurnDone, TurnDropped, TurnPaused,
)
from loom.platform.discord.tools import make_send_discord_file_tool, make_send_discord_embed_tool

if TYPE_CHECKING:
    from loom.core.session import LoomSession
    from loom.core.harness.middleware import ToolCall


# ---------------------------------------------------------------------------
# Confirm view (y / s / a / N buttons)
# ---------------------------------------------------------------------------

class _ConfirmView(View):
    """Discord UI view with Allow / Lease / Auto / Deny buttons for tool confirmation."""

    def __init__(self, timeout: float = 60.0) -> None:
        super().__init__(timeout=timeout)
        self._decision: ConfirmDecision | None = None
        self._done = asyncio.Event()

    @discord.ui.button(label="Allow (y)", style=ButtonStyle.green, emoji="✅")
    async def allow_button(self, interaction: Interaction, button: Button) -> None:
        self._decision = ConfirmDecision.ONCE
        self._done.set()
        await interaction.response.edit_message(
            content="✅ **Allowed** — executing tool…", view=None
        )

    @discord.ui.button(label="Lease (s)", style=ButtonStyle.blurple, emoji="⏱️")
    async def lease_button(self, interaction: Interaction, button: Button) -> None:
        self._decision = ConfirmDecision.SCOPE
        self._done.set()
        await interaction.response.edit_message(
            content="⏱️ **Lease granted** — executing tool…", view=None
        )

    @discord.ui.button(label="Auto (a)", style=ButtonStyle.grey, emoji="⚡")
    async def auto_button(self, interaction: Interaction, button: Button) -> None:
        self._decision = ConfirmDecision.AUTO
        self._done.set()
        await interaction.response.edit_message(
            content="⚡ **Auto-approve granted** — executing tool…", view=None
        )

    @discord.ui.button(label="Deny (N)", style=ButtonStyle.red, emoji="❌")
    async def deny_button(self, interaction: Interaction, button: Button) -> None:
        self._decision = ConfirmDecision.DENY
        self._done.set()
        await interaction.response.edit_message(
            content="❌ **Denied** — tool call blocked.", view=None
        )

    async def on_timeout(self) -> None:
        self._decision = ConfirmDecision.DENY
        self._done.set()

    async def wait_decision(self) -> ConfirmDecision:
        await self._done.wait()
        return self._decision if self._decision is not None else ConfirmDecision.DENY


# ---------------------------------------------------------------------------
# LoomDiscordBot
# ---------------------------------------------------------------------------

_MAX_CHARS     = 2000  # Discord per-message limit
_THREAD_ARCHIVE_MINUTES = 1440  # 24h auto-archive for threads

# ── Envelope display helpers (Issue #110) ────────────────────────────────

_ENVELOPE_STATE_ICONS: dict[str, str] = {
    "declared": "·", "authorized": "·", "prepared": "·",
    "executing": "⟳", "observed": "✓", "validated": "✓",
    "committed": "✓", "memorialized": "✓",
    "denied": "⊘", "aborted": "⊘", "timed_out": "✗",
    "reverting": "↩", "reverted": "↩",
}


def _format_envelope_status(view: ExecutionEnvelopeView) -> str:
    """Format an envelope view into a compact Discord status block."""
    lines: list[str] = []
    # Header
    header = f"-# Envelope {view.envelope_id} · {view.node_count} actions"
    if view.parallel_groups > 1:
        header += f" · {view.parallel_groups} parallel groups"
    if view.status == "failed":
        header += " · **failed**"
    elif view.status == "completed":
        header += f" · completed {view.elapsed_ms / 1000:.1f}s"
    lines.append(header)

    # Level list with state icons
    # Only show L{n} prefix when there are multiple levels (actual parallelism tiers)
    show_level_prefix = len(view.levels) > 1
    for level_idx, level_nodes in enumerate(view.levels):
        level_parts: list[str] = []
        for node_id in level_nodes:
            node = next((n for n in view.nodes if n.node_id == node_id), None)
            if node:
                icon = _ENVELOPE_STATE_ICONS.get(node.state, "?")
                name = node.tool_name
                extra = ""
                if node.error_snippet:
                    extra = f" ({node.error_snippet[:40]})"
                level_parts.append(f"{icon} {name}{extra}")
        prefix = f"L{level_idx}  " if show_level_prefix else ""
        lines.append(f"-# {prefix}{'  '.join(level_parts)}")

    return "\n".join(lines)


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
        # Resolve None to the default model from loom.toml
        if model is None:
            from loom.core.cognition.router import get_default_model
            model = get_default_model()
        self._model = model
        self._db_path = str(Path(db_path).expanduser())
        self._allowed_channels: set[int] = set(channel_ids or [])
        self._allowed_users: set[int] = set(allowed_user_ids or [])

        # thread_id → LoomSession (in-memory, cleared on restart)
        self._sessions: dict[int, "LoomSession"] = {}
        # thread_id → currently running turn Task
        self._running_turns: dict[int, asyncio.Task] = {}
        # thread_id → currently active confirmation message (Allow/Deny prompt)
        self._active_confirmations: dict[int, discord.Message] = {}
        # Turn summary display mode: "off" | "on" | "detail"
        self._summary_mode: str = "on"

        # Persistent thread → session_id mapping so existing threads resume
        # their context after a bot restart.
        # Stored at ~/.loom/discord_threads.json as {str(thread_id): session_id}
        self._thread_map_path = Path("~/.loom/discord_threads.json").expanduser()
        self._thread_map: dict[str, str] = self._load_thread_map()

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
            if not content and not message.attachments:
                return

            # Cancel any in-progress turn for this channel before starting a new one.
            # Without this, concurrent stream_turn() calls on the same session corrupt
            # message history (race between Pass-2 trim and tool_result append → 2013).
            key = message.channel.id
            existing = bot._running_turns.get(key)
            if existing and not existing.done():
                existing.cancel()
            task = asyncio.ensure_future(bot._handle_message(message, content, is_thread))
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
        else:
            # Message in main channel → create a new thread and start there
            thread = await self._create_session_thread(message, content)
            session = await self._start_session(thread.id)

        # Process attachments
        if getattr(message, "attachments", None):
            dl_dir = session.workspace / ".discord_downloads"
            dl_dir.mkdir(parents=True, exist_ok=True)
            attachment_notes = []
            for att in message.attachments:
                dest = dl_dir / att.filename
                try:
                    await att.save(dest)
                    attachment_notes.append(f"- {att.filename} (saved to .discord_downloads/{att.filename})")
                except Exception as e:
                    attachment_notes.append(f"- {att.filename} (failed to download: {e})")
            
            if attachment_notes:
                notes_str = "\n".join(attachment_notes)
                content += f"\n\n[系統通知：使用者上傳了附件]\n{notes_str}"
            
            # Start message if empty
            if not content.strip():
                content = "[系統通知：使用者僅上傳了附件]"

        if is_thread:
            await self._run_turn(message, content, session)
        else:
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

    def _load_thread_map(self) -> dict[str, str]:
        """Load persisted thread_id → session_id mapping from disk."""
        try:
            if self._thread_map_path.exists():
                return json.loads(self._thread_map_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_thread_map(self) -> None:
        """Persist the current thread → session mapping to disk."""
        try:
            self._thread_map_path.parent.mkdir(parents=True, exist_ok=True)
            self._thread_map_path.write_text(
                json.dumps(self._thread_map, indent=2), encoding="utf-8"
            )
        except Exception:
            pass  # never block on a save failure

    async def _start_session(self, thread_id: int) -> "LoomSession":
        from loom.core.session import LoomSession
        from loom.core.harness.middleware import BlastRadiusMiddleware

        # Resume the previous session for this thread if one was recorded.
        resume_id = self._thread_map.get(str(thread_id))
        session = LoomSession(
            model=self._model,
            db_path=self._db_path,
            resume_session_id=resume_id,
        )
        await session.start()

        # Persist thread → session mapping immediately after start so a crash
        # or clean restart can still find this thread's context.
        self._thread_map[str(thread_id)] = session.session_id
        self._save_thread_map()

        # Inject Discord tools
        session.registry.register(make_send_discord_file_tool(self._client, thread_id, session.workspace))
        session.registry.register(make_send_discord_embed_tool(self._client, thread_id))
        session.perm.authorize("send_discord_file")
        session.perm.authorize("send_discord_embed")

        confirm_fn = self._make_confirm_fn(thread_id)
        for mw in session._pipeline._middlewares:
            if isinstance(mw, BlastRadiusMiddleware):
                mw._confirm = confirm_fn
                break
        # Also patch skill check approval so it uses Discord confirm buttons
        session._confirm_fn = confirm_fn

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
        _needs_session = {"/think", "/compact", "/pause", "/stop", "/budget", "/auto", "/scope", "/summary"}
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

        elif command == "/model":
            assert session is not None
            if not arg:
                providers = ", ".join(session.router.providers)
                await message.channel.send(
                    f"Current model: **{session.model}**  providers: `{providers}`\n"
                    "Prefixes: `MiniMax-*` · `claude-*` · `ollama/<name>` · `lmstudio/<name>`"
                )
            else:
                ok = session.set_model(arg)
                if ok:
                    await message.channel.send(f"Model switched to: **{arg}**")
                else:
                    await message.channel.send(
                        f"Cannot switch to `{arg}` — prefix not recognised or provider "
                        "not registered (check `.env` key or `loom.toml [providers.*]`)."
                    )

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

        elif command == "/auto":
            assert session is not None
            if not session._strict_sandbox:
                await message.channel.send(
                    "❌ `/auto` requires `strict_sandbox = true` in `loom.toml`.\n"
                    "Without workspace confinement, auto-approving `run_bash` "
                    "would grant unrestricted shell access."
                )
            else:
                session.perm.exec_auto = not session.perm.exec_auto
                state = "on" if session.perm.exec_auto else "off"
                if session.perm.exec_auto:
                    await message.channel.send(
                        f"✅ Exec auto-approve: **{state}** — `run_bash` pre-authorized within workspace.\n"
                        "Absolute paths that escape the workspace still require confirmation."
                    )
                else:
                    await message.channel.send(
                        f"🔒 Exec auto-approve: **{state}** — `run_bash` will confirm every call."
                    )

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
                "`/model` — Show current model + registered providers\n"
                "`/model <name>` — Switch model  e.g. `ollama/llama3.2`  `claude-sonnet-4-6`\n"
                "`/personality [name]` — Switch cognitive persona\n"
                "`/personality off` — Remove active persona\n"
                "`/think` — View last turn's reasoning chain\n"
                "`/compact` — Compress older context\n"
                "`/auto` — Toggle run_bash auto-approve (requires strict_sandbox)\n"
                "`/pause` — Toggle HITL auto-pause after each tool batch\n"
                "`/stop` — Immediately cancel the current running turn\n"
                "`/budget` — Show context token usage\n"
                "`/scope` — Manage scope grants: `list` · `revoke <id>` · `clear`\n"
                "`/summary` — Turn summary mode: `off` · `on` · `detail`\n"
                "`/help` — Show this message\n\n"
                "Personalities: `adversarial` · `minimalist` · `architect` · `researcher` · `operator`\n\n"
                "*Send any message in the main channel to start a new session thread.*"
            )

        elif command == "/summary":
            valid_modes = ("off", "on", "detail")
            if not arg:
                await message.channel.send(
                    f"Turn summary mode: **{self._summary_mode}**\n"
                    f"Usage: `/summary off` · `/summary on` · `/summary detail`"
                )
            elif arg.lower() in valid_modes:
                self._summary_mode = arg.lower()
                await message.channel.send(f"Turn summary mode → **{self._summary_mode}**")
            else:
                await message.channel.send(
                    f"Unknown mode `{arg}`. Use: `off` · `on` · `detail`"
                )

        elif command == "/scope":
            assert session is not None
            sub = arg.split(maxsplit=1)
            subcmd = sub[0].lower() if sub else "list"
            subarg = sub[1].strip() if len(sub) > 1 else ""

            if subcmd == "list":
                now = time.time()
                active = [
                    (i, g) for i, g in enumerate(session.perm.grants)
                    if g.valid_until <= 0 or g.valid_until > now
                ]
                if not active:
                    await message.channel.send("*(no active scope grants)*")
                else:
                    lines = [f"**Active Scope Grants ({len(active)})**\n```"]
                    lines.append(f"{'ID':>3}  {'Tool':<16} {'Selector':<20} {'TTL':<10}")
                    lines.append(f"{'─'*3}  {'─'*16} {'─'*20} {'─'*10}")
                    for idx, g in active:
                        if g.valid_until <= 0:
                            ttl = "∞ (auto)" if g.source == "auto_approve" else "∞ (perm)"
                        else:
                            remaining = int(g.valid_until - now)
                            m, s = divmod(remaining, 60)
                            ttl = f"{m}m {s:02d}s"
                        tool = g.action if g.action != "*" else g.resource
                        lines.append(f"{idx:>3}  {tool:<16} {g.selector:<20} {ttl:<10}")
                    lines.append("```")
                    await message.channel.send("\n".join(lines))

            elif subcmd == "revoke":
                if not subarg.isdigit():
                    await message.channel.send("Usage: `/scope revoke <id>`")
                else:
                    grant_id = int(subarg)
                    if 0 <= grant_id < len(session.perm.grants):
                        g = session.perm.grants[grant_id]
                        tool = g.action if g.action != "*" else g.resource
                        session.perm.revoke_matching(lambda x, _g=g: x is _g)
                        await message.channel.send(
                            f"✅ Revoked grant #{grant_id}: `{tool}` · {g.selector}"
                        )
                    else:
                        await message.channel.send(
                            f"❌ Invalid grant ID `{grant_id}`. Use `/scope list` to see valid IDs."
                        )

            elif subcmd == "clear":
                count = len(session.perm.grants)
                session.perm.grants.clear()
                session.perm._usage.clear()
                await message.channel.send(f"🧹 Cleared {count} scope grant(s).")

            else:
                await message.channel.send(
                    "Usage: `/scope list` · `/scope revoke <id>` · `/scope clear`"
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
        """
        Run one agent turn with a split display strategy:

        - status_msg (edit-based): tool activity log only — sparse edits, no
          text streaming, so Markdown renders correctly and URL embeds don't flicker.
        - response (send-once): complete LLM text sent as a fresh new message
          after the turn finishes — Markdown and embeds render properly.
        - Reaction ⚙️ on the user's message: immediate "received" acknowledgement.
        - channel.typing(): "Bot is typing…" indicator while the turn runs.
        """
        # ── Acknowledge receipt ───────────────────────────────────────────
        try:
            await message.add_reaction("⚙️")
        except discord.HTTPException:
            pass

        # Placeholder shown while working; deleted if no tools were used.
        status_msg = await message.channel.send("-# ◌ working…")

        tool_buf = ""       # accumulates tool activity lines (edited into status_msg)
        narration_buf = ""  # accumulates LLM text; flushed as send-once before each tool
        _envelope_active = False    # True once we receive envelope events (suppresses old ToolBegin/End display)
        _last_envelope_view: ExecutionEnvelopeView | None = None
        _last_envelope_edit: float = 0.0  # monotonic timestamp of last envelope edit (debounce)
        _ENVELOPE_DEBOUNCE_S = 0.5
        # Turn-level stats for summary line
        _envelope_count = 0
        _total_actions = 0
        _total_failures = 0
        _total_elapsed_ms = 0.0
        _had_pause = False
        _had_rollback = False

        # ── Run turn with typing indicator ────────────────────────────────
        async with message.channel.typing():
            try:
                async for event in session.stream_turn(content):
                    if isinstance(event, TextChunk):
                        narration_buf += event.text

                    elif isinstance(event, ThinkCollapsed):
                        # Send as a persistent message so it isn't overwritten
                        # by subsequent envelope edits.
                        await message.channel.send(f"-# 💭 {event.summary}")

                    elif isinstance(event, EnvelopeStarted):
                        _envelope_active = True
                        _last_envelope_view = event.envelope
                        # Flush narration before envelope
                        narration = narration_buf.strip()
                        narration_buf = ""
                        if len(narration) >= 10:
                            await message.channel.send(f"⬥ {narration}")
                        tool_buf = _format_envelope_status(event.envelope)
                        await _safe_edit(status_msg, tool_buf.lstrip())
                        _last_envelope_edit = time.monotonic()

                    elif isinstance(event, EnvelopeUpdated):
                        _last_envelope_view = event.envelope
                        now = time.monotonic()
                        if now - _last_envelope_edit >= _ENVELOPE_DEBOUNCE_S:
                            tool_buf = _format_envelope_status(event.envelope)
                            await _safe_edit(status_msg, tool_buf.lstrip())
                            _last_envelope_edit = now

                    elif isinstance(event, EnvelopeCompleted):
                        _last_envelope_view = event.envelope
                        _envelope_active = False
                        # Accumulate turn-level stats
                        _envelope_count += 1
                        v = event.envelope
                        _total_actions += v.node_count
                        _total_elapsed_ms += v.elapsed_ms
                        _total_failures += sum(
                            1 for n in v.nodes
                            if n.state in ("denied", "aborted", "timed_out", "reverted")
                        )
                        # Freeze completed envelope as a permanent message
                        frozen = _format_envelope_status(event.envelope)
                        await _safe_edit(status_msg, frozen.lstrip())
                        # Create a fresh status_msg for the next envelope
                        status_msg = await message.channel.send("-# ◌ working…")
                        tool_buf = ""
                        _last_envelope_edit = time.monotonic()

                    elif isinstance(event, ToolBegin):
                        # Flush narration before tool activity (send-once, ⬥ prefix).
                        narration = narration_buf.strip()
                        narration_buf = ""
                        if len(narration) >= 10:
                            await message.channel.send(f"⬥ {narration}")

                        if not _envelope_active:
                            # Build tool line with kimaki-style symbol:
                            #   ◼︎ for file writes, ┣ for everything else.
                            if event.args:
                                first_val = next(iter(event.args.values()), "")
                                primary = str(first_val).replace("\n", "↵")[:120]
                                args_str = f'"{primary}"' if primary else ""
                            else:
                                args_str = ""
                            symbol = "◼︎" if event.name in ("write_file",) else "┣"
                            tool_line = (
                                f"\n{symbol} {event.name}"
                                + (f" — {args_str}" if args_str else "")
                            )
                            tool_buf += tool_line
                            await _safe_edit(status_msg, tool_buf.lstrip())

                    elif isinstance(event, ToolEnd):
                        if not _envelope_active:
                            if event.success:
                                tool_buf += f" ✓ {event.duration_ms:.0f}ms"
                            else:
                                err = (
                                    event.output[:80].replace("\n", " ")
                                    if event.output else "failed"
                                )
                                tool_buf += f" ✗ {err}"
                            await _safe_edit(status_msg, tool_buf.lstrip())

                    elif isinstance(event, TurnPaused):
                        _had_pause = True
                        pause_body = (
                            (tool_buf.lstrip() + "\n\n" if tool_buf else "")
                            + f"⏸ **Paused** after {event.tool_count_so_far} tool call(s).\n"
                            "Reply `r` to resume · `c` to cancel · or send a redirect message"
                        )
                        await _safe_edit(status_msg, pause_body)

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
                                tool_buf += f"\n*(redirected: {raw[:60]})*"
                        except asyncio.TimeoutError:
                            session.cancel()
                            tool_buf += "\n*(pause timed out — cancelled)*"

                    elif isinstance(event, CompressDone):
                        await message.channel.send(
                            f"-# 🧠 記憶壓縮：{event.fact_count} 條事實已存入語意記憶"
                        )

                    elif isinstance(event, TurnDropped):
                        # Surface silent drops so the user knows what happened
                        # instead of the turn just vanishing with no feedback.
                        if event.stop_reason == "stream_none":
                            if event.exhausted:
                                drop_msg = (
                                    f"-# ⚠️ 連線中斷且重試失敗（已完成 {event.tool_count} 個工具）"
                                )
                            else:
                                drop_msg = (
                                    f"-# ⚠️ 連線中斷，正在重試（第 {event.retry_count} 次）…"
                                )
                        else:
                            drop_msg = (
                                f"-# ⚠️ 任務中止：`stop_reason={event.stop_reason}` "
                                f"（已完成 {event.tool_count} 個工具）"
                            )
                        await message.channel.send(drop_msg)

                    elif isinstance(event, ActionRolledBack):
                        _had_rollback = True
                        icon = "✓" if event.rollback_success else "✗"
                        tool_buf += f"\n↩ {icon} {event.tool_name} rolled back"
                        if event.message:
                            tool_buf += f" — {event.message[:80]}"
                        await _safe_edit(status_msg, tool_buf.lstrip())

                    elif isinstance(event, ActionStateChange):
                        pass  # too granular for Discord display

                    elif isinstance(event, TurnDone):
                        pass  # summary handled after the loop

            except asyncio.CancelledError:
                # Cleanup any pending confirmation buttons in this thread immediately
                conf_msg = self._active_confirmations.pop(message.channel.id, None)
                if conf_msg:
                    try:
                        await _safe_edit(conf_msg, "🛑 **Turn Cancelled** — authorization revoked.", view=None)
                    except Exception:
                        pass

                # /stop — finalize what we have so far
                if tool_buf:
                    await _safe_edit(
                        status_msg, tool_buf.lstrip() + "\n\n🛑 *(stopped)*"
                    )
                else:
                    await _safe_edit(status_msg, "🛑 *(stopped)*")
                partial = narration_buf.strip()
                if partial:
                    await message.channel.send(f"⬥ {partial}\n\n🛑 *(stopped)*")
                raise

            except Exception as exc:
                await _safe_edit(status_msg, f"❌ Error: {exc}")
                return

        # typing() context exits here — "Bot is typing…" disappears.

        # ── Finalise status_msg (tool activity log) ───────────────────────
        if tool_buf:
            await _safe_edit(status_msg, tool_buf.lstrip())
        else:
            try:
                await status_msg.delete()
            except discord.HTTPException:
                pass

        # ── Send any remaining narration ──────────────────────────────────
        final = narration_buf.strip()
        if not final and not tool_buf:
            final = "*(no response)*"
        if final:
            remaining = f"⬥ {final}" if not final.startswith("⬥") else final
            while remaining:
                chunk, remaining = remaining[:_MAX_CHARS], remaining[_MAX_CHARS:]
                await message.channel.send(chunk)

        # ── Turn summary (if enabled) ─────────────────────────────────────
        if self._summary_mode != "off" and _envelope_count > 0:
            # Grants info
            active_grants = [
                g for g in session.perm.grants
                if g.valid_until <= 0 or g.valid_until > time.time()
            ]
            grants_str = f"grants {len(active_grants)} active" if active_grants else "grants 0"

            if self._summary_mode == "detail":
                # Embed-based detailed summary
                embed = discord.Embed(
                    title="Turn Summary",
                    color=0x2ecc71 if _total_failures == 0 else 0xe74c3c,
                )
                embed.add_field(name="Envelopes", value=str(_envelope_count), inline=True)
                embed.add_field(name="Actions", value=str(_total_actions), inline=True)
                embed.add_field(name="Failures", value=str(_total_failures), inline=True)
                embed.add_field(name="Elapsed", value=f"{_total_elapsed_ms / 1000:.1f}s", inline=True)
                if _had_pause:
                    embed.add_field(name="Paused", value="Yes", inline=True)
                if _had_rollback:
                    embed.add_field(name="Rollbacks", value="Yes", inline=True)
                embed.add_field(name="Grants", value=grants_str, inline=True)
                embed.set_footer(text=f"{session.current_personality or 'default'}  ·  context {session.budget.usage_fraction * 100:.0f}%  ·  {session.model}")
                await message.channel.send(embed=embed)
            else:
                # Compact one-liner
                parts = [f"✓ {_envelope_count} envelopes", f"{_total_actions} actions"]
                if _total_failures:
                    parts.append(f"{_total_failures} failed")
                parts.append(f"{_total_elapsed_ms / 1000:.1f}s")
                parts.append(grants_str)
                await message.channel.send(f"-# {' · '.join(parts)}")

        # ── Footer: persona / context / model ────────────────────────────
        persona = session.current_personality or "default"
        pct = session.budget.usage_fraction * 100
        model = session.model
        # Skip footer if detail summary already includes it
        if not (self._summary_mode == "detail" and _envelope_count > 0):
            await message.channel.send(
                f"-# {persona}  ·  context {pct:.0f}%  ·  {model}"
            )

        # ── Mark done ─────────────────────────────────────────────────────
        try:
            await message.remove_reaction("⚙️", self._client.user)
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------
    # Tool confirm via Discord buttons
    # ------------------------------------------------------------------

    def _make_confirm_fn(self, thread_id: int):
        client = self._client
        _LEASE_TTL_MIN = 30  # matches BlastRadiusMiddleware._SCOPE_LEASE_TTL

        async def _confirm(call: "ToolCall") -> ConfirmDecision:
            channel = client.get_channel(thread_id)
            if channel is None:
                return ConfirmDecision.DENY

            args_copy = dict(call.args)
            justification = args_copy.pop("justification", None)

            args_preview = "  ".join(
                f"{k}={str(v)[:40]}" for k, v in args_copy.items()
            )[:120]
            trust = call.trust_level.plain
            color = "🟡" if trust == "GUARDED" else "🔴"
            view = _ConfirmView(timeout=60.0)

            just_text = f"**Justification:** *{justification}*\n" if justification else ""

            msg = await channel.send(
                f"{color} **{trust}** — tool confirmation required\n"
                f"**`{call.tool_name}`**\n"
                f"```\n{args_preview}\n```\n"
                f"{just_text}"
                f"*Timeout 60s → auto-deny*",
                view=view,
            )
            self._active_confirmations[thread_id] = msg
            try:
                decision = await view.wait_decision()
            finally:
                self._active_confirmations.pop(thread_id, None)

            if decision == ConfirmDecision.SCOPE:
                await channel.send(
                    f"⏱️ **Scope lease granted** for `{call.tool_name}` — "
                    f"auto-approved for this scope for the next **{_LEASE_TTL_MIN} minutes**."
                )
            elif decision == ConfirmDecision.AUTO:
                await channel.send(
                    f"⚡ **Permanent auto-approve granted** for `{call.tool_name}` — "
                    f"all future calls of this tool class will be approved automatically."
                )

            return decision

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

async def _safe_edit(
    message: discord.Message, 
    content: str, 
    view: discord.ui.View | None = None
) -> None:
    try:
        kwargs = {"content": content[:_MAX_CHARS]}
        if view is not None or bool(message.components):  # clear buttons if message has a View
            kwargs["view"] = view
        await message.edit(**kwargs)
    except discord.HTTPException:
        pass
