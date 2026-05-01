"""
Precondition checks for Code_Weaver skill.

Code_Weaver is a unified coding skill — it modifies code, runs tests,
performs analysis, and interacts with GitHub. These checks enforce
safety invariants that the SKILL.md describes as discipline but cannot
enforce at the framework level.
"""

from __future__ import annotations

import asyncio


async def require_git_repo(call) -> bool:
    """Ensure we're inside a git repository before modifying anything.

    Code_Weaver's entire workflow (branch, commit, PR, diff) assumes git.
    Running outside a repo would produce broken output or wrong analysis.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "--is-inside-work-tree",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return proc.returncode == 0


async def reject_force_push(call) -> bool:
    """Block ``git push --force`` and variants to prevent history rewrite.

    Code_Weaver Layer 1 原則 2（Scope 先確認）+ 原則 5（驗證先於產出）:
    Force-pushing bypasses review and can destroy collaborators' work.
    """
    cmd = call.args.get("command", "")
    if "push" not in cmd:
        return True
    # Check for --force, -f, --force-with-lease (all are force variants)
    tokens = cmd.split()
    for i, token in enumerate(tokens):
        if token in ("--force", "-f", "--force-with-lease"):
            return False
    return True