"""
Skill Mutator — proposes candidate SKILL.md revisions (Issue #120 PR 2).

The mutator consumes a parent ``SkillGenome`` and one or more
``TaskDiagnostic`` objects (specifically their ``mutation_suggestions``)
and asks the LLM to rewrite the SKILL.md body incorporating the
suggestions without drifting from the skill's original purpose.

Proposed candidates are returned — **not** persisted here.  The caller
(usually ``TaskReflector``'s post-hook) is responsible for calling
``ProceduralMemory.insert_candidate(...)`` with the returned object,
which keeps this module a pure producer.

PR 2 scope
----------
- Only one strategy, ``apply_suggestions``.  Future strategies
  (targeted_insertion, distill, merge_diagnostics) plug in via the
  ``strategy`` field without touching callers.
- Pareto scoring is a stub — diagnostic quality_score carried through
  so PR 3 / PR 4 can compare candidates without a schema change.
- ``propose_candidate`` returns ``None`` on any failure (missing body,
  LLM error, empty response).  Failure is non-fatal by design: a
  missed mutation just means the skill evolves slower, not that the
  conversation breaks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loom.core.memory.procedural import SkillCandidate

if TYPE_CHECKING:
    from loom.core.cognition.router import LLMRouter
    from loom.core.cognition.task_reflector import BatchDiagnostic, TaskDiagnostic
    from loom.core.memory.procedural import SkillGenome

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mutation strategies (closed label set — new strategies extend this tuple)
# ---------------------------------------------------------------------------

MUTATION_STRATEGIES: tuple[str, ...] = (
    "apply_suggestions",  # PR 2 default: incorporate diagnostic mutation_suggestions
)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_MUTATION_PROMPT = """\
You are revising the SKILL.md for the skill "{skill_name}".

Current SKILL.md:
---
{skill_body}
---

A recent diagnostic of this skill's own use flagged these concrete \
mutation suggestions (every item came from observing a real failure):
---
{suggestions}
---

Also recorded from the same diagnostic — for context, not to re-apply:
- instructions_violated:
{violated}
- failure_patterns:
{failures}

Rewrite the SKILL.md body to incorporate the mutation suggestions.

Hard rules:
1. Preserve the existing frontmatter block (the ``---`` metadata at the top) \
   exactly, character-for-character.  Do not modify ``name``, ``description`` \
   or any other frontmatter field.
2. Keep the skill's original purpose and tone.  Additive edits are preferred \
   over large rewrites — the reader should still recognise this as the same \
   skill.
3. Each mutation suggestion must be visibly reflected in the output, not \
   silently dropped.
4. Output ONLY the full revised SKILL.md content (frontmatter + body).  No \
   prose preamble, no code fences, no trailing commentary.
"""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class MutationProposal:
    """Thin wrapper so the caller can inspect what was sent to the LLM
    (useful for audit logs / PR 4 meta-skill-engineer integration)
    before persisting the returned ``SkillCandidate``.
    """
    candidate: SkillCandidate
    prompt_preview: str   # first ~200 chars of the prompt
    raw_response_chars: int


# ---------------------------------------------------------------------------
# Core mutator
# ---------------------------------------------------------------------------

class SkillMutator:
    """Produces ``SkillCandidate`` objects from diagnostic feedback.

    Usage
    -----
    Instantiate once per session, then call ``propose_candidate`` from
    the TaskReflector post-hook when the config + quality gate allow it.

    Parameters
    ----------
    router, model:
        LLM router + model id for the rewrite call.
    enabled:
        Master toggle (``loom.toml`` ``[mutation].enabled``).
    quality_ceiling:
        Only propose when ``diagnostic.quality_score <= quality_ceiling``.
        High-quality turns don't need a mutation.
    min_suggestions:
        Require at least this many ``mutation_suggestions`` before firing.
    max_body_chars:
        Truncate the parent body in the prompt to keep the request cheap.
    """

    def __init__(
        self,
        router: "LLMRouter",
        model: str,
        enabled: bool = False,
        quality_ceiling: float = 3.5,
        min_suggestions: int = 1,
        max_body_chars: int = 6000,
        fast_track_threshold: float = 0.20,
    ) -> None:
        self._router = router
        self._model = model
        self._enabled = enabled
        self._quality_ceiling = quality_ceiling
        self._min_suggestions = min_suggestions
        self._max_body_chars = max_body_chars
        self._fast_track_threshold = fast_track_threshold

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def should_propose(self, diagnostic: "TaskDiagnostic") -> bool:
        """Gate check — cheaper than running the LLM when the answer is no."""
        if not self._enabled:
            return False
        if len(diagnostic.mutation_suggestions) < self._min_suggestions:
            return False
        if diagnostic.quality_score > self._quality_ceiling:
            return False
        return True

    async def propose_candidate(
        self,
        parent: "SkillGenome",
        diagnostic: "TaskDiagnostic",
        session_id: str | None = None,
    ) -> MutationProposal | None:
        """Run the LLM rewrite and build a ``SkillCandidate``.

        Returns ``None`` when the gate fails, the LLM errors, or the
        response is too short to be a plausible SKILL.md.
        """
        if not self.should_propose(diagnostic):
            return None

        if not parent.body.strip():
            logger.debug("SkillMutator: parent %s has empty body — skipping", parent.name)
            return None

        prompt = _MUTATION_PROMPT.format(
            skill_name=parent.name,
            skill_body=parent.body[: self._max_body_chars],
            suggestions=_bullet(diagnostic.mutation_suggestions, limit=10),
            violated=_bullet(diagnostic.instructions_violated, limit=5),
            failures=_bullet(diagnostic.failure_patterns, limit=5),
        )

        raw = ""
        try:
            response = await self._router.chat(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000,
            )
            raw = (response.text or "").strip()
        except Exception as exc:
            logger.debug("SkillMutator LLM call failed: %s", exc)
            return None

        new_body = _strip_fencing(raw)
        if not _looks_like_skill_md(new_body, parent.body):
            logger.debug(
                "SkillMutator rewrite rejected: body too short or missing frontmatter "
                "(skill=%s len=%d)", parent.name, len(new_body),
            )
            return None

        diagnostic_key = _diagnostic_key(diagnostic)
        candidate = SkillCandidate(
            parent_skill_name=parent.name,
            parent_version=parent.version,
            candidate_body=new_body,
            mutation_strategy="apply_suggestions",
            diagnostic_keys=[diagnostic_key] if diagnostic_key else [],
            origin_session_id=session_id,
            pareto_scores={diagnostic.task_type: diagnostic.quality_score},
            notes=(
                f"from diagnostic quality={diagnostic.quality_score:.1f} "
                f"task_type={diagnostic.task_type}"
            ),
        )
        return MutationProposal(
            candidate=candidate,
            prompt_preview=prompt[:200],
            raw_response_chars=len(raw),
        )

    async def from_batch_diagnostic(
        self,
        parent: "SkillGenome",
        batch: "BatchDiagnostic",
        session_id: str | None = None,
    ) -> "MutationProposal | None":
        """Generate a candidate from a Grader-produced ``BatchDiagnostic``.

        Differs from ``propose_candidate`` in two ways:

        1. **No quality_ceiling gate** — batch path is explicitly triggered by
           meta-skill-engineer, not background reflection.
        2. **fast_track** — if ``batch.improvement >= 0.20`` the candidate is
           flagged so the caller can skip shadow N-wins and promote directly,
           because Grader already proved the improvement empirically.
        """
        if not self._enabled:
            logger.debug("from_batch_diagnostic: mutation disabled in config")
            return None

        suggestions = batch.aggregated_suggestions
        if not suggestions:
            logger.debug(
                "from_batch_diagnostic: no mutation_suggestions in batch (skill=%s)",
                parent.name,
            )
            return None

        if not parent.body.strip():
            logger.debug("from_batch_diagnostic: parent %s has empty body", parent.name)
            return None

        prompt = _MUTATION_PROMPT.format(
            skill_name=parent.name,
            skill_body=parent.body[: self._max_body_chars],
            suggestions=_bullet(suggestions, limit=10),
            violated=_bullet(batch.aggregated_violations, limit=5),
            failures=_bullet(batch.aggregated_failures, limit=5),
        )

        raw = ""
        try:
            response = await self._router.chat(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000,
            )
            raw = (response.text or "").strip()
        except Exception as exc:
            logger.debug("from_batch_diagnostic LLM call failed: %s", exc)
            return None

        new_body = _strip_fencing(raw)
        if not _looks_like_skill_md(new_body, parent.body):
            logger.debug(
                "from_batch_diagnostic rewrite rejected: implausible body "
                "(skill=%s len=%d)", parent.name, len(new_body),
            )
            return None

        fast_track = (
            batch.improvement is not None
            and batch.improvement >= self._fast_track_threshold
        )
        diagnostic_keys = [
            _diagnostic_key(d) for d in batch.diagnostics if _diagnostic_key(d)
        ]
        candidate = SkillCandidate(
            parent_skill_name=parent.name,
            parent_version=parent.version,
            candidate_body=new_body,
            mutation_strategy="batch_meta_skill_engineer",
            diagnostic_keys=diagnostic_keys,
            origin_session_id=session_id,
            pareto_scores={"pass_rate": batch.pass_rate},
            fast_track=fast_track,
            notes=(
                f"batch pass_rate={batch.pass_rate:.0%}"
                + (f" improvement={batch.improvement:+.0%}" if batch.improvement is not None else "")
            ),
        )
        return MutationProposal(
            candidate=candidate,
            prompt_preview=prompt[:200],
            raw_response_chars=len(raw),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bullet(items: list[str], limit: int) -> str:
    if not items:
        return "(none)"
    return "\n".join(f"- {s[:220]}" for s in items[:limit])


def _strip_fencing(text: str) -> str:
    """Remove leading/trailing ```…``` blocks if the LLM added them."""
    lines = text.strip().splitlines()
    if not lines:
        return ""
    if lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].rstrip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _looks_like_skill_md(body: str, parent_body: str) -> bool:
    """Quick plausibility check before persisting a candidate.

    Hard enough that random LLM preamble is rejected, lenient enough
    that a genuine rewrite passes. We don't enforce frontmatter equality
    byte-for-byte because trailing whitespace commonly drifts; the PR 3
    promotion step will do a stricter diff when it actually writes the
    file.
    """
    if not body:
        return False
    # Require some length and at least one shared non-trivial line with
    # the parent so we don't accept a hallucinated skill from another domain.
    if len(body) < 80:
        return False
    parent_lines = {l.strip() for l in parent_body.splitlines() if len(l.strip()) > 20}
    if not parent_lines:
        return True  # parent was essentially empty — nothing to anchor against
    new_lines = {l.strip() for l in body.splitlines() if len(l.strip()) > 20}
    return bool(parent_lines & new_lines)


def _diagnostic_key(diagnostic: "TaskDiagnostic") -> str:
    """Reconstruct the SemanticMemory key written by ``TaskReflector``."""
    ts = diagnostic.timestamp.isoformat(timespec="seconds")
    return f"skill:{diagnostic.skill_name}:diagnostic:{ts}"
