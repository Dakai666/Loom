"""Discord emoji reactions (#188).

Two roles in this module:

* **Lifecycle reactions** — the bot itself adds ⚙️ / ✅ / 🔴 to the user's
  message as state-at-a-glance signals. Driven by the bot, not the agent.

* **Expressive reactions** — the agent ("絲絲") can reach for any emoji
  via the ``add_discord_reaction`` tool to colour her replies with mood
  / agreement / surprise. The named vocabulary below is *suggestions*,
  not a hard whitelist; the tool accepts arbitrary unicode emoji.
"""

from __future__ import annotations


# Curated vocabulary. Names are used by the bot's lifecycle hooks AND
# accepted as shortcodes by the agent-facing reaction tool, so the agent
# can write either the name ("done") or the emoji ("✅"). New entries are
# fine — keep the table flat, no nesting / categories.
REACTION: dict[str, str] = {
    # ── Lifecycle (driven by the bot, not the agent) ──────────────────
    "received":  "⚙️",   # turn started — added on _handle_message
    "done":      "✅",   # turn finished cleanly
    "failed":    "🔴",   # turn errored or was cancelled
    "warning":   "🟡",   # degraded / partial — reserved for future use

    # ── Expressive (agent reaches for these via the tool) ─────────────
    "thinking":  "💭",
    "uploading": "📤",
    "celebrate": "🎉",
    "fire":      "🔥",
    "love":      "❤️",
    "broken":    "💔",
    "puzzled":   "🤔",
    "eyes":      "👀",
    "sparkle":   "✨",
    "rocket":    "🚀",
    "wave":      "👋",
    "salute":    "🫡",
}


def resolve(emoji_or_name: str) -> str:
    """Map a shortcode (``"done"``) to its emoji (``"✅"``).

    Raw emoji strings pass through untouched so callers can use either
    form. Empty / whitespace-only inputs raise ``ValueError`` — Discord
    rejects empty reactions with 50035 anyway, fail fast.
    """
    if not emoji_or_name or not emoji_or_name.strip():
        raise ValueError("empty emoji")
    key = emoji_or_name.strip()
    return REACTION.get(key, key)
