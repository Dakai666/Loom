"""
Timezone Utilities — Issue #124

Provides a single source of truth for all user-facing timestamps
in the Loom framework.

Architecture
------------
Two clocks:
  - `utc_now()`     — always UTC; used for logging, DB, cron scheduling
  - `local_now()`   — user's timezone (from [timezone] config in loom.toml);
                      used for ALL timestamps that appear in LLM prompts

The [timezone] section in loom.toml:
  user = "Asia/Taipei"   # user-facing display / LLM context
  internal = "UTC"        # system timestamps (logging, DB, cron)

All code that injects timestamps into messages going to the LLM MUST use
``local_now()`` — never ``datetime.now(UTC)`` directly.
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
    global _USER_ZONE
    if _USER_ZONE is None:
        cfg = _load_timezone_config()
        _USER_ZONE = _zone(cfg.get("user", "Asia/Taipei"))
    return datetime.now(_USER_ZONE)


def local_zone_name() -> str:
    """Return the configured user timezone name (e.g. 'Asia/Taipei')."""
    global _USER_ZONE
    if _USER_ZONE is None:
        cfg = _load_timezone_config()
        _USER_ZONE = _zone(cfg.get("user", "Asia/Taipei"))
    return str(_USER_ZONE.key)


def user_timestamp() -> str:
    """
    Returns a formatted timestamp string for LLM prompts.

    Format: ``[YYYY-MM-DD HH:MM Asia/Taipei]``

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
