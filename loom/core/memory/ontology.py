"""
Memory ontology — single source of truth for fact classification axes.

Every semantic / relational fact is classified along three axes:

  - source    : trust tier (existing — see semantic.classify_source)
  - domain    : semantic territory (this module — DOMAINS)
  - temporal  : lifecycle state    (this module — TEMPORALS)

Reference: outputs/doc/memory_ontology_draft_2026-05-03.md
"""

from __future__ import annotations

from typing import Final


# ---------------------------------------------------------------------------
# Domain — semantic territory of a fact
# ---------------------------------------------------------------------------

DOMAIN_SELF: Final = "self"            # Agent identity, principles, self-awareness
DOMAIN_USER: Final = "user"            # User preferences, relationship, known/unknown
DOMAIN_PROJECT: Final = "project"      # Architecture decisions, config, workflow
DOMAIN_KNOWLEDGE: Final = "knowledge"  # External knowledge, tool usage, research

DOMAINS: Final = frozenset({
    DOMAIN_SELF,
    DOMAIN_USER,
    DOMAIN_PROJECT,
    DOMAIN_KNOWLEDGE,
})

DEFAULT_DOMAIN: Final = DOMAIN_KNOWLEDGE


# ---------------------------------------------------------------------------
# Temporal — lifecycle state of a fact
# ---------------------------------------------------------------------------

TEMPORAL_EPHEMERAL: Final = "ephemeral"  # Session-scoped, dies on session end
TEMPORAL_RECENT: Final = "recent"        # Active within recent window (~7d)
TEMPORAL_MILESTONE: Final = "milestone"  # Permanent anchor, never auto-decays
TEMPORAL_ARCHIVED: Final = "archived"    # Long-untouched, near-decay

TEMPORALS: Final = frozenset({
    TEMPORAL_EPHEMERAL,
    TEMPORAL_RECENT,
    TEMPORAL_MILESTONE,
    TEMPORAL_ARCHIVED,
})

DEFAULT_TEMPORAL: Final = TEMPORAL_RECENT


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def normalize_domain(value: str | None) -> str:
    """Return a valid domain — falls back to DEFAULT_DOMAIN for unknown/None."""
    if value and value in DOMAINS:
        return value
    return DEFAULT_DOMAIN


def normalize_temporal(value: str | None) -> str:
    """Return a valid temporal — falls back to DEFAULT_TEMPORAL for unknown/None."""
    if value and value in TEMPORALS:
        return value
    return DEFAULT_TEMPORAL
