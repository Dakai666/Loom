"""
Precondition checks for meta-skill-engineer skill (Issue #64 Phase B).

meta-skill-engineer creates and iterates SKILL.md files and test cases.
Its write scope must be limited to skills/ directories — it should never
modify framework code, user code, or system files.
"""

from __future__ import annotations

import os


async def require_skills_dir_target(call) -> bool:
    """Ensure write_file targets a skills/ directory.

    meta-skill-engineer only produces SKILL.md files, test cases, and
    agent prompts — all within skills/.  Writing outside this boundary
    is a scope violation.
    """
    path = call.args.get("path", "")
    # Normalize path for consistent checking
    normalized = os.path.normpath(path)
    # Accept both absolute and relative paths containing /skills/ or starting with skills/
    return "/skills/" in normalized or normalized.startswith("skills/") or normalized.startswith("skills" + os.sep)
