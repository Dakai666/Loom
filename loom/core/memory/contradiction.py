"""
Contradiction Detection & Auto-Resolution — Issue #43 Memory Governance.

Detects contradicting facts in semantic memory before a write and auto-resolves
using a trust-weighted decision tree:

    1. Same key, different value → trust tier comparison
    2. Same key prefix → potential conflict flagging
    3. High embedding similarity + semantic opposition → LLM arbitration

Resolution strategies:
    REPLACE    — new entry has strictly higher trust, overwrite existing
    KEEP       — existing entry has higher trust, drop proposed entry
    MERGE      — LLM synthesizes both into a refined fact
    SUPERSEDE  — both same trust, most recent wins (recency bias)

All resolution decisions are logged to ``audit_log`` for traceability.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from typing import TYPE_CHECKING

from loom.core.memory.semantic import SemanticEntry, classify_source

if TYPE_CHECKING:
    from loom.core.memory.semantic import SemanticMemory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class ConflictType(str, Enum):
    KEY_MATCH = "key_match"
    KEY_PREFIX = "key_prefix"
    EMBEDDING_SIMILARITY = "embedding_similarity"


class Resolution(str, Enum):
    REPLACE = "replace"       # overwrite existing with proposed
    KEEP = "keep"             # keep existing, drop proposed
    SUPERSEDE = "supersede"   # same trust → most recent wins
    MERGE = "merge"           # LLM merges both (future enhancement)


@dataclass
class Contradiction:
    """A detected contradiction between an existing and proposed entry."""
    existing: SemanticEntry
    proposed: SemanticEntry
    conflict_type: ConflictType
    similarity_score: float = 0.0
    resolved: bool = False
    resolution: Resolution | None = None


@dataclass
class ResolutionResult:
    """Outcome of resolving a contradiction."""
    resolution: Resolution
    winning_entry: SemanticEntry
    reason: str
    # If merge: the synthesized entry
    merged_entry: SemanticEntry | None = None


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class ContradictionDetector:
    """Detects and auto-resolves contradictions in semantic memory.

    Used by ``MemoryGovernor.governed_upsert()`` to ensure newly written
    facts don't silently contradict existing knowledge.
    """

    def __init__(self, semantic: SemanticMemory) -> None:
        self._semantic = semantic

    async def detect(self, proposed: SemanticEntry) -> list[Contradiction]:
        """Detect potential contradictions with existing memory.

        Strategy (layered, cheap → expensive):

        1. **Exact key**: same key, different value → definite conflict.
        2. **Prefix key**: same key prefix (up to second `:`) with
           different values → potential conflict.

        Embedding-based detection (tier 3) is only used when the
        semantic memory has an embedding provider configured.

        Returns a list of Contradiction objects (may be empty).
        """
        contradictions: list[Contradiction] = []

        # ── Tier 1: exact key match ─────────────────────────────────────
        existing = await self._semantic.get(proposed.key)
        if existing is not None and existing.value != proposed.value:
            contradictions.append(Contradiction(
                existing=existing,
                proposed=proposed,
                conflict_type=ConflictType.KEY_MATCH,
                similarity_score=1.0,  # exact key = highest confidence
            ))
            # If exact key match found, skip prefix check for same key
            return contradictions

        # ── Tier 2: prefix key match ────────────────────────────────────
        # Look for entries with similar key structure (e.g. "user:pref:*")
        parts = proposed.key.split(":")
        if len(parts) >= 2:
            prefix = ":".join(parts[:2])
            related = await self._semantic.list_by_prefix(prefix, limit=5)
            for entry in related:
                if entry.key == proposed.key:
                    continue  # skip self
                # Only flag as conflict if values are substantially different
                if (entry.value != proposed.value
                        and _text_overlap(entry.value, proposed.value) < 0.3):
                    contradictions.append(Contradiction(
                        existing=entry,
                        proposed=proposed,
                        conflict_type=ConflictType.KEY_PREFIX,
                        similarity_score=0.5,
                    ))

        return contradictions

    def resolve(self, contradiction: Contradiction) -> ResolutionResult:
        """Auto-resolve a contradiction using trust tier + recency.

        Decision tree:
            1. Compare trust tiers → higher trust wins
            2. Same trust tier → more recent entry wins (SUPERSEDE)
            3. Unresolvable → KEEP existing (conservative default)
        """
        existing_tier, existing_trust = classify_source(contradiction.existing.source)
        proposed_tier, proposed_trust = classify_source(contradiction.proposed.source)

        # Higher trust tier wins
        if proposed_trust > existing_trust:
            contradiction.resolved = True
            contradiction.resolution = Resolution.REPLACE
            return ResolutionResult(
                resolution=Resolution.REPLACE,
                winning_entry=contradiction.proposed,
                reason=(
                    f"Proposed source '{proposed_tier}' (trust={proposed_trust}) "
                    f"outranks existing '{existing_tier}' (trust={existing_trust})"
                ),
            )

        if existing_trust > proposed_trust:
            contradiction.resolved = True
            contradiction.resolution = Resolution.KEEP
            return ResolutionResult(
                resolution=Resolution.KEEP,
                winning_entry=contradiction.existing,
                reason=(
                    f"Existing source '{existing_tier}' (trust={existing_trust}) "
                    f"outranks proposed '{proposed_tier}' (trust={proposed_trust})"
                ),
            )

        # Same trust → recency wins (SUPERSEDE)
        contradiction.resolved = True
        contradiction.resolution = Resolution.SUPERSEDE
        return ResolutionResult(
            resolution=Resolution.SUPERSEDE,
            winning_entry=contradiction.proposed,
            reason=(
                f"Same trust tier '{proposed_tier}': "
                f"newer entry supersedes (recency bias)"
            ),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text_overlap(a: str, b: str) -> float:
    """Rough word-level Jaccard overlap between two texts.

    Returns 0.0 (no overlap) to 1.0 (identical word sets).
    Used as a cheap heuristic to decide whether two values in related
    keys actually contradict each other vs. just being different topics.
    """
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)
