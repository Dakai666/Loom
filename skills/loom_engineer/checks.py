"""
Precondition checks for loom_engineer skill (Issue #64 Phase B).

loom_engineer is an implementation skill — it modifies code, runs tests,
and produces commits.  These checks enforce safety invariants that the
SKILL.md describes as discipline but cannot enforce at the framework level.
"""

from __future__ import annotations

import asyncio


async def require_git_repo(call) -> bool:
    """Ensure we're inside a git repository before modifying anything.

    loom_engineer's entire workflow (branch, commit, PR) assumes git.
    Running outside a repo would produce broken output.
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

    loom_engineer principle #6: "review 先於 commit".
    Force-pushing bypasses review and can destroy others' work.
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
