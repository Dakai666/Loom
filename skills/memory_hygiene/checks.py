"""
Precondition checks for memory_hygiene skill (Issue #64 Phase B).

memory_hygiene maintains Loom's memory system health.  Because it deals
with the agent's long-term memory, safety is paramount:
- A backup must exist before any cleanup runs
- Direct SQL mutation is forbidden — all writes go through framework APIs
"""

from __future__ import annotations

import os
from pathlib import Path


async def require_memory_backup(call) -> bool:
    """Ensure a recent memory.db backup exists before cleanup operations.

    Checks for the existence of a backup file at ~/.loom/memory.db.bak
    or ~/.loom/backups/memory.db.*.  If no backup exists, the check
    fails — the user (or agent) must create one first.

    Read-only commands (ls, du, cat, sqlite3 ... .schema) are allowed
    without backup.
    """
    cmd = call.args.get("command", "")

    # Allow read-only diagnostic commands without backup
    readonly_prefixes = [
        "ls ", "du ", "stat ", "wc ", "file ",
        "cat ", "head ", "tail ",
        "sqlite3 ",  # Will be further checked by reject_direct_db_mutation
    ]
    cmd_stripped = cmd.strip()
    if any(cmd_stripped.startswith(p) for p in readonly_prefixes):
        # sqlite3 read-only queries are OK
        if cmd_stripped.startswith("sqlite3 "):
            readonly_sql = [".schema", ".tables", "SELECT", "PRAGMA", ".mode", ".headers"]
            if any(kw in cmd for kw in readonly_sql):
                return True
        else:
            return True

    # For non-read-only commands, require backup
    loom_dir = Path.home() / ".loom"
    backup_paths = [
        loom_dir / "memory.db.bak",
        loom_dir / "backups",
    ]

    for bp in backup_paths:
        if bp.is_file():
            return True
        if bp.is_dir() and any(bp.iterdir()):
            return True

    return False


async def reject_direct_db_mutation(call) -> bool:
    """Block direct SQL mutation commands on memory.db.

    All memory writes must go through framework APIs (memorize, relate,
    etc.) which enforce governance, contradiction checks, and audit trails.
    Direct SQL bypasses all of these.
    """
    cmd = call.args.get("command", "").upper()

    # Only check commands that touch sqlite3
    if "SQLITE3" not in cmd and "MEMORY.DB" not in cmd:
        return True

    # Block mutation SQL keywords
    mutation_keywords = [
        "INSERT", "UPDATE", "DELETE", "DROP", "ALTER",
        "REPLACE", "TRUNCATE", "CREATE TABLE",
    ]
    return not any(kw in cmd for kw in mutation_keywords)
