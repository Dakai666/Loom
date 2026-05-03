"""
Memory Lifecycle — single source of truth for fact decay & state transitions.

Replaces two scattered hardcoded half-lives (``semantic._DEFAULT_HALF_LIFE_DAYS``
and ``governance._prune_relational_decay``'s ``base_half_life``) with a
``(domain × temporal) → half_life_days`` table, and fixes issue #299
(non-dreaming relational triples never being cleaned).

State transitions per cycle (single threshold, two-stage):

  1. ``temporal == 'archived'`` AND eff_conf < threshold → DELETE
  2. ``temporal == 'recent'``    AND eff_conf < threshold → demote to archived
  3. ``temporal == 'milestone'`` → never (half-life = infinity for self/user/project;
                                         365d for knowledge — slowest decay class)
  4. ``temporal == 'ephemeral'`` → not handled here; session compressor expires them

Step 1 runs first so a row newly-demoted in step 2 doesn't get deleted in
the same cycle — its archived life starts fresh next pass.

``last_accessed_at`` (set by :meth:`MemorySearch._mark_accessed` on every
recall hit) is the time anchor when more recent than ``updated_at``, so a
fact still in active use never decays — N1 from PR #303 review.

Reference: outputs/doc/memory_ontology_draft_2026-05-03.md §3
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import TYPE_CHECKING

from loom.core.memory.ontology import (
    DOMAIN_KNOWLEDGE,
    DOMAIN_PROJECT,
    DOMAIN_SELF,
    DOMAIN_USER,
    TEMPORAL_ARCHIVED,
    TEMPORAL_MILESTONE,
    TEMPORAL_RECENT,
)

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Half-life table — single source of truth (issue #281 / Phase 0 §3)
# ---------------------------------------------------------------------------
#
# Adjusted from initial Phase 0 draft after PR #303 review N1: knowledge/recent
# was 60d, raised to 90d so legacy LLM-classified rows don't decay aggressively
# while we wait for explicit re-classification on touch.

_INF = math.inf

_HALF_LIFE_TABLE: dict[tuple[str, str], float] = {
    # recent — actively used facts
    (DOMAIN_SELF,      TEMPORAL_RECENT):    180.0,
    (DOMAIN_USER,      TEMPORAL_RECENT):     90.0,
    (DOMAIN_PROJECT,   TEMPORAL_RECENT):     90.0,
    (DOMAIN_KNOWLEDGE, TEMPORAL_RECENT):     90.0,  # was 60 — N1 concession
    # milestone — permanent anchors; only knowledge can ever expire
    (DOMAIN_SELF,      TEMPORAL_MILESTONE): _INF,
    (DOMAIN_USER,      TEMPORAL_MILESTONE): _INF,
    (DOMAIN_PROJECT,   TEMPORAL_MILESTONE): _INF,
    (DOMAIN_KNOWLEDGE, TEMPORAL_MILESTONE): 365.0,
    # archived — second-chance state; shorter half-lives so unused rows
    # progress to delete in a reasonable window
    (DOMAIN_SELF,      TEMPORAL_ARCHIVED):   30.0,
    (DOMAIN_USER,      TEMPORAL_ARCHIVED):   30.0,
    (DOMAIN_PROJECT,   TEMPORAL_ARCHIVED):   30.0,
    (DOMAIN_KNOWLEDGE, TEMPORAL_ARCHIVED):   14.0,
}

# Fallback used when a row's domain/temporal value is somehow outside the
# closed enums (shouldn't happen — dataclass __post_init__ normalizes).
_FALLBACK_HALF_LIFE = 90.0


def half_life_for(domain: str, temporal: str) -> float:
    """Return the half-life in days for a (domain, temporal) pair.

    ``math.inf`` means "never decays" — used by milestone-class facts in
    every domain except knowledge.
    """
    return _HALF_LIFE_TABLE.get((domain, temporal), _FALLBACK_HALF_LIFE)


def effective_confidence(
    confidence: float,
    updated_at: datetime,
    last_accessed_at: datetime | None,
    domain: str,
    temporal: str,
) -> float:
    """Time-decayed confidence using the (domain, temporal) half-life.

    The decay clock starts from ``max(updated_at, last_accessed_at)`` —
    a fact that's been recalled recently doesn't decay even if it hasn't
    been re-written. Returns at least 0.01 so a fully-decayed fact stays
    visible to audits.
    """
    half = half_life_for(domain, temporal)
    if half == _INF:
        return confidence
    anchor = updated_at
    if last_accessed_at is not None and last_accessed_at > anchor:
        anchor = last_accessed_at
    days = (datetime.now(UTC) - anchor).total_seconds() / 86400.0
    decayed = confidence * math.pow(2.0, -days / half)
    return max(0.01, round(decayed, 4))


# ---------------------------------------------------------------------------
# Cycle result
# ---------------------------------------------------------------------------

@dataclass
class LifecycleResult:
    """Per-table summary of one ``MemoryLifecycle.run()`` invocation."""
    semantic_examined:   int = 0
    semantic_archived:   int = 0
    semantic_deleted:    int = 0
    relational_examined: int = 0
    relational_archived: int = 0
    relational_deleted:  int = 0
    dry_run: bool = False

    @property
    def total_archived(self) -> int:
        return self.semantic_archived + self.relational_archived

    @property
    def total_deleted(self) -> int:
        return self.semantic_deleted + self.relational_deleted


# ---------------------------------------------------------------------------
# MemoryLifecycle
# ---------------------------------------------------------------------------

class MemoryLifecycle:
    """Run decay + state-transition cycle across semantic + relational tables.

    Single ``threshold`` (default 0.1) — both archive and delete steps use
    the same effective_confidence cutoff. Two-stage transition (recent →
    archived → deleted) gives a fact a structurally-meaningful "second
    chance" window before disappearing.
    """

    def __init__(
        self,
        db: "aiosqlite.Connection",
        threshold: float = 0.1,
    ) -> None:
        self._db = db
        self._threshold = threshold

    async def run(self, dry_run: bool = False) -> LifecycleResult:
        """Run one full lifecycle cycle. Returns counts; never raises."""
        result = LifecycleResult(dry_run=dry_run)

        # Order matters: deletes (already-archived rows still decayed)
        # run BEFORE demotes (recent rows newly below threshold). This
        # prevents a row from being demoted-then-deleted in the same pass —
        # newly-demoted rows get a fresh archived-state half-life window
        # to be re-accessed before the next cycle considers them again.
        sem_e, sem_d = await self._process_table_delete(
            "semantic_entries", dry_run=dry_run,
        )
        sem_arch_e, sem_a = await self._process_table_demote(
            "semantic_entries", dry_run=dry_run,
        )
        result.semantic_examined = sem_e + sem_arch_e
        result.semantic_deleted = sem_d
        result.semantic_archived = sem_a

        rel_e, rel_d = await self._process_table_delete(
            "relational_entries", dry_run=dry_run, key_col="id",
        )
        rel_arch_e, rel_a = await self._process_table_demote(
            "relational_entries", dry_run=dry_run, key_col="id",
        )
        result.relational_examined = rel_e + rel_arch_e
        result.relational_deleted = rel_d
        result.relational_archived = rel_a

        return result

    # -- private --------------------------------------------------------------

    async def _process_table_delete(
        self,
        table: str,
        dry_run: bool,
        key_col: str = "key",
    ) -> tuple[int, int]:
        """Delete archived rows that have decayed past threshold.

        Returns (examined, deleted)."""
        cursor = await self._db.execute(
            f"SELECT {key_col}, confidence, updated_at, last_accessed_at, "
            f"domain, temporal FROM {table} WHERE temporal = ?",
            (TEMPORAL_ARCHIVED,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return 0, 0

        to_delete: list = []
        for row in rows:
            try:
                eff = effective_confidence(
                    confidence=row[1],
                    updated_at=datetime.fromisoformat(row[2]),
                    last_accessed_at=(
                        datetime.fromisoformat(row[3]) if row[3] else None
                    ),
                    domain=row[4],
                    temporal=row[5],
                )
            except Exception as exc:
                logger.debug("lifecycle: skip %s row %r (%s)", table, row[0], exc)
                continue
            if eff < self._threshold:
                to_delete.append(row[0])

        if to_delete and not dry_run:
            placeholders = ",".join("?" * len(to_delete))
            await self._db.execute(
                f"DELETE FROM {table} WHERE {key_col} IN ({placeholders})",
                to_delete,
            )
            await self._db.commit()

        return len(rows), len(to_delete)

    async def _process_table_demote(
        self,
        table: str,
        dry_run: bool,
        key_col: str = "key",
    ) -> tuple[int, int]:
        """Demote recent rows whose effective_confidence has fallen below
        threshold to ``temporal='archived'``. Milestone rows are skipped
        because their half-life is infinity (or 365d for knowledge —
        which delays the demote rather than skipping it).

        Returns (examined, demoted)."""
        cursor = await self._db.execute(
            f"SELECT {key_col}, confidence, updated_at, last_accessed_at, "
            f"domain, temporal FROM {table} "
            f"WHERE temporal IN (?, ?)",
            (TEMPORAL_RECENT, TEMPORAL_MILESTONE),
        )
        rows = await cursor.fetchall()
        if not rows:
            return 0, 0

        to_demote: list = []
        for row in rows:
            try:
                eff = effective_confidence(
                    confidence=row[1],
                    updated_at=datetime.fromisoformat(row[2]),
                    last_accessed_at=(
                        datetime.fromisoformat(row[3]) if row[3] else None
                    ),
                    domain=row[4],
                    temporal=row[5],
                )
            except Exception as exc:
                logger.debug("lifecycle: skip %s row %r (%s)", table, row[0], exc)
                continue
            # Milestone rows demote only if knowledge — others have inf half-life
            # so eff stays at original confidence (well above threshold).
            if eff < self._threshold and row[5] != TEMPORAL_MILESTONE:
                to_demote.append(row[0])

        if to_demote and not dry_run:
            placeholders = ",".join("?" * len(to_demote))
            await self._db.execute(
                f"UPDATE {table} SET temporal = ? "
                f"WHERE {key_col} IN ({placeholders})",
                (TEMPORAL_ARCHIVED, *to_demote),
            )
            await self._db.commit()

        return len(rows), len(to_demote)
