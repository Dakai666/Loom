"""
Timezone Utilities — Issues #124, #388

Single source of truth for user-facing timestamps in the Loom framework.

Architecture
------------
Two clocks:
  - ``utc_now()``    — always UTC; used for internal logging, DB writes,
                       cron scheduling
  - ``local_now()``  — user's configured timezone (from [timezone] config
                       in loom.toml); used for every timestamp that the
                       LLM or the user is going to see
  - ``user_zone()``  — the user's ``ZoneInfo`` itself; use when parsing
                       agent-supplied date/datetime strings so naked
                       inputs are interpreted in the same zone the agent
                       sees in injected prompt timestamps

The ``[timezone]`` section in ``loom.toml``::

    user     = "Asia/Taipei"   # any IANA tz name; falls back to UTC if absent
    internal = "UTC"           # system timestamps (logging, DB, cron)

Boundary cheat sheet
--------------------
**User-zone paths** (agent / human visible — always use ``local_now()``
or ``user_zone()``)::

  - LLM prompt timestamp prefix (``user_timestamp()``)
  - Memory tool inputs: ``recall`` / ``recall_period`` / ``ledger_recall``
    parse ``since``/``until`` via ``user_zone()``
  - Memory tool outputs: ``_format_period_results``, ledger
    narrative/summary/raw render, ``ReflectionAPI.recent_tool_calls``
  - Discord chime delivery stamp + daily compaction loop trigger time

**UTC paths** (internal only — keep ``datetime.now(UTC)``)::

  - SQLite memory store columns (``semantic.updated_at``,
    ``episodic.created_at``, ledger event ``timestamp``); agents read
    them via the user-zone tools above, never raw
  - Forensics / dreaming / counter_factual / autonomy audit timestamps
  - Cron scheduling: ``CronTrigger.should_fire(datetime.now(UTC))``;
    cron strings in ``loom.toml [[autonomy.schedules]]`` are always
    written in UTC, the operator hand-converts from local time. The
    per-schedule ``timezone`` field is currently a no-op — do not rely
    on it (tracked separately).

When in doubt: if the timestamp is about to enter a tool result, a
prompt, or chat UI, route through this module. Never inject
``datetime.now(UTC)`` directly into anything the agent or user reads.
"""

from __future__ import annotations

import zoneinfo
from datetime import datetime, UTC
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Lazy-loaded config (avoids circular imports at import time)
# ---------------------------------------------------------------------------

# Cache the resolved zoneinfos so repeated calls are cheap.
_USER_ZONE: zoneinfo.ZoneInfo | None = None
_INTERNAL_ZONE: zoneinfo.ZoneInfo = zoneinfo.ZoneInfo("UTC")


def _ensure_user_zone() -> zoneinfo.ZoneInfo:
    """Lazily resolve and cache the user timezone from loom.toml.

    Falls back to UTC if ``[timezone].user`` is absent or unreadable —
    never to a hardcoded regional timezone.
    """
    global _USER_ZONE
    if _USER_ZONE is None:
        cfg = _load_timezone_config()
        _USER_ZONE = _zone(cfg.get("user", "UTC"))
    return _USER_ZONE


def _load_timezone_config() -> dict[str, str]:
    """Load [timezone] section from loom.toml. Returns {} on miss."""
    try:
        import tomllib

        candidates = [
            Path.cwd() / "loom.toml",
            Path(__file__).parents[2] / "loom.toml",
        ]
        for path in candidates:
            if path.exists():
                with open(path, "rb") as fh:
                    cfg = tomllib.load(fh)
                    tz = cfg.get("timezone", {})
                    if tz:
                        return tz
    except Exception:
        pass
    return {}


def _zone(tz_name: str) -> zoneinfo.ZoneInfo:
    """Resolve a timezone name to a ZoneInfo, with fallback."""
    try:
        return zoneinfo.ZoneInfo(tz_name)
    except Exception:
        # Unknown timezone — fall back to UTC silently
        return zoneinfo.ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    """
    Returns the current UTC datetime (timezone-aware).

    Use for:
    - Internal logging and audit trails
    - Database timestamps
    - Cron schedule comparisons (the engine uses UTC internally)
    """
    return datetime.now(UTC)


def local_now() -> datetime:
    """
    Returns the current datetime in the user's configured timezone
    (from ``[timezone].user`` in loom.toml).

    This is the ONLY function that should be used when injecting
    timestamps into LLM prompts or user-visible messages.

    Falls back to UTC if ``[timezone].user`` is not configured.
    """
    return datetime.now(_ensure_user_zone())


def local_zone_name() -> str:
    """Return the configured user timezone name (e.g. 'Asia/Taipei')."""
    return str(_ensure_user_zone().key)


def user_zone() -> zoneinfo.ZoneInfo:
    """
    Return the user's configured timezone as a :class:`zoneinfo.ZoneInfo`.

    Use when parsing agent-supplied date/datetime strings — naked inputs
    should be interpreted in the user's zone, matching what the agent
    sees in injected prompt timestamps (``user_timestamp()``).
    """
    return _ensure_user_zone()


def user_timestamp() -> str:
    """
    Returns a formatted timestamp string for LLM prompts.

    Format: ``[YYYY-MM-DD HH:MM <timezone>]``  (timezone from loom.toml)

    This is what gets prepended to user messages in ``stream_turn()``
    and to autonomy trigger notifications.
    """
    return local_now().strftime(f"[%Y-%m-%d %H:%M {local_zone_name()}]")


# ---------------------------------------------------------------------------
# Internal helpers used by the framework
# ---------------------------------------------------------------------------

def cron_timestamp() -> str:
    """
    UTC timestamp for cron log entries.

    Format: ``[YYYY-MM-DD HH:MM UTC]``
    """
    return utc_now().strftime("[%Y-%m-%d %H:%M UTC]")
