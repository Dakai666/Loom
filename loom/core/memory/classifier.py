"""
Domain classifier — heuristic prefix-based domain inference for memory facts.

When a caller (memorize tool, session compressor, dreaming) doesn't know
the right domain, the governor calls :func:`infer_domain` to upgrade the
default ``knowledge`` to a more specific axis based on the entry's key /
value shape.

The heuristic looks at:

  1. Key namespace prefixes (``user:`` / ``project:`` / ``loom:identity:`` etc.)
  2. Subject prefixes for relational triples
  3. Source tier (``user_explicit`` → user-aimed unless key says otherwise)

This is intentionally **rule-based, not LLM-based**. P1-C will add an
LLM-assisted batch classifier for legacy data migration — that classifier
shares the canonical DOMAINS enum with this module so heuristic-vs-LLM
disagreement is a strict comparison.
"""

from __future__ import annotations

from loom.core.memory.ontology import (
    DOMAIN_KNOWLEDGE,
    DOMAIN_PROJECT,
    DOMAIN_SELF,
    DOMAIN_USER,
)


# Lower-cased key prefix → domain.  Order matters only for documentation;
# matching is exact-prefix, so longer namespaces are checked first below.
_KEY_PREFIX_RULES: tuple[tuple[str, str], ...] = (
    # Self — agent identity / principles / self-awareness
    ("loom:identity", DOMAIN_SELF),
    ("loom:self", DOMAIN_SELF),
    ("agent:identity", DOMAIN_SELF),
    ("self:", DOMAIN_SELF),
    ("identity:", DOMAIN_SELF),
    # User
    ("user:", DOMAIN_USER),
    ("u:", DOMAIN_USER),
    # Project — code architecture, config, workflow
    ("project:", DOMAIN_PROJECT),
    ("loom:config", DOMAIN_PROJECT),
    ("loom:arch", DOMAIN_PROJECT),
    ("repo:", DOMAIN_PROJECT),
    ("config:", DOMAIN_PROJECT),
    # Knowledge — explicit knowledge namespace
    ("knowledge:", DOMAIN_KNOWLEDGE),
    ("fact:knowledge", DOMAIN_KNOWLEDGE),
    ("skill:", DOMAIN_KNOWLEDGE),
)


def infer_domain(key: str | None) -> str:
    """Best-effort domain inference from a fact key.

    Returns one of the four DOMAINS values. When no rule matches, returns
    ``DOMAIN_KNOWLEDGE`` — the safe default per Memory Ontology v0.1.
    """
    if not key:
        return DOMAIN_KNOWLEDGE
    k = key.lower().lstrip()
    for prefix, domain in _KEY_PREFIX_RULES:
        if k.startswith(prefix):
            return domain
    return DOMAIN_KNOWLEDGE
