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

_HALF_LIFE_TABLE: dict[tuple[str, str], float] = {
    # recent — actively used facts
    (DOMAIN_SELF,      TEMPORAL_RECENT):    180.0,
    (DOMAIN_USER,      TEMPORAL_RECENT):     90.0,
    (DOMAIN_PROJECT,   TEMPORAL_RECENT):     90.0,
    (DOMAIN_KNOWLEDGE, TEMPORAL_RECENT):     90.0,  # was 60 — N1 concession
    # milestone — permanent anchors. self/user/project never expire (inf
    # half-life ⇒ effective_confidence stays at original value forever).
    # knowledge/milestone has 365d so an outdated external fact eventually
    # transitions through archived → deleted via the normal threshold path.
    (DOMAIN_SELF,      TEMPORAL_MILESTONE): math.inf,
    (DOMAIN_USER,      TEMPORAL_MILESTONE): math.inf,
    (DOMAIN_PROJECT,   TEMPORAL_MILESTONE): math.inf,
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


def _decay_factor(half_life: float, days: float) -> float:
    """Standalone helper for tests/auditing — exposes the math separately
    from the time-anchor logic in :func:`effective_confidence`."""
    if half_life == math.inf:
        return 1.0
    return math.pow(2.0, -days / half_life)


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
    if half == math.inf:
        return confidence
    anchor = updated_at
    if last_accessed_at is not None and last_accessed_at > anchor:
        anchor = last_accessed_at
    days = (datetime.now(UTC) - anchor).total_seconds() / 86400.0
    decayed = confidence * _decay_factor(half, days)
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
    skipped: bool = False  # throttle short-circuit (last run within min_gap_minutes)

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

    async def run_for_table(
        self,
        table: str,
        dry_run: bool = False,
        key_col: str = "key",
    ) -> tuple[int, int, int]:
        """Run delete + demote on a single table. Public entry point used
        by :meth:`SemanticMemory.prune_decayed` so the legacy ``memory_prune``
        CLI tool stays narrowly-scoped to one table without reaching into
        ``MemoryLifecycle``'s private helpers.

        Returns ``(examined, archived, deleted)``.
        """
        examined_d, deleted = await self._process_table_delete(
            table, dry_run=dry_run, key_col=key_col,
        )
        examined_a, archived = await self._process_table_demote(
            table, dry_run=dry_run, key_col=key_col,
        )
        return examined_d + examined_a, archived, deleted

    async def run(
        self,
        dry_run: bool = False,
        min_gap_minutes: float = 0.0,
    ) -> LifecycleResult:
        """Run one full lifecycle cycle. Returns counts; never raises.

        ``min_gap_minutes`` enables cross-caller throttling — if the previous
        run completed within that window, returns immediately with
        ``skipped=True``. Persisted in ``memory_meta`` so daemon-cron and
        ``session.stop()`` paths both honour the same throttle. Set 0 to
        always run (default; preserves test/legacy behaviour).
        ``dry_run`` short-circuits the throttle so previews never touch state.
        """
        result = LifecycleResult(dry_run=dry_run)

        if min_gap_minutes > 0 and not dry_run:
            last_run = await self._read_last_run_at()
            if last_run is not None:
                gap_min = (datetime.now(UTC) - last_run).total_seconds() / 60.0
                if gap_min < min_gap_minutes:
                    logger.debug(
                        "lifecycle: skip — last run %.1fmin ago (< %.1fmin gap)",
                        gap_min, min_gap_minutes,
                    )
                    result.skipped = True
                    return result

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

        if not dry_run:
            await self._write_last_run_at()

        return result

    # -- meta kv (throttle bookkeeping) --------------------------------------

    _META_KEY_LAST_RUN = "lifecycle.last_run_at"

    async def _read_last_run_at(self) -> datetime | None:
        cursor = await self._db.execute(
            "SELECT value FROM memory_meta WHERE key = ?",
            (self._META_KEY_LAST_RUN,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        try:
            return datetime.fromisoformat(row[0])
        except ValueError:
            return None

    async def _write_last_run_at(self) -> None:
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "INSERT INTO memory_meta(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (self._META_KEY_LAST_RUN, now, now),
        )
        await self._db.commit()

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
        """Demote rows whose effective_confidence has fallen below threshold
        to ``temporal='archived'``. Both ``recent`` and ``milestone`` rows
        are examined — but ``effective_confidence`` returns the original
        confidence unchanged for self/user/project milestone (inf half-life),
        so they never cross the threshold and never get demoted. Only
        ``knowledge/milestone`` (365d half-life) can eventually demote,
        which is the design intent — outdated external knowledge expires
        through the normal pipeline instead of accumulating forever.

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
            if eff < self._threshold:
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
