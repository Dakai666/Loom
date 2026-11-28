"""
Precondition checks for news-aggregator skill.

Limits run_bash to the scripts/ directory under skills/news-aggregator/
to prevent arbitrary command execution.

2026-10-24: this guard was written with the key `check:` instead of `ref:` in
SKILL.md's frontmatter.  Loom's parser only accepts `ref:`, so the whole entry
was silently discarded and the guard never ran — for four months.  It looked
like protection and was not.  The frontmatter is fixed; this file is the
mechanism it now actually reaches.

"""

from __future__ import annotations

import os


async def scripts_only_bash(call) -> bool:
    """Ensure run_bash stays inside the skill's legitimate scope.

    Allowed:
      * anything referencing skills/news-aggregator/scripts/ — the primary
        action (running fetch_news.py and helpers)

    Everything else is denied.  The point is not to make the skill unable to
    work; it is to make sure a skill whose SKILL.md gets replaced cannot
    quietly gain the ability to run arbitrary commands.
    """
    command = (call.args.get("command", "") or "").strip()
    if not command:
        return True

    normalized = os.path.normpath(command)
    scripts_dir = os.path.join("skills", "news-aggregator", "scripts")
    if scripts_dir in normalized or normalized.startswith(scripts_dir):
        return True

    return False
