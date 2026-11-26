"""One readable reason when a guard stops or asks (#704 S6).

Whichever layer holds a call back (the permission mode's decision, a hard
guardrail, read before write, scope, the argument schema), DK and Loom
Agent see one line in one format::

    守門・<layer>：<reason>

A confirm puts the session's permission mode in front of it
(``[自理] 守門・權限模式：Luna abstained``), on CLI and Discord alike. A
stop (an error the model reads) starts with the same line, then says what
to do next.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .mode import PermissionMode

GUARD_REASON_KEY = "guard_reason"

LAYERS = {
    "mode": "權限模式",
    "hard": "硬護欄",
    "read_before_write": "先讀後寫",
    "scope": "授權範圍",
    "schema": "參數格式",
}


@dataclass(frozen=True)
class GuardReason:
    layer: str  # a key of LAYERS
    reason: str

    def line(self, mode: PermissionMode | None = None) -> str:
        prefix = f"[{mode.label}] " if mode is not None else ""
        return f"{prefix}守門・{LAYERS[self.layer]}：{self.reason}"


def guard_error(layer: str, reason: str, guidance: str = "") -> str:
    """The error text of a stop: the reason line, then what to do."""
    line = GuardReason(layer, reason).line()
    return f"{line}\n{guidance}" if guidance else line


def _missing(diff: Any) -> str:
    missing = getattr(diff, "missing", None) or ()
    return ", ".join(f"{r.resource}:{r.action} → {r.selector}" for r in missing)


def ask_reason(note: str | None, verdict: Any, diff: Any) -> GuardReason:
    """Why a call goes to DK, or is skipped when nobody attends.

    *note* is what the decision point said (``hard: …``, a classifier
    reason, ``""`` when nothing could approve it, None when it was not
    consulted); *verdict* and *diff* are the scope check's."""
    from .approval import describe_note
    from .scope import PermissionVerdict

    note = note or ""
    if note.startswith("hard: "):
        return GuardReason("hard", note.removeprefix("hard: "))
    beyond = verdict == PermissionVerdict.EXPAND_SCOPE
    if note == "rule-refuse: scope_ceiling" or (beyond and not note):
        missing = _missing(diff)
        return GuardReason("scope", "beyond the declared scope" + (f" ({missing})" if missing else ""))
    if note:
        return GuardReason("mode", describe_note(note))
    return GuardReason("mode", "no rule or grant covers this call")


def _call_mode(call: Any) -> PermissionMode | None:
    try:
        return PermissionMode(call.metadata.get("permission_mode"))
    except ValueError:
        return None


def confirm_line(call: Any) -> str:
    """The reason line a confirm shows, with the mode in front."""
    reason = call.metadata.get(GUARD_REASON_KEY)
    if not isinstance(reason, GuardReason):
        reason = ask_reason(call.metadata.get("classifier_note"),
                            call.metadata.get("scope_verdict"), call.metadata.get("scope_diff"))
    return reason.line(_call_mode(call))
