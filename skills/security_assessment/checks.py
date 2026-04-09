"""
Precondition checks for security_assessment skill (Issue #64 Phase B).

security_assessment performs penetration testing, code review, and risk
assessment.  These checks enforce the skill's red lines at the framework
level — particularly the prohibition on destructive commands and
production environment operations.
"""

from __future__ import annotations

import os


async def reject_destructive_commands(call) -> bool:
    """Block destructive shell commands during security assessment.

    Red line #3: "Exploitation 階段嚴禁造成資料損失或服務中斷".
    This check catches common destructive patterns.  It is not
    exhaustive — defense in depth with BlastRadiusMiddleware.
    """
    cmd = call.args.get("command", "")
    if not cmd:
        return True

    destructive_patterns = [
        "rm -rf",
        "rm -r /",
        "DROP TABLE",
        "DROP DATABASE",
        "DELETE FROM",
        "TRUNCATE",
        "mkfs",
        "dd if=",
        "> /dev/sd",
        "format c:",
        ":(){:|:&};:",   # fork bomb
    ]
    cmd_lower = cmd.lower()
    return not any(pattern.lower() in cmd_lower for pattern in destructive_patterns)


async def reject_production_env(call) -> bool:
    """Block command execution in production environments.

    Red line #2: "所有測試必須在隔離環境進行".
    Checks LOOM_ENV and common production indicators.
    """
    env = os.environ.get("LOOM_ENV", "").lower()
    if env == "production":
        return False

    node_env = os.environ.get("NODE_ENV", "").lower()
    if node_env == "production":
        return False

    return True
