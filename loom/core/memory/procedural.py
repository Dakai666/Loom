"""
Procedural Memory — the Skill Genome system.

Each Skill is a versioned, self-assessing entity that tracks its own
effectiveness and can be deprecated when confidence falls too low.

Phase 1: data structures and persistence only.
Phase 2: skill evaluation and auto-deprecation will be wired in.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC

import aiosqlite


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
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_deprecated(self) -> bool:
        return self.confidence <= self.deprecation_threshold

    def record_outcome(self, success: bool) -> None:
        """Update confidence and success_rate after an observed outcome."""
        self.usage_count += 1
        # Exponential moving average — recent outcomes weighted more
        alpha = 0.1
        outcome = 1.0 if success else 0.0
        self.success_rate = (1 - alpha) * self.success_rate + alpha * outcome
        self.confidence = self.success_rate
        self.updated_at = datetime.now(UTC)


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
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                version               = excluded.version,
                confidence            = excluded.confidence,
                usage_count           = excluded.usage_count,
                success_rate          = excluded.success_rate,
                deprecation_threshold = excluded.deprecation_threshold,
                tags                  = excluded.tags,
                body                  = excluded.body,
                updated_at            = excluded.updated_at
            """,
            (
                skill.id, skill.name, skill.version, skill.confidence,
                skill.usage_count, skill.success_rate, skill.parent_skill,
                skill.deprecation_threshold,
                json.dumps(skill.tags, ensure_ascii=False),
                skill.body,
                skill.created_at.isoformat(), now,
            ),
        )
        await self._db.commit()

    async def get(self, name: str) -> SkillGenome | None:
        cursor = await self._db.execute(
            "SELECT id, name, version, confidence, usage_count, success_rate, "
            "parent_skill, deprecation_threshold, tags, body, created_at, updated_at "
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
            body=row[9], created_at=datetime.fromisoformat(row[10]),
            updated_at=datetime.fromisoformat(row[11]),
        )

    async def list_active(self) -> list[SkillGenome]:
        """Return all non-deprecated skills ordered by confidence."""
        cursor = await self._db.execute(
            "SELECT id, name, version, confidence, usage_count, success_rate, "
            "parent_skill, deprecation_threshold, tags, body, created_at, updated_at "
            "FROM skill_genomes WHERE confidence > deprecation_threshold "
            "ORDER BY confidence DESC"
        )
        rows = await cursor.fetchall()
        return [
            SkillGenome(
                id=r[0], name=r[1], version=r[2], confidence=r[3],
                usage_count=r[4], success_rate=r[5], parent_skill=r[6],
                deprecation_threshold=r[7], tags=json.loads(r[8]),
                body=r[9], created_at=datetime.fromisoformat(r[10]),
                updated_at=datetime.fromisoformat(r[11]),
            )
            for r in rows
        ]
