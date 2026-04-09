"""
Precondition checks for systematic_code_analyst skill (Issue #64 Phase B).

systematic_code_analyst is a **read-only** analysis skill.  It must never
modify the codebase it's analyzing.  This check elevates the SKILL.md
discipline rule ("不要幫忙重構，除非使用者明確要求") from a suggestion
into a framework-enforced hard gate.
"""

from __future__ import annotations


async def reject_write_operations(call) -> bool:
    """Block all write_file calls.  Analysis skill is strictly read-only.

    The analyst's job is to observe and report, never to modify.
    If modification is needed, the user should switch to loom_engineer.
    """
    return False
