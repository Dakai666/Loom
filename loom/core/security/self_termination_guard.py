"""
Self-termination guard — Issue #98.

Prevents the Agent from executing commands that would kill or otherwise
terminate the Loom process itself (pkill loom, killall loom, etc.).

Patterns are deliberately narrow: they only match commands that target
Loom's own process names.  Generic "kill" patterns are NOT included
because they would produce too many false positives in normal use.

Usage::

    from loom.core.security.self_termination_guard import SelfTerminationGuard

    guard = SelfTerminationGuard()
    verdict = guard.check("pkill -f loom")
    if verdict.is_blocked:
        raise PermissionError(f"Blocked: {verdict.reason}")

The guard is intentionally stateless and has no external dependencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Process names that, if killed, would terminate Loom itself.
_PROCESS_NAMES = r"\b(?:hermes|loom|loom\.py|gateway|cli\.py)\b"

# Commands that, when combined with _PROCESS_NAMES, constitute
# a self-termination threat.
_KILL_CMDS = r"\b(?:pkill|killall|kill)\b"


# ---------------------------------------------------------------------------
# Pattern groups
# ---------------------------------------------------------------------------

# 1. Bare kill + target:   pkill loom, killall -f loom, pkill -9 gateway, etc.
#    Optional flags between command and target are covered.
_PATTERN_BARE_KILL = re.compile(
    rf"""^                # anchor to avoid partial-word matches
    (?:pkill|killall)\s+(?:-\w+\s+)*  # pkill/killall with optional flags
    ({_PROCESS_NAMES})                   # target process name
    (?:\s|$|\s+[^\s])                   # followed by space, end, or non-flag arg
    """,
    re.VERBOSE | re.IGNORECASE,
)

# 2. kill via command substitution:  kill $(pgrep loom), kill -TERM `pgrep gateway`
#    We look for the kill command, optional flags, then $(...) or `...` containing
#    any of the target names.  Simple and correct over exhaustive.
_PATTERN_KILL_CMD_SUB = re.compile(
    rf"""(?:\bkill)\s+(?:-\w+\s+)*     # kill with optional flags
    (?:                                # command substitution:
        \$ \( [^)]*? ({_PROCESS_NAMES}) [^)]* \)   # $( ... target ... )
    |
        ` [^`]*   ({_PROCESS_NAMES})   [^`]* `      # ` ... target ... `
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# 3a. Detach operator followed by a Loom target:  nohup gateway run, disown loom
_PATTERN_DETACH_TARGET = re.compile(
    rf"""\b(?:nohup|disown|setsid)\b\s+\S*\s*
    ({_PROCESS_NAMES})""",
    re.VERBOSE | re.IGNORECASE,
)

# 3b. Loom process backgrounded at end of command:  gateway run &
_PATTERN_LOOM_BACKGROUND = re.compile(
    rf"""^({_PROCESS_NAMES})\b[^\n&]*&\s*$""",
    re.VERBOSE | re.IGNORECASE,
)

# 4. Persistence mechanisms (warn only, not block)
_PATTERN_AUTHORIZED_KEYS = re.compile(
    r"authorized_keys",
    re.IGNORECASE,
)
_PATTERN_CRONTAB_MODIFY = re.compile(
    r"crontab\s+-(?:e|l|-r)",
    re.IGNORECASE,
)
_PATTERN_SERVICE_ENABLE = re.compile(
    r"(?:systemctl\s+(?:enable|start)|service\s+(?:enable|start))\s+",
    re.IGNORECASE,
)
_PATTERN_UPDATE_RC_D = re.compile(
    r"update-rc\.d",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GuardVerdict:
    """Result of a self-termination guard check."""
    is_blocked: bool
    verdict: str           # "allow" | "block" | "warn"
    pattern_key: str
    description: str


class SelfTerminationGuard:
    """
    Stateless guard that checks a command string for self-termination
    and related dangerous patterns.

    All patterns are class-level constants, so instantiating the guard
    has zero overhead and you may reuse a single instance across all calls.
    """

    __slots__ = ()

    def check(self, command: str) -> GuardVerdict:
        """
        Check *command* for self-termination or persistence patterns.

        Returns a ``GuardVerdict``:

        - ``verdict == "block"`` — command matches a self-termination pattern;
          it MUST NOT be executed.
        - ``verdict == "warn"`` — command matches a persistence-related pattern;
          raise a warning but do not automatically block.
        - ``verdict == "allow"`` — no patterns matched.
        """
        if not command or not command.strip():
            return GuardVerdict(False, "allow", "", "")

        # --- Block patterns ---
        # 1. Bare pkill/killall targeting Loom processes
        if _PATTERN_BARE_KILL.search(command):
            m = _PATTERN_BARE_KILL.search(command)
            return GuardVerdict(
                is_blocked=True,
                verdict="block",
                pattern_key="bare_kill",
                description=(
                    f"Self-termination: command would kill Loom process '{m.group(1)}'. "
                    f"Matched: {m.group(0)!r}"
                ),
            )

        # 2. kill via command substitution $(pgrep loom) / `pgrep gateway`
        if _PATTERN_KILL_CMD_SUB.search(command):
            m = _PATTERN_KILL_CMD_SUB.search(command)
            return GuardVerdict(
                is_blocked=True,
                verdict="block",
                pattern_key="kill_cmd_sub",
                description=(
                    f"Self-termination: kill command targets a Loom process via "
                    f"command substitution. Matched: {m.group(0)!r}"
                ),
            )

        # 3. Detach operator targeting Loom:  disown loom, nohup gateway run
        if _PATTERN_DETACH_TARGET.search(command):
            m = _PATTERN_DETACH_TARGET.search(command)
            return GuardVerdict(
                is_blocked=True,
                verdict="block",
                pattern_key="detach_target",
                description=(
                    f"Self-termination: detach operator targets Loom process "
                    f"'{m.group(1)}'. Matched: {m.group(0)!r}"
                ),
            )

        # 4. Loom process backgrounded at end of command:  gateway run &
        if _PATTERN_LOOM_BACKGROUND.search(command):
            m = _PATTERN_LOOM_BACKGROUND.search(command)
            return GuardVerdict(
                is_blocked=True,
                verdict="block",
                pattern_key="loom_background",
                description=(
                    f"Self-termination: Loom process '{m.group(1)}' "
                    f"would be backgrounded and detached from supervision. "
                    f"Matched: {m.group(0)!r}"
                ),
            )

        # --- Warn patterns (persistence mechanisms) ---
        if _PATTERN_AUTHORIZED_KEYS.search(command):
            return GuardVerdict(False, "warn", "authorized_keys",
                "Persistence warning: command may modify authorized_keys.")
        if _PATTERN_CRONTAB_MODIFY.search(command):
            return GuardVerdict(False, "warn", "crontab_modify",
                "Persistence warning: command may modify crontab.")
        if _PATTERN_SERVICE_ENABLE.search(command):
            return GuardVerdict(False, "warn", "service_enable",
                "Persistence warning: command may enable/start a system service.")
        if _PATTERN_UPDATE_RC_D.search(command):
            return GuardVerdict(False, "warn", "update_rc_d",
                "Persistence warning: command may modify runlevel links.")

        return GuardVerdict(False, "allow", "", "")

    def is_allowed(self, command: str) -> bool:
        """Shorthand: returns True only when verdict is "allow"."""
        return self.check(command).verdict == "allow"

    def is_blocked(self, command: str) -> bool:
        """Shorthand: returns True when the command MUST NOT execute."""
        return self.check(command).verdict == "block"
