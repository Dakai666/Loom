"""One permission mode per session (#704): 安全 / 自理 / YOLO.

"Stop / Loom decides / ask DK" is the *outcome* of each decision, not a
mode. The mode only changes who decides the ordinary calls between the
hard guardrails, which hold in all three:

- 安全 (``safe``): the conservative rules and the approval classifier as
  before #704; anything else asks DK (skipped when nobody attends).
- 自理 (``self``, the default on every entry point): as 安全, and an
  operator's explicit delegation covers its delivery step. A post the
  current turn's trusted intent asks for, to the issue or PR it names,
  goes to Luna instead of asking DK at each step (``approval.delegated_post``).
- YOLO (``yolo``): within the hard guardrails, run without Luna and without
  asking because an effect is unknown or no grant covers it
  (``approval.hard_boundary``). Crossing a hard guardrail still asks.

Only a runtime-verified operator switches the mode: the CLI's ``--mode``
flag or ``/mode``, Discord's ``/mode`` from an allowlisted user. No tool
sets it, so the model, a skill, a document or tool output cannot. Each
switch is written to the ledger (``permission_mode_change``) and applies
from the next tool dispatch: BlastRadius reads the mode once per call.
"""

from __future__ import annotations

import logging
import uuid
from enum import Enum
from typing import Any

_log = logging.getLogger(__name__)


class PermissionMode(Enum):
    SAFE = "safe"
    SELF = "self"
    YOLO = "yolo"

    @property
    def label(self) -> str:
        return _LABELS[self]


DEFAULT_MODE = PermissionMode.SELF

_LABELS = {
    PermissionMode.SAFE: "安全",
    PermissionMode.SELF: "自理",
    PermissionMode.YOLO: "YOLO",
}
_ALIASES = {
    "safe": PermissionMode.SAFE, "安全": PermissionMode.SAFE,
    "self": PermissionMode.SELF, "自理": PermissionMode.SELF,
    "yolo": PermissionMode.YOLO,
}


def parse_mode(text: str) -> PermissionMode | None:
    """``safe``/``安全``, ``self``/``自理`` or ``yolo`` (any case), else None."""
    return _ALIASES.get(text.strip().lower())


MODE_NAMES = "safe（安全）/ self（自理）/ yolo"


async def record_mode_change(
    emitter: Any, *, old: PermissionMode, new: PermissionMode,
    operator: str, entry: str,
) -> None:
    """Write one ``permission_mode_change`` row. A switch happens between
    turns, so it gets its own turn id. A failed write is logged, never
    raised: the switch itself already happened."""
    payload = {"from": old.value, "to": new.value, "operator": operator, "entry": entry}
    if emitter is None:
        _log.warning("permission mode switched without a ledger: %s", payload)
        return
    try:
        await emitter.emit("permission_mode_change", payload,
                           turn_id=f"mode_{uuid.uuid4().hex[:12]}")
    except Exception:
        _log.exception("ledger permission_mode_change emit failed: %s", payload)


async def mode_command(session: Any, arg: str, *, operator: str, entry: str) -> str:
    """``/mode`` for every frontend: no argument shows the mode in effect,
    a mode name switches to it. The caller has verified the operator."""
    current: PermissionMode = session.perm.mode
    if not arg.strip():
        return f"Permission mode: {current.label} ({current.value}). Switch with /mode {MODE_NAMES}."
    new = parse_mode(arg)
    if new is None:
        return f"Unknown mode {arg.strip()!r}. Use one of: {MODE_NAMES}."
    if new is current:
        return f"Permission mode is already {current.label} ({current.value})."
    await session.set_permission_mode(new, operator=operator, entry=entry)
    return (f"Permission mode: {current.label} → {new.label} ({new.value}), "
            "from the next tool call. Hard guardrails hold in every mode.")
