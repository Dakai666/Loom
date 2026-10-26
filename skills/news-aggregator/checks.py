"""
Precondition checks for news-aggregator skill.

Limits run_bash to the scripts/ directory under skills/news-aggregator/
to prevent arbitrary command execution.

2026-10-24: this guard was written with the key `check:` instead of `ref:` in
SKILL.md's frontmatter.  Loom's parser only accepts `ref:`, so the whole entry
was silently discarded and the guard never ran — for four months.  It looked
like protection and was not.  The frontmatter is fixed; this file is the
mechanism it now actually reaches.

Also fixed on 2026-10-24: the original scope would have blocked the morning
briefing's own `mkdir -p news/YYYY-MM-DD` step.  The briefing runs unattended
at 08:00, so a guard that breaks it is worse than no guard.  The scope below
allows exactly that one operation and nothing else.
"""

from __future__ import annotations

import os
import re

# Commands that are legitimately part of the briefing workflow but live
# outside scripts/.  Each is matched narrowly — this is not a general
# "allow mkdir" hole.
_ALLOWED_OUTSIDE = (
    # `mkdir -p news/YYYY-MM-DD` — create today's briefing output directory.
    re.compile(r"^mkdir\s+-p\s+news/\d{4}-\d{2}-\d{2}\s*$"),
    # `mkdir news/YYYY-MM-DD` (without -p) — same purpose.
    re.compile(r"^mkdir\s+news/\d{4}-\d{2}-\d{2}\s*$"),
    # 2026-10-26: step 4 — download today's cover image to its one fixed
    # path.  Only -s/-L/--max-time flags; the URL must be http(s) and
    # double-quoted with no `$`, backtick, backslash or `"` inside, so
    # nothing in it can expand or break out of the quotes.
    re.compile(
        r"^curl(?:\s+(?:-s|-L|-sL|-Ls|--max-time\s+\d+))*"
        r"\s+-o\s+news/\d{4}-\d{2}-\d{2}/cover\.jpg"
        r'\s+"https?://[^"$`\\\s]+"\s*$'
    ),
)


async def scripts_only_bash(call) -> bool:
    """Ensure run_bash stays inside the skill's legitimate scope.

    Allowed:
      * anything referencing skills/news-aggregator/scripts/ — the primary
        action (running fetch_news.py and helpers)
      * `mkdir news/YYYY-MM-DD` — the briefing's own output-directory step
      * `curl ... -o news/YYYY-MM-DD/cover.jpg "<url>"` — step 4 cover image

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

    # Match the raw command, not `normalized`: normpath mangles URLs
    # (`http://` -> `http:/`) and the shell runs the raw string, so
    # `x/../mkdir ...` must not pass as `mkdir ...`.
    for pattern in _ALLOWED_OUTSIDE:
        if pattern.match(command):
            return True

    return False
