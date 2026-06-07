"""
Shared parser for autonomy permission fields — ``trust_level`` /
``allowed_tools`` / ``scope_grants`` (issue #525).

These fields describe *what an autonomous turn is pre-authorised to do*.
``schedules.toml`` entries have carried them since #444; the circadian rhythm
loader (``rhythm.toml``) reuses the same parser so both express permissions
through one code path (DK: "純 code 層統一"). Extracting the parser is the
unification — both loaders import it instead of re-reading the dict inline.

The two callers consume different subsets: a **schedule** uses all three
(``trust_level`` gates the planner's notify/HOLD decision). A **phase anchor**
uses only ``allowed_tools`` / ``scope_grants`` — the chime path has no planner
gate, so ``trust_level`` is meaningless there and the rhythm loader ignores it.
The parser still returns ``trust_level`` for the schedule caller.

Contract is tolerant, like every other autonomy loader: a missing field yields
a neutral default, a malformed one is dropped (never raised), so a single typo
never silences the whole entry. ``trust_level`` is reported as ``None`` when
absent so each caller applies its own default (a bare schedule wants
``"guarded"``).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

VALID_TRUST_LEVELS = {"safe", "guarded", "critical"}


def validate_trust_level(value: str, source_name: str) -> str:
    """Return *value* if it is a known trust level, else ``"guarded"``.

    An unknown level is a config typo; defaulting to the stricter ``guarded``
    (re-confirm) fails safe rather than silently widening authority.
    """
    if value not in VALID_TRUST_LEVELS:
        logger.warning(
            "[autonomy] %r has invalid trust_level=%r, defaulting to 'guarded'",
            source_name, value,
        )
        return "guarded"
    return value


def parse_permission_fields(data: dict[str, Any], source_name: str) -> dict[str, Any]:
    """Extract the permission triple from a schedule/anchor table.

    Returns ``{"trust_level", "allowed_tools", "scope_grants"}``:

    - ``trust_level``: ``None`` when absent (caller applies its own default),
      otherwise validated to a known level (invalid → ``"guarded"``).
    - ``allowed_tools``: list of tool-name strings; a non-list is dropped to
      ``[]``.
    - ``scope_grants``: list of grant dicts, each keeping at least ``resource``
      and ``action``; malformed entries (wrong type, missing keys) are dropped
      individually so one bad grant doesn't void the rest.
    """
    raw_tl = data.get("trust_level")
    trust_level = None if raw_tl is None else validate_trust_level(str(raw_tl), source_name)

    raw_tools = data.get("allowed_tools")
    allowed_tools = [str(t) for t in raw_tools] if isinstance(raw_tools, list) else []

    raw_grants = data.get("scope_grants")
    scope_grants: list[dict[str, Any]] = []
    if isinstance(raw_grants, list):
        for g in raw_grants:
            if isinstance(g, dict) and "resource" in g and "action" in g:
                scope_grants.append(g)
            else:
                logger.warning(
                    "[autonomy] %r has a malformed scope_grant %r; skipping it",
                    source_name, g,
                )

    return {
        "trust_level": trust_level,
        "allowed_tools": allowed_tools,
        "scope_grants": scope_grants,
    }
