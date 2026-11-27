"""
Schedule registry — the autonomy *work-item* data, split out of ``loom.toml``.

Issue #444: ``[[autonomy.schedules]]`` / ``[[autonomy.triggers]]`` used to live
inside ``loom.toml`` next to system config (cognition / memory / sandbox /
harness). That put two very different things on one mutation surface: an agent
editing a schedule (which it does routinely — it owns the schedule registry)
could fat-finger a sandbox key and shift Loom's whole startup posture. The
schedule registry now lives in its own ``autonomy/schedules.toml`` — same
per-agent, cwd-relative, ``.example``-tracked convention as
``autonomy/circadian/rhythm.toml``.

The master on/off switch (``[autonomy] enabled``) deliberately *stays* in
``loom.toml``: it's a system-posture decision, not a work item. Only the
per-schedule entries move here.

The reader is tolerant by contract (mirrors ``rhythm.load_rhythm``): a missing
or malformed file yields empty arrays so the daemon still starts; a single bad
entry is dropped individually, never failing the whole file. Per-entry
*semantic* validation of skill boundaries runs here; invalid references or
widening reject only that entry with ERROR. Legacy entries without skills
retain their contract with a migration WARNING. Chime target validation stays
in the daemon builder.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from loom.autonomy.skill_allows import SkillAllowsError, activity_permission_fields, activity_workspace

logger = logging.getLogger(__name__)

# Workspace-relative, cwd-anchored — identical isolation model to
# ``autonomy/circadian/rhythm.toml``: different agent = different workspace =
# different registry, one daemon. The daemon resolves this against the
# ``loom.toml`` directory so it survives being launched from elsewhere.
DEFAULT_SCHEDULES_PATH = Path("autonomy/schedules.toml")

_ARRAY_KEYS = ("schedules", "triggers")


def load_schedules(path: Path | None = None, *, workspace: Path | None = None) -> dict:
    """Read the schedule registry, returning ``{"schedules": [...], "triggers": [...]}``.

    Every failure mode the daemon should survive yields *empty* arrays: file
    absent, unreadable bytes, invalid TOML. A top-level key that isn't a TOML
    array is coerced to ``[]`` with a warning; non-table entries inside an array
    are skipped individually so one typo never silences the rest of the file.
    """
    p = path or DEFAULT_SCHEDULES_PATH
    if not p.exists():
        logger.info(
            "[autonomy] schedule registry not found at %s — daemon runs with no schedules",
            p,
        )
        return {"schedules": [], "triggers": []}
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        logger.warning(
            "[autonomy] schedule registry at %s unreadable (%s); no schedules registered",
            p,
            exc,
        )
        return {"schedules": [], "triggers": []}

    resolved: dict[str, list[dict]] = {}
    ws = activity_workspace(p, DEFAULT_SCHEDULES_PATH, workspace)
    for key in _ARRAY_KEYS:
        resolved[key] = []
        for entry in _coerce_array(raw, key):
            name = entry.get("name", "<unnamed>")
            try:
                if isinstance(entry.get("target"), dict) and "skills" in entry["target"]:
                    raise SkillAllowsError(
                        "target.skills is misplaced; put skills on the schedule table, "
                        "before [schedules.target] if present")
                perms = activity_permission_fields(entry, f"{key} {name!r}", ws)
            except SkillAllowsError as exc:
                logger.error("[autonomy] %s %r rejected: %s", key, name, exc)
                continue
            # Leave absent legacy fields absent; a skill reference resolves
            # both fields even when its allows is explicitly empty.
            resolved[key].append({**entry, **perms} if "skills" in entry else entry)
    return resolved


def _coerce_array(raw: dict, key: str) -> list[dict]:
    """Return ``raw[key]`` as a list of tables, dropping malformed entries."""
    entries = raw.get(key)
    if entries is None:
        return []
    if not isinstance(entries, list):
        logger.warning(
            "[autonomy] schedule registry: %r must be a TOML array of tables, got %s — ignoring",
            key,
            type(entries).__name__,
        )
        return []
    out: list[dict] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            logger.warning(
                "[autonomy] schedule registry: %s[%d] is not a table; skipping", key, idx
            )
            continue
        out.append(entry)
    return out
