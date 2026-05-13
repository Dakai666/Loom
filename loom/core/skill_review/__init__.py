"""Skill review — pure ledger queries that surface per-skill usage history.

Consumers (doc/54 §4.3.2):
- ``skill_review`` agent tool (conversational pull, real-time)
- Weekly worker (cron, markdown render to outputs/self_check/)

Both go through the same :func:`query_skill_ledger` pure function so the
aggregation logic lives in one place. No LLM, no scoring — just shaped
event evidence the consumer can reason about.
"""

from loom.core.skill_review.query import (
    SkillEpisode,
    SkillUsageDigest,
    query_skill_ledger,
)
from loom.core.skill_review.render import render_digest_as_text

__all__ = [
    "SkillEpisode",
    "SkillUsageDigest",
    "query_skill_ledger",
    "render_digest_as_text",
]
