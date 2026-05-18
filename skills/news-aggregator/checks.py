"""
Precondition checks for news-aggregator skill.

Limits run_bash to the scripts/ directory under skills/news-aggregator/
to prevent arbitrary command execution.
"""

from __future__ import annotations

import os


async def scripts_only_bash(call) -> bool:
    """Ensure run_bash commands execute only within skills/news-aggregator/scripts/.

    news-aggregator's primary action is running fetch_news.py and related
    scripts. Allowing arbitrary bash outside this scope defeats the purpose
    of the guard.
    """
    command = call.args.get("command", "")
    # Normalize path for consistent checking
    normalized = os.path.normpath(command)
    # Must reference paths under skills/news-aggregator/scripts/
    scripts_dir = os.path.join("skills", "news-aggregator", "scripts")
    return scripts_dir in normalized or normalized.startswith(scripts_dir)