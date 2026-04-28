"""
Discord-specific tools for the Loom Agent.
Provides capabilities to send files and rich embeds directly to the Discord thread.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    import discord

from loom.core.harness.registry import ToolDefinition
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.middleware import ToolCall, ToolResult


# Discord SelectMenu hard limits — surface as constants so callers and tests
# can reference them without re-deriving from the API docs.
_SELECT_MAX_OPTIONS = 25
_SELECT_LABEL_MAX = 100
_SELECT_DESCRIPTION_MAX = 100
_SELECT_TIMEOUT_DEFAULT = 60
_SELECT_TIMEOUT_MAX = 600  # 10 min — anything longer is almost certainly a bug

def make_send_discord_file_tool(client: discord.Client, thread_id: int, workspace: Path) -> ToolDefinition:
    async def executor(call: ToolCall) -> ToolResult:
        import discord as _discord
        channel = client.get_channel(thread_id)
        if channel is None:
            return ToolResult(call.id, call.tool_name, False, error="Discord channel/thread not found or accessible.")

        filepath = call.args.get("filepath", "")
        if not filepath:
            return ToolResult(call.id, call.tool_name, False, error="Missing 'filepath'.")

        target_path = (workspace / filepath).resolve()
        if not target_path.is_relative_to(workspace):
            return ToolResult(call.id, call.tool_name, False, error="Cannot access files outside the workspace.")

        if not target_path.exists() or not target_path.is_file():
            return ToolResult(call.id, call.tool_name, False, error=f"File not found or is a directory: {filepath}")

        try:
            await channel.send(file=_discord.File(str(target_path)))
            return ToolResult(call.id, call.tool_name, True, output=f"Successfully sent file {filepath} to Discord.")
        except Exception as e:
            return ToolResult(call.id, call.tool_name, False, error=f"Failed to send file: {e}")

    return ToolDefinition(
        name="send_discord_file",
        description="Send a file from your workspace directly into the current Discord thread. Use this if the user asks you to send them an image, document, or media file.",
        trust_level=TrustLevel.GUARDED,
        input_schema={
            "type": "object",
            "properties": {
                "filepath": {"type": "string", "description": "The relative path to the file in the workspace."},
            },
            "required": ["filepath"]
        },
        executor=executor
    )

def make_send_discord_embed_tool(client: discord.Client, thread_id: int) -> ToolDefinition:
    """Rich embed v2 (#188) — title / description / fields plus thumbnail,
    author, footer, auto-timestamp, and a colour palette by tier name.

    Validates against Discord's hard caps up front (#231 follow-up) so an
    oversize embed becomes a clean tool error instead of a 50035.
    """
    from loom.platform.discord.embeds import build_embed, validate_embed_args

    async def executor(call: ToolCall) -> ToolResult:
        err = validate_embed_args(call.args)
        if err:
            return ToolResult(call.id, call.tool_name, False, error=err)

        channel = client.get_channel(thread_id)
        if channel is None:
            return ToolResult(
                call.id, call.tool_name, False,
                error="Discord channel/thread not found or accessible.",
            )

        embed = build_embed(call.args)
        try:
            await channel.send(embed=embed)
            return ToolResult(
                call.id, call.tool_name, True,
                output="Successfully sent rich embed to Discord.",
            )
        except Exception as e:
            return ToolResult(call.id, call.tool_name, False, error=f"Failed to send embed: {e}")

    return ToolDefinition(
        name="send_discord_embed",
        description=(
            "Send a rich embed panel (Discord card-style block) to the current "
            "thread. Use for structured summaries, status dashboards, or any "
            "moment where a wall of plain text would feel flat. Supports "
            "thumbnail / author / footer / auto-timestamp and a named colour "
            "tier (info/confirm/report/alert/input) on top of raw hex colours. "
            "Inline fields tile horizontally — useful for metrics rows."
        ),
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Embed title (≤256 chars)."},
                "description": {"type": "string", "description": "Main body (≤4096 chars)."},
                "color": {
                    "type": "string",
                    "description": (
                        "Either a tier name (info · confirm · report · alert · input) "
                        "or a hex code like '#ff0000'. Default: 'info'."
                    ),
                },
                "thumbnail": {"type": "string", "description": "URL of a small image shown top-right."},
                "author_name": {"type": "string", "description": "Top-left attribution name."},
                "author_icon": {"type": "string", "description": "URL of an icon next to author name."},
                "footer": {"type": "string", "description": "Footer text (≤2048 chars)."},
                "timestamp": {
                    "type": "boolean",
                    "description": "If true, append an ISO timestamp in the footer.",
                },
                "fields": {
                    "type": "array",
                    "description": "Up to 25 fields. Each name ≤256, value ≤1024.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "value": {"type": "string"},
                            "inline": {"type": "boolean"},
                        },
                        "required": ["name", "value"],
                    },
                },
            },
            "required": ["title", "description"],
        },
        executor=executor,
    )


def make_add_discord_reaction_tool(
    client: discord.Client,
    thread_id: int,
    *,
    last_user_message_lookup,
) -> ToolDefinition:
    """Tool that lets the agent express mood / agreement / surprise via
    emoji reactions on a thread message (#188).

    Lifecycle reactions (⚙️/✅/🔴) are added by the bot automatically on
    turn boundaries; this tool is for the agent's own voice — celebration,
    sympathy, quiet acknowledgement, anything where a single emoji says
    more than a sentence.

    ``last_user_message_lookup`` is a callable returning the most recent
    user-message id in the bound thread, used as the default target when
    the agent doesn't pass an explicit ``message_id``.
    """
    from loom.platform.discord.reactions import REACTION, resolve

    async def executor(call: ToolCall) -> ToolResult:
        import discord as _discord

        emoji_arg = call.args.get("emoji")
        if not isinstance(emoji_arg, str) or not emoji_arg.strip():
            return ToolResult(
                call.id, call.tool_name, False,
                error="'emoji' is required (raw emoji or shortcode like 'celebrate').",
            )
        try:
            emoji = resolve(emoji_arg)
        except ValueError as e:
            return ToolResult(call.id, call.tool_name, False, error=str(e))

        channel = client.get_channel(thread_id)
        if channel is None:
            return ToolResult(
                call.id, call.tool_name, False,
                error="Discord channel/thread not found or accessible.",
            )

        message_id = call.args.get("message_id")
        if message_id is None:
            message_id = last_user_message_lookup(thread_id)
        if message_id is None:
            return ToolResult(
                call.id, call.tool_name, False,
                error="No target message — pass 'message_id' or wait for a user message in this thread.",
            )

        try:
            target = await channel.fetch_message(int(message_id))
            await target.add_reaction(emoji)
        except _discord.HTTPException as e:
            return ToolResult(call.id, call.tool_name, False, error=f"Failed to add reaction: {e}")
        except (ValueError, TypeError):
            return ToolResult(call.id, call.tool_name, False, error="Invalid message_id.")

        return ToolResult(
            call.id, call.tool_name, True,
            output={"emoji": emoji, "message_id": int(message_id)},
        )

    # Hint of the curated vocabulary for the tool description — agent
    # gets to see the named shortcodes inline alongside the example
    # emoji. Lifecycle keys stay out (those are bot-managed).
    expressive = ", ".join(
        f"{name}={emoji}" for name, emoji in REACTION.items()
        if name not in {"received", "done", "failed", "warning"}
    )

    return ToolDefinition(
        name="add_discord_reaction",
        description=(
            "Add an emoji reaction to a message in the current Discord thread "
            "to express mood, agreement, or quiet acknowledgement. Targets the "
            "user's most recent message by default. Accepts any unicode emoji, "
            "OR one of these shortcodes: " + expressive + ". Lifecycle "
            "reactions (⚙️/✅/🔴) are managed automatically — don't duplicate "
            "them. Use sparingly: a single reaction can land harder than a "
            "paragraph; spamming feels noisy."
        ),
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "emoji": {
                    "type": "string",
                    "description": "Unicode emoji (e.g. '🎉') or shortcode (e.g. 'celebrate').",
                },
                "message_id": {
                    "type": "integer",
                    "description": "Optional. Defaults to the most recent user message in the thread.",
                },
            },
            "required": ["emoji"],
        },
        executor=executor,
    )


def _validate_select_args(args: dict[str, Any]) -> str | None:
    """Return an error string if args are unusable, else None.

    Validation runs before we touch Discord so the agent gets a clean tool
    error instead of a 50035 from the API. Discord's hard caps (25 options,
    100-char label/description) are non-negotiable — exceeding them silently
    truncates or 400s; we'd rather reject and let the agent retry.
    """
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        return "Missing or empty 'title'."

    options = args.get("options")
    if not isinstance(options, list) or not options:
        return "'options' must be a non-empty list."
    if len(options) > _SELECT_MAX_OPTIONS:
        return f"Discord allows at most {_SELECT_MAX_OPTIONS} options per select menu (got {len(options)})."

    seen_values: set[str] = set()
    for i, opt in enumerate(options):
        if not isinstance(opt, dict):
            return f"options[{i}] must be an object."
        label = opt.get("label")
        value = opt.get("value")
        if not isinstance(label, str) or not label:
            return f"options[{i}].label is required."
        if not isinstance(value, str) or not value:
            return f"options[{i}].value is required."
        if len(label) > _SELECT_LABEL_MAX:
            return f"options[{i}].label exceeds {_SELECT_LABEL_MAX} chars."
        desc = opt.get("description")
        if desc is not None:
            if not isinstance(desc, str):
                return f"options[{i}].description must be a string."
            if len(desc) > _SELECT_DESCRIPTION_MAX:
                return f"options[{i}].description exceeds {_SELECT_DESCRIPTION_MAX} chars."
        if value in seen_values:
            return f"Duplicate option value: {value!r}."
        seen_values.add(value)

    max_values = args.get("max_values", 1)
    if not isinstance(max_values, int) or max_values < 1:
        return "'max_values' must be a positive integer."
    if max_values > len(options):
        return f"'max_values' ({max_values}) cannot exceed option count ({len(options)})."

    timeout = args.get("timeout_seconds", _SELECT_TIMEOUT_DEFAULT)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        return "'timeout_seconds' must be a positive number."
    if timeout > _SELECT_TIMEOUT_MAX:
        return f"'timeout_seconds' must be ≤ {_SELECT_TIMEOUT_MAX}."

    return None


def make_send_discord_select_tool(
    client: discord.Client,
    thread_id: int,
    *,
    register_active: Callable[[int, Any], None] | None = None,
    unregister_active: Callable[[int], None] | None = None,
) -> ToolDefinition:
    """Tool factory for ``send_discord_select`` (#190).

    Renders a Discord SelectMenu in the bound thread and **blocks** the tool
    call until the user picks an option or the timeout expires. This is the
    same pattern as ``_ConfirmView`` — choosing is just a different verb than
    confirming, but the lifecycle (render → wait → disable) is identical.

    The optional ``register_active`` / ``unregister_active`` hooks let the bot
    track in-flight select menus per thread so a cancelled turn can disable
    them mid-flight (matches ``_active_confirmations`` behaviour). They are
    optional so the factory stays unit-testable without a live bot.
    """
    async def executor(call: ToolCall) -> ToolResult:
        import discord as _discord

        err = _validate_select_args(call.args)
        if err:
            return ToolResult(call.id, call.tool_name, False, error=err)

        channel = client.get_channel(thread_id)
        if channel is None:
            return ToolResult(
                call.id, call.tool_name, False,
                error="Discord channel/thread not found or accessible.",
            )

        title: str = call.args["title"]
        placeholder: str = call.args.get("placeholder") or "Choose an option"
        options: list[dict[str, Any]] = call.args["options"]
        max_values: int = int(call.args.get("max_values", 1))
        timeout: float = float(call.args.get("timeout_seconds", _SELECT_TIMEOUT_DEFAULT))

        # Build the options + view.
        select_options = [
            _discord.SelectOption(
                label=o["label"],
                value=o["value"],
                description=o.get("description"),
            )
            for o in options
        ]

        done: asyncio.Event = asyncio.Event()
        result_state: dict[str, Any] = {"selected": None, "cancelled": False}

        select = _discord.ui.Select(
            placeholder=placeholder[:_SELECT_LABEL_MAX],
            min_values=1,
            max_values=max_values,
            options=select_options,
        )

        async def _on_select(interaction: _discord.Interaction) -> None:
            picks = list(select.values)
            label_for = {o["value"]: o["label"] for o in options}
            result_state["selected"] = picks
            result_state["labels"] = [label_for.get(v, v) for v in picks]
            done.set()
            # Edit the message to disable further interaction. Echo the choice
            # so the thread reads as a coherent dialogue instead of leaving a
            # frozen menu behind.
            picked_str = ", ".join(label_for.get(v, v) for v in picks)
            try:
                await interaction.response.edit_message(
                    content=f"✅ **{title}** → {picked_str}", view=None,
                )
            except _discord.HTTPException:
                pass

        select.callback = _on_select
        view = _discord.ui.View(timeout=timeout)
        view.add_item(select)

        msg = None
        try:
            msg = await channel.send(content=f"**{title}**", view=view)
        except _discord.HTTPException as e:
            return ToolResult(
                call.id, call.tool_name, False,
                error=f"Failed to send select menu: {e}",
            )

        if register_active is not None:
            register_active(thread_id, msg)

        try:
            # asyncio.wait_for drives the timeout authoritatively. The View's
            # own timer is parallel but only governs Discord's UI-side
            # disable; this loop owns the agent's wait.
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # View timeout already fired; tidy the message so the menu doesn't
            # linger as a clickable trap.
            result_state["cancelled"] = True
            try:
                await msg.edit(content=f"⌛ **{title}** — no response (timed out).", view=None)
            except _discord.HTTPException:
                pass
        finally:
            if unregister_active is not None:
                unregister_active(thread_id)

        if result_state["cancelled"] or result_state["selected"] is None:
            return ToolResult(
                call.id, call.tool_name, True,
                output={"cancelled": True},
            )

        picks = result_state["selected"]
        labels = result_state["labels"]
        if max_values == 1:
            output = {"selected": picks[0], "label": labels[0]}
        else:
            output = {"selected": picks, "labels": labels}
        return ToolResult(call.id, call.tool_name, True, output=output)

    return ToolDefinition(
        name="send_discord_select",
        description=(
            "Present the user with a bounded set of choices via a Discord "
            "SelectMenu (dropdown) and **block** until they pick one. Use "
            "*sparingly* — only when the choice set is inherently bounded "
            "(≤25), typing the answer would be high-friction, and a free-text "
            "reply would be ambiguous. For open-ended questions, just ask in "
            "natural language. Returns either {selected, label[s]} on a pick "
            "or {cancelled: true} on timeout."
        ),
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Prompt shown above the menu.",
                },
                "placeholder": {
                    "type": "string",
                    "description": "Greyed-out hint inside the dropdown before a choice is made.",
                },
                "options": {
                    "type": "array",
                    "description": f"Choices (max {_SELECT_MAX_OPTIONS}). Values must be unique.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": f"Visible text (max {_SELECT_LABEL_MAX} chars)."},
                            "value": {"type": "string", "description": "Stable identifier returned to the agent."},
                            "description": {"type": "string", "description": f"Optional sub-text (max {_SELECT_DESCRIPTION_MAX} chars)."},
                        },
                        "required": ["label", "value"],
                    },
                },
                "max_values": {
                    "type": "integer",
                    "description": "How many options the user may pick. Default 1.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": f"Seconds before auto-cancel. Default {_SELECT_TIMEOUT_DEFAULT}, max {_SELECT_TIMEOUT_MAX}.",
                },
            },
            "required": ["title", "options"],
        },
        executor=executor,
    )
