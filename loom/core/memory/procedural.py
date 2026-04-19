"""
Procedural Memory — the Skill Genome system.

Each Skill is a versioned, self-assessing entity that tracks its own
effectiveness and can be deprecated when confidence falls too low.

Issue #120 PR 2 adds ``SkillCandidate`` — proposed revisions generated
by ``SkillMutator`` from diagnostic feedback. Candidates are persisted
in the ``skill_candidates`` table and live alongside the parent
``SkillGenome`` without replacing it; promotion / rollback is handled
by PR 3.

Phase 1: data structures and persistence only.
Phase 2: skill evaluation and auto-deprecation will be wired in.
"""

import json
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import datetime, UTC

import aiosqlite


# ---------------------------------------------------------------------------
# Skill candidate status (Issue #120 PR 2)
# ---------------------------------------------------------------------------

CANDIDATE_STATUSES: tuple[str, ...] = (
    "generated",      # produced by SkillMutator, awaiting review
    "shadow",         # running in shadow mode alongside the parent (PR 3)
    "promoted",       # accepted — replaced the parent SKILL.md (PR 3)
    "deprecated",     # rejected; never promoted
    "rolled_back",    # previously promoted, now rolled back (PR 3)
)


@dataclass
class SkillGenome:
    """
    A skill with evolutionary metadata.

    The confidence score drifts based on observed success/failure.
    When it falls below `deprecation_threshold`, the skill is retired.
    """
    name: str
    body: str                          # The skill content in natural language
    version: int = 1
    confidence: float = 1.0
    usage_count: int = 0
    success_rate: float = 1.0
    parent_skill: str | None = None    # Inheritance lineage
    deprecation_threshold: float = 0.3
    tags: list[str] = field(default_factory=list)
    precondition_check_refs: list[dict] = field(default_factory=list)
    """
    Skill-declared precondition check references (Issue #64 Phase B).

    Each dict: {"ref": "checks.fn_name", "applies_to": ["run_bash"], "description": "..."}
    Parsed from SKILL.md frontmatter, resolved to callables at load_skill() time.
    """
    maturity_tag: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Minimum observations before a skill can be deprecated.  A brand-new skill
    # with confidence=0.5 would be killed by its very first failure without this
    # guard (0.5 * 0.9 = 0.45 < deprecation_threshold=0.3 after just one miss).
    MIN_SAMPLES_BEFORE_DEPRECATION: int = 3

    @property
    def is_deprecated(self) -> bool:
        if self.usage_count < self.MIN_SAMPLES_BEFORE_DEPRECATION:
            return False
        return self.confidence <= self.deprecation_threshold

    def record_outcome(self, success: bool) -> None:
        """Update confidence and success_rate after an observed outcome.

        .. deprecated:: Issue #56
            Binary success/failure tracking is replaced by quality-gradient
            self-assessment in ``SkillOutcomeTracker``.  This method is kept
            for backward compatibility but is no longer called by the core
            session loop.
        """
        warnings.warn(
            "SkillGenome.record_outcome() is deprecated; "
            "use SkillOutcomeTracker quality-gradient assessment instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.usage_count += 1
        outcome = 1.0 if success else 0.0
        if self.usage_count == 1:
            # First observation: set baseline directly instead of blending with
            # the default 1.0 prior, which would misleadingly inflate confidence.
            self.success_rate = outcome
            self.confidence = outcome
        else:
            # Exponential moving average — recent outcomes weighted more
            alpha = 0.1
            self.success_rate = (1 - alpha) * self.success_rate + alpha * outcome
            self.confidence = self.success_rate
        self.updated_at = datetime.now(UTC)


# ---------------------------------------------------------------------------
# Skill version history reasons (Issue #120 PR 3)
# ---------------------------------------------------------------------------

HISTORY_REASONS: tuple[str, ...] = (
    "promote",   # archived because a candidate was promoted over it
    "rollback",  # archived as part of a rollback operation
    "manual",    # explicit archive call
)


@dataclass
class SkillVersionRecord:
    """Snapshot of a SKILL.md body captured before a lifecycle transition.

    Written whenever ``SkillPromoter`` replaces the active body — either by
    promoting a candidate or by rolling back to an earlier version.  The
    archive survives the swap so rollback can restore any prior revision.
    """

    skill_name: str
    version: int
    body: str
    reason: str = "promote"
    source_candidate_id: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    archived_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.reason not in HISTORY_REASONS:
            raise ValueError(
                f"Invalid history reason {self.reason!r}; "
                f"expected one of {HISTORY_REASONS}"
            )


@dataclass
class SkillCandidate:
    """A proposed revision of a parent ``SkillGenome`` (Issue #120 PR 2).

    Candidates are generated by ``SkillMutator`` from one or more
    ``TaskDiagnostic`` entries — specifically their
    ``mutation_suggestions`` — and stored in ``skill_candidates`` until
    lifecycle management (PR 3) promotes, deprecates, or rolls them back.

    ``diagnostic_keys`` records the SemanticMemory keys of the
    diagnostics that motivated this candidate so the audit trail from
    *observation → proposed fix* stays intact.
    """

    parent_skill_name: str
    parent_version: int
    candidate_body: str
    mutation_strategy: str
    diagnostic_keys: list[str] = field(default_factory=list)
    origin_session_id: str | None = None
    status: str = "generated"
    pareto_scores: dict[str, float] = field(default_factory=dict)
    notes: str | None = None
    fast_track: bool = False
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.status not in CANDIDATE_STATUSES:
            raise ValueError(
                f"Invalid candidate status {self.status!r}; "
                f"expected one of {CANDIDATE_STATUSES}"
            )


class ProceduralMemory:
    """Read/write access to the skill_genomes table."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def upsert(self, skill: SkillGenome) -> None:
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """
            INSERT INTO skill_genomes
                (id, name, version, confidence, usage_count, success_rate,
                 parent_skill, deprecation_threshold, tags, body,
                 precondition_check_refs, maturity_tag, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                version                 = excluded.version,
                confidence              = excluded.confidence,
                usage_count             = excluded.usage_count,
                success_rate            = excluded.success_rate,
                deprecation_threshold   = excluded.deprecation_threshold,
                tags                    = excluded.tags,
                body                    = excluded.body,
                precondition_check_refs = excluded.precondition_check_refs,
                maturity_tag            = excluded.maturity_tag,
                updated_at              = excluded.updated_at
            """,
            (
                skill.id, skill.name, skill.version, skill.confidence,
                skill.usage_count, skill.success_rate, skill.parent_skill,
                skill.deprecation_threshold,
                json.dumps(skill.tags, ensure_ascii=False),
                skill.body,
                json.dumps(skill.precondition_check_refs, ensure_ascii=False),
                skill.maturity_tag,
                skill.created_at.isoformat(), now,
            ),
        )
        await self._db.commit()

    async def get(self, name: str) -> SkillGenome | None:
        cursor = await self._db.execute(
            "SELECT id, name, version, confidence, usage_count, success_rate, "
            "parent_skill, deprecation_threshold, tags, body, "
            "precondition_check_refs, maturity_tag, created_at, updated_at "
            "FROM skill_genomes WHERE name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return SkillGenome(
            id=row[0], name=row[1], version=row[2], confidence=row[3],
            usage_count=row[4], success_rate=row[5], parent_skill=row[6],
            deprecation_threshold=row[7], tags=json.loads(row[8]),
            body=row[9], precondition_check_refs=json.loads(row[10]),
            maturity_tag=row[11],
            created_at=datetime.fromisoformat(row[12]),
            updated_at=datetime.fromisoformat(row[13]),
        )

    async def list_active(self) -> list[SkillGenome]:
        """Return all non-deprecated skills ordered by confidence."""
        cursor = await self._db.execute(
            "SELECT id, name, version, confidence, usage_count, success_rate, "
            "parent_skill, deprecation_threshold, tags, body, "
            "precondition_check_refs, maturity_tag, created_at, updated_at "
            "FROM skill_genomes "
            "WHERE confidence > deprecation_threshold "
            "   OR usage_count < ? "
            "ORDER BY confidence DESC",
            (SkillGenome.MIN_SAMPLES_BEFORE_DEPRECATION,),
        )
        rows = await cursor.fetchall()
        return [
            SkillGenome(
                id=r[0], name=r[1], version=r[2], confidence=r[3],
                usage_count=r[4], success_rate=r[5], parent_skill=r[6],
                deprecation_threshold=r[7], tags=json.loads(r[8]),
                body=r[9], precondition_check_refs=json.loads(r[10]),
                maturity_tag=r[11],
                created_at=datetime.fromisoformat(r[12]),
                updated_at=datetime.fromisoformat(r[13]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Skill candidate CRUD (Issue #120 PR 2)
    # ------------------------------------------------------------------

    async def insert_candidate(self, candidate: SkillCandidate) -> None:
        """Persist a newly-generated candidate (status default ``generated``)."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """
            INSERT INTO skill_candidates
                (id, parent_skill_name, parent_version, candidate_body,
                 mutation_strategy, diagnostic_keys, origin_session_id,
                 status, pareto_scores, notes, fast_track, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.id,
                candidate.parent_skill_name,
                candidate.parent_version,
                candidate.candidate_body,
                candidate.mutation_strategy,
                json.dumps(candidate.diagnostic_keys, ensure_ascii=False),
                candidate.origin_session_id,
                candidate.status,
                json.dumps(candidate.pareto_scores, ensure_ascii=False),
                candidate.notes,
                1 if candidate.fast_track else 0,
                candidate.created_at.isoformat(),
                now,
            ),
        )
        await self._db.commit()

    async def get_candidate(self, candidate_id: str) -> SkillCandidate | None:
        cursor = await self._db.execute(
            "SELECT id, parent_skill_name, parent_version, candidate_body, "
            "mutation_strategy, diagnostic_keys, origin_session_id, status, "
            "pareto_scores, notes, fast_track, created_at, updated_at "
            "FROM skill_candidates WHERE id = ?",
            (candidate_id,),
        )
        row = await cursor.fetchone()
        return _row_to_candidate(row) if row else None

    async def list_candidates(
        self,
        parent_skill_name: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[SkillCandidate]:
        """List candidates newest-first, optionally filtered by parent / status."""
        where: list[str] = []
        args: list[object] = []
        if parent_skill_name is not None:
            where.append("parent_skill_name = ?")
            args.append(parent_skill_name)
        if status is not None:
            where.append("status = ?")
            args.append(status)

        sql = (
            "SELECT id, parent_skill_name, parent_version, candidate_body, "
            "mutation_strategy, diagnostic_keys, origin_session_id, status, "
            "pareto_scores, notes, fast_track, created_at, updated_at "
            "FROM skill_candidates"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)

        cursor = await self._db.execute(sql, tuple(args))
        rows = await cursor.fetchall()
        return [_row_to_candidate(r) for r in rows]

    async def update_candidate_status(
        self,
        candidate_id: str,
        status: str,
        notes: str | None = None,
    ) -> bool:
        """Update a candidate's lifecycle status; returns True if a row changed."""
        if status not in CANDIDATE_STATUSES:
            raise ValueError(
                f"Invalid candidate status {status!r}; "
                f"expected one of {CANDIDATE_STATUSES}"
            )
        now = datetime.now(UTC).isoformat()
        if notes is None:
            cursor = await self._db.execute(
                "UPDATE skill_candidates SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, candidate_id),
            )
        else:
            cursor = await self._db.execute(
                "UPDATE skill_candidates "
                "SET status = ?, notes = ?, updated_at = ? WHERE id = ?",
                (status, notes, now, candidate_id),
            )
        await self._db.commit()
        return cursor.rowcount > 0

    async def update_maturity_tag(
        self,
        skill_name: str,
        tag: str | None,
    ) -> bool:
        """Set or clear the maturity_tag on a SkillGenome. Returns True if updated."""
        now = datetime.now(UTC).isoformat()
        cursor = await self._db.execute(
            "UPDATE skill_genomes SET maturity_tag = ?, updated_at = ? WHERE name = ?",
            (tag, now, skill_name),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Skill version history (Issue #120 PR 3)
    # ------------------------------------------------------------------

    async def archive_version(self, record: SkillVersionRecord) -> None:
        """Persist a pre-swap snapshot of a SKILL.md body."""
        await self._db.execute(
            """
            INSERT INTO skill_version_history
                (id, skill_name, version, body, reason,
                 source_candidate_id, archived_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.skill_name,
                record.version,
                record.body,
                record.reason,
                record.source_candidate_id,
                record.archived_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def list_history(
        self,
        skill_name: str,
        limit: int = 50,
    ) -> list[SkillVersionRecord]:
        """Return archived versions of a skill, newest first."""
        cursor = await self._db.execute(
            "SELECT id, skill_name, version, body, reason, "
            "source_candidate_id, archived_at "
            "FROM skill_version_history "
            "WHERE skill_name = ? "
            "ORDER BY archived_at DESC LIMIT ?",
            (skill_name, limit),
        )
        rows = await cursor.fetchall()
        return [_row_to_history(r) for r in rows]

    async def get_history_version(
        self,
        skill_name: str,
        version: int,
    ) -> SkillVersionRecord | None:
        """Fetch the most recent archive of a specific version."""
        cursor = await self._db.execute(
            "SELECT id, skill_name, version, body, reason, "
            "source_candidate_id, archived_at "
            "FROM skill_version_history "
            "WHERE skill_name = ? AND version = ? "
            "ORDER BY archived_at DESC LIMIT 1",
            (skill_name, version),
        )
        row = await cursor.fetchone()
        return _row_to_history(row) if row else None

    async def latest_history(
        self,
        skill_name: str,
    ) -> SkillVersionRecord | None:
        """Most recently archived version — used as the default rollback target."""
        cursor = await self._db.execute(
            "SELECT id, skill_name, version, body, reason, "
            "source_candidate_id, archived_at "
            "FROM skill_version_history "
            "WHERE skill_name = ? "
            "ORDER BY archived_at DESC LIMIT 1",
            (skill_name,),
        )
        row = await cursor.fetchone()
        return _row_to_history(row) if row else None


def _row_to_history(row: tuple) -> SkillVersionRecord:
    return SkillVersionRecord(
        id=row[0],
        skill_name=row[1],
        version=row[2],
        body=row[3],
        reason=row[4],
        source_candidate_id=row[5],
        archived_at=datetime.fromisoformat(row[6]),
    )


def _row_to_candidate(row: tuple) -> SkillCandidate:
    return SkillCandidate(
        id=row[0],
        parent_skill_name=row[1],
        parent_version=row[2],
        candidate_body=row[3],
        mutation_strategy=row[4],
        diagnostic_keys=json.loads(row[5]),
        origin_session_id=row[6],
        status=row[7],
        pareto_scores=json.loads(row[8]),
        notes=row[9],
        fast_track=bool(row[10]),
        created_at=datetime.fromisoformat(row[11]),
        updated_at=datetime.fromisoformat(row[12]),
    )
