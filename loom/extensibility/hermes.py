"""
HermesLens — converts NousResearch hermes-agent skill format to Loom SkillGenome dicts.

Expected source format (dict or JSON string)
--------------------------------------------
{
    "skills": [
        {
            "name":        "refactor_extract_function",
            "description": "Extract a sub-function when a method exceeds 30 lines.",
            "body":        "Optional override; falls back to description if absent.",
            "examples":    ["Before: long_fn()  After: extracted_fn() + caller()"],
            "tags":        ["refactor", "python"],
            "confidence":  0.85
        }
    ]
}

The extracted skill dicts are ready for SkillImportPipeline.process().
"""

from __future__ import annotations

from loom.extensibility.lens import BaseLens, LensResult


class HermesLens(BaseLens):
    """
    Lens for hermes-agent (NousResearch) procedural memory format.

    Extracts skills and returns them as Loom SkillGenome-compatible dicts.
    """

    name = "hermes"
    version = "1.0"

    def supports(self, source: str | dict) -> bool:
        """Return True if the source contains a top-level 'skills' key."""
        parsed = self._parse(source)
        if isinstance(parsed, dict):
            return "skills" in parsed
        # Fallback: quick heuristic on raw string
        if isinstance(source, str):
            return '"skills"' in source or "'skills'" in source
        return False

    def extract(self, source: str | dict) -> LensResult:
        """Extract skill dicts from a hermes-agent source."""
        parsed = self._parse(source)

        if not isinstance(parsed, dict):
            return LensResult(
                source="hermes",
                warnings=["Could not parse source as a dict"],
            )

        raw_skills = parsed.get("skills", [])
        if not isinstance(raw_skills, list):
            return LensResult(
                source="hermes",
                warnings=["'skills' field is not a list"],
            )

        skills: list[dict] = []
        warnings: list[str] = []

        for i, raw in enumerate(raw_skills):
            if not isinstance(raw, dict):
                warnings.append(f"Skill[{i}] is not a dict — skipped")
                continue

            name = (raw.get("name") or "").strip()
            if not name:
                warnings.append(f"Skill[{i}] has no 'name' — skipped")
                continue

            # Accept 'body' first, fall back to 'description'
            body = (raw.get("body") or raw.get("description") or "").strip()
            if not body:
                warnings.append(f"Skill '{name}' has no body/description — skipped")
                continue

            confidence = float(raw.get("confidence", 0.8))
            confidence = max(0.0, min(1.0, confidence))

            skills.append({
                "name":       name,
                "body":       body,
                "tags":       list(raw.get("tags", [])),
                "confidence": confidence,
            })

        return LensResult(source="hermes", skills=skills, warnings=warnings)
