"""Native Discord slash commands (#189).

Text-prefix commands (`/new`, `/help`, …) stay supported for CLI/Discord
parity — see the long comment in :func:`bot.LoomDiscordBot._cmd_help`.
This module adds the same surface as proper application commands so
Discord's autocomplete, type validation, and per-command help work too.

Design
------
Every command body lives on ``LoomDiscordBot._cmd_*``. The handlers here
are thin shims: look up the active session by channel id, call the
backend method, reply with its return string. ``/loom-new`` and
``/loom-compact`` are the two exceptions because they fire multiple
Discord messages — they're handled inline.

Commands are namespaced ``loom-*`` so they don't collide with whatever
other bots are in the same server. Discord shows the bot name on hover
but the textual prefix is what users actually type to filter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from loom.platform.discord.bot import LoomDiscordBot
    from loom.core.session import LoomSession


# Personalities and summary modes are static enough that hard-coded Choices
# are clearer than dynamic autocomplete. Models and grant IDs *are* dynamic
# and use autocomplete callbacks below.
_PERSONALITY_CHOICES = [
    app_commands.Choice(name="adversarial", value="adversarial"),
    app_commands.Choice(name="minimalist", value="minimalist"),
    app_commands.Choice(name="architect", value="architect"),
    app_commands.Choice(name="researcher", value="researcher"),
    app_commands.Choice(name="operator", value="operator"),
    app_commands.Choice(name="off (clear)", value="off"),
]

_SUMMARY_CHOICES = [
    app_commands.Choice(name="off", value="off"),
    app_commands.Choice(name="on", value="on"),
    app_commands.Choice(name="detail", value="detail"),
]

_SCOPE_SUB_CHOICES = [
    app_commands.Choice(name="list", value="list"),
    app_commands.Choice(name="revoke", value="revoke"),
    app_commands.Choice(name="clear", value="clear"),
]


def _session_for(bot: "LoomDiscordBot", interaction: discord.Interaction) -> "LoomSession | None":
    """Return the session bound to the interaction's channel, if any.

    Slash commands fire from any channel the user can see, so we explicitly
    look up by channel id rather than assuming a thread context.
    """
    if interaction.channel is None:
        return None
    return bot._sessions.get(interaction.channel.id)


async def _require_session(
    bot: "LoomDiscordBot", interaction: discord.Interaction
) -> "LoomSession | None":
    """Return the session or send a friendly error and return None."""
    session = _session_for(bot, interaction)
    if session is None:
        await interaction.response.send_message(
            "This command must be used inside a Loom session thread. "
            "Send a message in the main channel (or use `/loom-new`) to start one.",
            ephemeral=True,
        )
        return None
    return session


def register_slash_commands(bot: "LoomDiscordBot") -> None:
    """Register every `/loom-*` slash command on ``bot._tree``.

    Called once during ``LoomDiscordBot.__init__``. The actual sync to
    Discord happens in ``on_ready`` so we don't have to be online here.
    """
    tree = bot._tree

    # ── /loom-help ────────────────────────────────────────────────────
    @tree.command(name="loom-help", description="Show the Loom command list.")
    async def loom_help(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(bot._cmd_help(), ephemeral=True)

    # ── /loom-new ─────────────────────────────────────────────────────
    @tree.command(
        name="loom-new",
        description="Open a new Loom session thread under the current channel.",
    )
    async def loom_new(interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if channel is None:
            await interaction.response.send_message(
                "Cannot create a thread here.", ephemeral=True,
            )
            return

        # Resolve the parent text channel — works whether we're called from
        # the lobby or from inside an existing thread.
        parent: discord.TextChannel | None
        if isinstance(channel, discord.Thread):
            parent = channel.parent  # type: ignore[assignment]
        elif isinstance(channel, discord.TextChannel):
            parent = channel
        else:
            parent = None

        if parent is None:
            await interaction.response.send_message(
                "Cannot create a thread here — slash command must be used in a guild text channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        new_thread = await parent.create_thread(
            name="New session",
            auto_archive_duration=1440,  # matches _THREAD_ARCHIVE_MINUTES
            type=discord.ChannelType.public_thread,
        )
        await bot._start_session(new_thread.id)
        await new_thread.send("✨ New session started. Send your first message here.")
        await interaction.followup.send(
            f"✨ Opened new session → {new_thread.mention}", ephemeral=True,
        )

    # ── /loom-sessions ────────────────────────────────────────────────
    @tree.command(name="loom-sessions", description="List recent Loom sessions.")
    async def loom_sessions(interaction: discord.Interaction) -> None:
        session = await _require_session(bot, interaction)
        if session is None:
            return
        reply = await bot._cmd_sessions(session)
        await interaction.response.send_message(reply, ephemeral=True)

    # ── /loom-think ───────────────────────────────────────────────────
    @tree.command(name="loom-think", description="View the last turn's reasoning chain.")
    async def loom_think(interaction: discord.Interaction) -> None:
        session = await _require_session(bot, interaction)
        if session is None:
            return
        await interaction.response.send_message(bot._cmd_think(session), ephemeral=True)

    # ── /loom-compact ─────────────────────────────────────────────────
    @tree.command(name="loom-compact", description="Compress older context to free tokens.")
    async def loom_compact(interaction: discord.Interaction) -> None:
        session = await _require_session(bot, interaction)
        if session is None:
            return
        pct = session.budget.usage_fraction * 100
        await interaction.response.defer(thinking=True)
        await session._smart_compact()
        await interaction.followup.send(f"✅ Context compacted (was {pct:.1f}% used).")

    # ── /loom-model ───────────────────────────────────────────────────
    async def _model_autocomplete(
        interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        session = _session_for(bot, interaction)
        if session is None:
            return []
        # Surface registered providers as completion hints. Specific model
        # names per provider are too dynamic to enumerate up front.
        hints = []
        for prefix in ("MiniMax-M2", "claude-sonnet-4-6", "claude-opus-4-7", "deepseek/deepseek-chat"):
            if current.lower() in prefix.lower():
                hints.append(app_commands.Choice(name=prefix, value=prefix))
        return hints[:25]

    @tree.command(name="loom-model", description="Show or switch the active LLM model.")
    @app_commands.describe(model="Model name. Leave empty to show the current model.")
    @app_commands.autocomplete(model=_model_autocomplete)
    async def loom_model(interaction: discord.Interaction, model: str | None = None) -> None:
        session = await _require_session(bot, interaction)
        if session is None:
            return
        await interaction.response.send_message(
            bot._cmd_model(session, model or ""), ephemeral=True,
        )

    # ── /loom-personality ─────────────────────────────────────────────
    @tree.command(
        name="loom-personality",
        description="Switch cognitive persona, or omit to show the active one.",
    )
    @app_commands.describe(name="Persona to activate. Choose 'off (clear)' to remove.")
    @app_commands.choices(name=_PERSONALITY_CHOICES)
    async def loom_personality(
        interaction: discord.Interaction,
        name: app_commands.Choice[str] | None = None,
    ) -> None:
        session = await _require_session(bot, interaction)
        if session is None:
            return
        arg = name.value if name is not None else ""
        await interaction.response.send_message(
            bot._cmd_personality(session, arg), ephemeral=True,
        )

    # ── /loom-auto ────────────────────────────────────────────────────
    @tree.command(
        name="loom-auto",
        description="Toggle run_bash auto-approve (requires strict_sandbox).",
    )
    async def loom_auto(interaction: discord.Interaction) -> None:
        session = await _require_session(bot, interaction)
        if session is None:
            return
        await interaction.response.send_message(bot._cmd_auto(session))

    # ── /loom-pause ───────────────────────────────────────────────────
    @tree.command(
        name="loom-pause",
        description="Toggle HITL auto-pause after each tool batch.",
    )
    async def loom_pause(interaction: discord.Interaction) -> None:
        session = await _require_session(bot, interaction)
        if session is None:
            return
        await interaction.response.send_message(bot._cmd_pause(session))

    # ── /loom-stop ────────────────────────────────────────────────────
    @tree.command(name="loom-stop", description="Cancel the running turn in this thread.")
    async def loom_stop(interaction: discord.Interaction) -> None:
        # /loom-stop deliberately doesn't require an active session — it's
        # safe to call when nothing is running, and the bot returns a
        # benign \"nothing is running\" reply.
        if interaction.channel is None:
            await interaction.response.send_message(
                "Cannot stop — no channel context.", ephemeral=True,
            )
            return
        await interaction.response.send_message(bot._cmd_stop(interaction.channel.id))

    # ── /loom-budget ──────────────────────────────────────────────────
    @tree.command(name="loom-budget", description="Show current context token usage.")
    async def loom_budget(interaction: discord.Interaction) -> None:
        session = await _require_session(bot, interaction)
        if session is None:
            return
        await interaction.response.send_message(bot._cmd_budget(session), ephemeral=True)

    # ── /loom-title ───────────────────────────────────────────────────
    @tree.command(name="loom-title", description="Set or show the current session's title.")
    @app_commands.describe(title="New title. Omit to display the current one.")
    async def loom_title(
        interaction: discord.Interaction,
        title: str | None = None,
    ) -> None:
        session = await _require_session(bot, interaction)
        if session is None:
            return
        reply = await bot._cmd_title(session, title or "")
        await interaction.response.send_message(reply, ephemeral=True)

    # ── /loom-summary ─────────────────────────────────────────────────
    @tree.command(
        name="loom-summary",
        description="Set or show the per-turn summary mode.",
    )
    @app_commands.describe(mode="off · on · detail. Omit to show the current mode.")
    @app_commands.choices(mode=_SUMMARY_CHOICES)
    async def loom_summary(
        interaction: discord.Interaction,
        mode: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.send_message(
            bot._cmd_summary(mode.value if mode else ""), ephemeral=True,
        )

    # ── /loom-scope ───────────────────────────────────────────────────
    async def _scope_id_autocomplete(
        interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[int]]:
        session = _session_for(bot, interaction)
        if session is None:
            return []
        import time
        now = time.time()
        out: list[app_commands.Choice[int]] = []
        for idx, g in enumerate(session.perm.grants):
            if g.valid_until > 0 and g.valid_until <= now:
                continue  # expired
            label = f"#{idx} · {g.action if g.action != '*' else g.resource} · {g.selector}"
            if current and current not in label:
                continue
            out.append(app_commands.Choice(name=label[:100], value=idx))
            if len(out) >= 25:
                break
        return out

    @tree.command(name="loom-scope", description="Manage active scope grants.")
    @app_commands.describe(
        sub="list / revoke / clear",
        grant_id="Grant ID for revoke (see /loom-scope sub:list).",
    )
    @app_commands.choices(sub=_SCOPE_SUB_CHOICES)
    @app_commands.autocomplete(grant_id=_scope_id_autocomplete)
    async def loom_scope(
        interaction: discord.Interaction,
        sub: app_commands.Choice[str],
        grant_id: int | None = None,
    ) -> None:
        session = await _require_session(bot, interaction)
        if session is None:
            return
        subarg = str(grant_id) if grant_id is not None else ""
        await interaction.response.send_message(
            bot._cmd_scope(session, sub.value, subarg), ephemeral=True,
        )
