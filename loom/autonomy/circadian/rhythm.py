"""
Rhythm table — the per-agent answer to "今天怎麼過".

doc/56 §3.3 draws a hard line: ``loom.toml`` is for cron / trigger *engine*
config, not for describing a day's life. So the daily phase anchors (when
``dawn`` fires, what ``shared_learning`` *means* to this agent) live in a
separate workspace-relative file — ``autonomy/circadian/rhythm.toml`` — that
each agent owns independently. 絲絲 in one workspace and 小晴 in another can
share the same circadian engine while their days look nothing alike.

The reader is tolerant by contract (issue #460 acceptance row 3): a missing
or malformed file logs a warning and returns ``[]`` so the dawn/close
lifecycle from PR1 keeps working. Anchors with bad time strings or duplicate
names are dropped individually, not whole-file rejected, so one typo never
silences the rest of the day.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Workspace-relative path. Per-agent isolation is by cwd (same convention as
# ``loom.toml`` itself), so a single hardcoded path is enough — different
# agents run in different workspaces and see different rhythm files.
DEFAULT_RHYTHM_PATH = Path("autonomy/circadian/rhythm.toml")


@dataclass(frozen=True)
class Anchor:
    """One time-anchored phase on the agent's day.

    ``meaning`` is markdown-flavoured text the cron handler injects verbatim
    into the ``<system_chime>`` body — *not* a bare phase enum (see doc/57 §
    "Phase chime prompt contract"). Keeping the life-semantics in data, not
    in code, is what lets per-agent rhythm tables stay independent.
    """

    time: str       # "HH:MM" local wall-clock in [autonomy.circadian].timezone
    name: str       # short identifier; becomes part of the cron trigger name
    meaning: str    # markdown body the chime injects (the "why" of this phase)
    public: bool = True

    @property
    def trigger_name(self) -> str:
        return f"circadian:phase_{self.name}"


def _validate_hhmm(value: str) -> bool:
    try:
        h_str, m_str = value.split(":")
        h, m = int(h_str), int(m_str)
    except (ValueError, AttributeError):
        return False
    return 0 <= h < 24 and 0 <= m < 60


def load_rhythm(path: Path | None = None) -> list[Anchor]:
    """Read the rhythm table, returning anchors in declaration order.

    Returns ``[]`` for any failure mode the engine should survive: file
    absent, unreadable bytes, invalid TOML, missing ``[[anchors]]`` array.
    Per-anchor errors (missing fields, bad time, duplicate name) are logged
    and the bad anchor skipped — the rest of the table still loads.
    """
    p = path or DEFAULT_RHYTHM_PATH
    if not p.exists():
        logger.info("[circadian] rhythm table not found at %s — engine runs with no phase anchors", p)
        return []
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        logger.warning("[circadian] rhythm table at %s unreadable (%s); no anchors registered", p, exc)
        return []

    anchors: list[Anchor] = []
    seen: set[str] = set()
    entries = raw.get("anchors")
    if not isinstance(entries, list):
        if entries is not None:
            logger.warning("[circadian] rhythm: 'anchors' must be a TOML array, got %s", type(entries).__name__)
        return []

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            logger.warning("[circadian] rhythm anchor #%d is not a table; skipping", idx)
            continue
        try:
            time = str(entry["time"])
            name = str(entry["name"])
        except KeyError as exc:
            logger.warning("[circadian] rhythm anchor #%d missing required field %s; skipping", idx, exc)
            continue
        if not _validate_hhmm(time):
            logger.warning("[circadian] rhythm anchor %r has invalid time %r; skipping", name, time)
            continue
        if name in seen:
            logger.warning("[circadian] rhythm anchor name=%r duplicated; keeping first occurrence", name)
            continue
        meaning = str(entry.get("meaning", "")).strip()
        public = bool(entry.get("public", True))
        anchors.append(Anchor(time=time, name=name, meaning=meaning, public=public))
        seen.add(name)

    return anchors
