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

An activity can recur (issue #526): ``time`` accepts a list, so one block
(``name = "pet"``, ``time = ["10:00", "19:00"]``) fires at every slot while
staying a single *identity* — one ``daily_weave`` section, one ``meaning``.
The loader expands such a block into one :class:`Anchor` per slot, giving each
a unique trigger name (``@HHMM`` suffix). Because recurrence has its own
expression, ``name`` is still required to be unique *across blocks* — a
duplicate block name remains a genuine error (keep-first).
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loom.autonomy.permission_fields import parse_permission_fields

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
    name: str       # the activity's *identity* — the join key into daily_weave
    meaning: str    # markdown body the chime injects (the "why" of this phase)
    public: bool = True
    trigger_suffix: str = ""  # "@HHMM" disambiguator for multi-time activities

    # Permission triple (issue #525) — the same fields a schedules.toml entry
    # uses, so a phase can do routine tool work (research, write a draft, run
    # the pet script) without re-asking DK every day. _deliver_phase_chime
    # forwards these onto the ChimeRequest; bot._apply_chime_permissions then
    # pre-authorises them for the phase turn (and revokes after).
    trust_level: str | None = None              # None = no override (normal gating)
    allowed_tools: tuple[str, ...] = ()
    scope_grants: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def trigger_name(self) -> str:
        # ``name`` is the activity identity (one weave section per name), so it
        # is NOT unique when an activity recurs (issue #526: pet at 10:00 and
        # 19:00). The cron evaluator keys triggers by name, so each fire needs a
        # unique trigger — the loader appends a per-slot ``@HHMM`` suffix when an
        # activity has >1 time. Single-time activities keep the bare
        # ``circadian:phase_<name>`` form (backward-compatible).
        return f"circadian:phase_{self.name}{self.trigger_suffix}"


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

    ``time`` may be a scalar ``"HH:MM"`` or a list of them (issue #526). A
    list expands to one anchor per *valid, distinct* slot, all sharing name +
    meaning; invalid slots inside the list are dropped individually. When a
    block contributes >1 slot, each anchor's trigger name carries an ``@HHMM``
    suffix so the cron evaluator (which keys triggers by name) sees them as
    distinct fires instead of silently collapsing them onto the first.
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
            raw_time = entry["time"]
            name = str(entry["name"])
        except KeyError as exc:
            logger.warning("[circadian] rhythm anchor #%d missing required field %s; skipping", idx, exc)
            continue

        # ``time`` is a scalar or a list. Normalise to an ordered, de-duplicated
        # list of valid HH:MM slots; bad slots are dropped individually so one
        # typo in a list never silences the whole activity.
        raw_slots = raw_time if isinstance(raw_time, list) else [raw_time]
        slots: list[str] = []
        for raw_slot in raw_slots:
            slot = str(raw_slot)
            if not _validate_hhmm(slot):
                logger.warning("[circadian] rhythm anchor %r has invalid time %r; skipping that slot", name, slot)
                continue
            if slot not in slots:
                slots.append(slot)
        if not slots:
            logger.warning("[circadian] rhythm anchor %r has no valid time; skipping", name)
            continue
        if name in seen:
            logger.warning("[circadian] rhythm anchor name=%r duplicated; keeping first occurrence", name)
            continue

        meaning = str(entry.get("meaning", "")).strip()
        public = bool(entry.get("public", True))
        # Permission fields parse through the shared autonomy helper (issue
        # #525) — same code path as schedules.toml — and belong to the activity
        # identity, so every expanded slot of a recurring activity shares them.
        perms = parse_permission_fields(entry, f"rhythm anchor {name!r}")
        allowed_tools = tuple(perms["allowed_tools"])
        scope_grants = tuple(perms["scope_grants"])
        trust_level = perms["trust_level"]
        # Suffix the trigger only when the activity genuinely recurs: a single
        # surviving slot keeps the bare ``circadian:phase_<name>`` (backward
        # compatible), so the @HHMM form reflects reality, not declared intent.
        multi = len(slots) > 1
        for slot in slots:
            suffix = f"@{slot.replace(':', '')}" if multi else ""
            anchors.append(Anchor(
                time=slot, name=name, meaning=meaning, public=public,
                trigger_suffix=suffix, trust_level=trust_level,
                allowed_tools=allowed_tools, scope_grants=scope_grants,
            ))
        seen.add(name)

    return anchors
