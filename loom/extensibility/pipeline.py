"""
SkillImportPipeline — validate and gate foreign skills before persisting.

Pipeline steps (in order)
--------------------------
1. Schema validation  — required fields ('name', 'body') must be non-empty.
2. Confidence gate    — skill confidence must meet the minimum threshold.
3. Deduplication      — skip if a skill with the same name already exists.

Usage
-----
    pipeline = SkillImportPipeline(procedural_memory, min_confidence=0.5)

    # Phase 1: evaluate
    decisions = await pipeline.process(lens_result.skills)

    # Phase 2: persist approved
    count = await pipeline.import_approved(decisions, lens_result.skills)
"""

from __future__ import annotations

from dataclasses import dataclass

from loom.core.memory.procedural import ProceduralMemory, SkillGenome


# ---------------------------------------------------------------------------
# ImportDecision
# ---------------------------------------------------------------------------

@dataclass
class ImportDecision:
    """Result of evaluating one skill through the import pipeline."""
    skill_name: str
    approved: bool
    reason: str
    adjusted_confidence: float = 0.0


# ---------------------------------------------------------------------------
# SkillImportPipeline
# ---------------------------------------------------------------------------

class SkillImportPipeline:
    """
    Validates and gates foreign skills before importing into ProceduralMemory.

    Parameters
    ----------
    procedural:     The procedural memory store to check for duplicates and persist to.
    min_confidence: Minimum confidence score required for approval (default 0.5).
    """

    REQUIRED_FIELDS: tuple[str, ...] = ("name", "body")

    def __init__(
        self,
        procedural: ProceduralMemory,
        min_confidence: float = 0.5,
    ) -> None:
        self._procedural = procedural
        self.min_confidence = max(0.0, min(1.0, min_confidence))

    async def process(self, skills: list[dict]) -> list[ImportDecision]:
        """
        Evaluate each raw skill dict and return one ImportDecision per entry.
        Does not modify the database.
        """
        return [await self._evaluate(raw) for raw in skills]

    async def import_approved(
        self,
        decisions: list[ImportDecision],
        skills: list[dict],
    ) -> int:
        """
        Persist all approved skills to ProceduralMemory.

        Parameters
        ----------
        decisions: Output of ``process()``.
        skills:    The original raw skill dicts (must correspond 1-to-1 with decisions).

        Returns the number of skills actually written.
        """
        skill_map: dict[str, dict] = {
            s.get("name", ""): s for s in skills
        }
        imported = 0

        for decision in decisions:
            if not decision.approved:
                continue
            raw = skill_map.get(decision.skill_name)
            if raw is None:
                continue

            genome = SkillGenome(
                name=raw["name"],
                body=raw["body"],
                tags=list(raw.get("tags", [])),
                confidence=decision.adjusted_confidence,
            )
            await self._procedural.upsert(genome)
            imported += 1

        return imported

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _evaluate(self, raw: dict) -> ImportDecision:
        # Step 1: schema validation
        for field in self.REQUIRED_FIELDS:
            if not (raw.get(field) or "").strip():
                return ImportDecision(
                    skill_name=(raw.get("name") or "(unnamed)"),
                    approved=False,
                    reason=f"Missing required field: '{field}'",
                )

        name = raw["name"].strip()
        confidence = float(raw.get("confidence", 0.8))
        confidence = max(0.0, min(1.0, confidence))

        # Step 2: confidence gate
        if confidence < self.min_confidence:
            return ImportDecision(
                skill_name=name,
                approved=False,
                reason=(
                    f"Confidence {confidence:.2f} is below "
                    f"threshold {self.min_confidence:.2f}"
                ),
                adjusted_confidence=confidence,
            )

        # Step 3: deduplication
        existing = await self._procedural.get(name)
        if existing is not None:
            return ImportDecision(
                skill_name=name,
                approved=False,
                reason=(
                    f"Skill '{name}' already exists "
                    f"(confidence: {existing.confidence:.2f})"
                ),
                adjusted_confidence=confidence,
            )

        return ImportDecision(
            skill_name=name,
            approved=True,
            reason="Passed all checks",
            adjusted_confidence=confidence,
        )
