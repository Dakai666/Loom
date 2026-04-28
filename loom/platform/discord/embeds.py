"""Discord rich embed v2 (#188) — builder + length validation.

The validation half is the explicit follow-up from #231 that we punted:
``send_discord_embed`` had no length guards and would 50035 on oversized
title / description / field values just like plain ``.send()`` did. Now
every Discord-side hard cap is checked up front so the agent gets a
clean tool error instead of an exception.
"""

from __future__ import annotations

from datetime import datetime, UTC
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import discord


# Discord embed hard caps — see https://discord.com/developers/docs/resources/message#embed-object-embed-limits
EMBED_TITLE_MAX = 256
EMBED_DESCRIPTION_MAX = 4096
EMBED_AUTHOR_NAME_MAX = 256
EMBED_FIELD_NAME_MAX = 256
EMBED_FIELD_VALUE_MAX = 1024
EMBED_FOOTER_TEXT_MAX = 2048
EMBED_FIELDS_MAX = 25
EMBED_TOTAL_MAX = 6000  # sum of all text content


# Color palette by notification "type" (#188). Keys mirror the names in
# the issue. Hex values match the Discord brand colors.
COLOR_TIERS: dict[str, int] = {
    "info":    0x5865F2,  # blurple
    "confirm": 0xFEE75C,  # yellow
    "report":  0x57F287,  # green
    "alert":   0xED4245,  # red
    "input":   0xEB459E,  # pink
}
DEFAULT_COLOR = COLOR_TIERS["info"]


def resolve_color(value: str | int | None) -> int:
    """Coerce a ``color`` arg to an int. Accepts:

    - Named tier (``"info"``, ``"alert"``, …)
    - Hex string (``"#ff0000"`` / ``"ff0000"``)
    - Raw int (passed through)
    - ``None`` → :data:`DEFAULT_COLOR`

    Returns :data:`DEFAULT_COLOR` on parse failure rather than raising —
    a wrong color shouldn't fail the tool.
    """
    if value is None or value == "":
        return DEFAULT_COLOR
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.lower() in COLOR_TIERS:
        return COLOR_TIERS[s.lower()]
    try:
        return int(s.lstrip("#"), 16)
    except (ValueError, TypeError):
        return DEFAULT_COLOR


def validate_embed_args(args: dict[str, Any]) -> str | None:
    """Return an error string if the args would 50035, else None.

    Mirrors the up-front validation in ``send_discord_select`` — better
    a clean tool error than an exception out of discord.py.
    """
    title = args.get("title")
    if title is not None and not isinstance(title, str):
        return "'title' must be a string."
    if isinstance(title, str) and len(title) > EMBED_TITLE_MAX:
        return f"'title' exceeds {EMBED_TITLE_MAX} chars."

    desc = args.get("description")
    if desc is not None and not isinstance(desc, str):
        return "'description' must be a string."
    if isinstance(desc, str) and len(desc) > EMBED_DESCRIPTION_MAX:
        return f"'description' exceeds {EMBED_DESCRIPTION_MAX} chars."

    author_name = args.get("author_name")
    if author_name is not None:
        if not isinstance(author_name, str):
            return "'author_name' must be a string."
        if len(author_name) > EMBED_AUTHOR_NAME_MAX:
            return f"'author_name' exceeds {EMBED_AUTHOR_NAME_MAX} chars."

    footer = args.get("footer")
    if footer is not None:
        if not isinstance(footer, str):
            return "'footer' must be a string."
        if len(footer) > EMBED_FOOTER_TEXT_MAX:
            return f"'footer' exceeds {EMBED_FOOTER_TEXT_MAX} chars."

    fields = args.get("fields") or []
    if not isinstance(fields, list):
        return "'fields' must be a list."
    if len(fields) > EMBED_FIELDS_MAX:
        return f"At most {EMBED_FIELDS_MAX} fields allowed (got {len(fields)})."
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            return f"fields[{i}] must be an object."
        name = f.get("name", "")
        val = f.get("value", "")
        if not isinstance(name, str) or len(name) > EMBED_FIELD_NAME_MAX:
            return f"fields[{i}].name must be a string ≤ {EMBED_FIELD_NAME_MAX} chars."
        if not isinstance(val, str) or len(val) > EMBED_FIELD_VALUE_MAX:
            return f"fields[{i}].value must be a string ≤ {EMBED_FIELD_VALUE_MAX} chars."

    # Discord enforces a 6000-char ceiling across the whole embed.
    total = (
        len(title or "")
        + len(desc or "")
        + len(author_name or "")
        + len(footer or "")
        + sum(len(f.get("name", "")) + len(f.get("value", "")) for f in fields)
    )
    if total > EMBED_TOTAL_MAX:
        return (
            f"Total embed text length {total} exceeds Discord's "
            f"{EMBED_TOTAL_MAX}-char ceiling. Trim title/description/fields."
        )

    return None


def build_embed(args: dict[str, Any]) -> "discord.Embed":
    """Assemble a ``discord.Embed`` from validated args.

    Caller must run :func:`validate_embed_args` first; this function
    trusts its input.
    """
    import discord as _discord

    embed = _discord.Embed(
        title=args.get("title"),
        description=args.get("description") or "",
        color=resolve_color(args.get("color")),
    )

    author_name = args.get("author_name")
    if author_name:
        embed.set_author(name=author_name, icon_url=args.get("author_icon") or None)

    thumb = args.get("thumbnail")
    if thumb:
        embed.set_thumbnail(url=str(thumb))

    for f in args.get("fields") or []:
        embed.add_field(
            name=str(f.get("name", "Field")),
            value=str(f.get("value", "-")),
            inline=bool(f.get("inline", False)),
        )

    footer = args.get("footer")
    if footer:
        embed.set_footer(text=footer)

    if args.get("timestamp"):
        embed.timestamp = datetime.now(UTC)

    return embed
