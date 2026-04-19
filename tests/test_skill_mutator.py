"""
Tests for Issue #120 PR 2 — SkillMutator + SkillCandidate store.

Covers:
- ``SkillMutator.should_propose`` gate logic (enabled, quality ceiling,
  min suggestions threshold).
- ``propose_candidate`` happy path, empty-parent guard, bad-LLM-output
  rejection, code-fence stripping.
- ``ProceduralMemory`` candidate CRUD: insert → get → list → update_status
  roundtrip, status filter, status validation error.
- ``TaskReflector`` mutation post-hook: candidate persisted + subscriber
  fired when the mutator is wired in; skipped when the gate fails.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock

import pytest

from loom.core.cognition.skill_mutator import (
    MUTATION_STRATEGIES,
    MutationProposal,
    SkillMutator,
    _bullet,
    _looks_like_skill_md,
    _strip_fencing,
)
from loom.core.cognition.task_reflector import TaskDiagnostic, TaskReflector
from loom.core.memory.procedural import (
    CANDIDATE_STATUSES,
    ProceduralMemory,
    SkillCandidate,
    SkillGenome,
)
from loom.core.memory.store import SQLiteStore


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


SAMPLE_SKILL_BODY = """\
---
name: review-skill
description: Review code changes carefully before responding.
---

# Review Skill

Always read the relevant file before editing it.
Run tests after every meaningful edit.
Keep diffs small and well-scoped.
"""


def _make_diagnostic(
    *,
    quality_score: float = 3.0,
    suggestions: list[str] | None = None,
) -> TaskDiagnostic:
    return TaskDiagnostic(
        skill_name="review-skill",
        session_id="s1",
        turn_index=2,
        task_type="code_review",
        task_type_confidence=0.8,
        instructions_followed=["Read the file first"],
        instructions_violated=["Skipped running tests"],
        failure_patterns=["No verification step"],
        success_patterns=["Small diff"],
        mutation_suggestions=(
            suggestions if suggestions is not None
            else ["Add an explicit 'run pytest' step after every edit"]
        ),
        quality_score=quality_score,
        envelope_ids=["e1"],
    )


def _make_skill() -> SkillGenome:
    return SkillGenome(
        name="review-skill",
        body=SAMPLE_SKILL_BODY,
        version=1,
        confidence=0.6,
        success_rate=0.6,
        usage_count=2,
    )


def _build_mutator(
    *,
    llm_response: str | None = None,
    enabled: bool = True,
    quality_ceiling: float = 3.5,
    min_suggestions: int = 1,
) -> tuple[SkillMutator, MagicMock]:
    router = MagicMock()
    router.chat = AsyncMock(
        return_value=MagicMock(
            text=(llm_response if llm_response is not None else SAMPLE_SKILL_BODY + "\nRun pytest after every edit.\n")
        )
    )
    mutator = SkillMutator(
        router=router,
        model="test-model",
        enabled=enabled,
        quality_ceiling=quality_ceiling,
        min_suggestions=min_suggestions,
    )
    return mutator, router


# ---------------------------------------------------------------------------
# Module constants + helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_strategies_include_apply_suggestions(self):
        assert "apply_suggestions" in MUTATION_STRATEGIES

    def test_bullet_empty(self):
        assert _bullet([], limit=5) == "(none)"

    def test_bullet_truncates_items(self):
        out = _bullet(["x" * 500], limit=5)
        # Each line starts with "- " so the content is capped at 220 chars.
        assert out.startswith("- ")
        assert len(out) <= 2 + 220

    def test_bullet_respects_limit(self):
        items = [f"s{i}" for i in range(10)]
        out = _bullet(items, limit=3)
        assert out.count("\n") == 2  # exactly 3 lines

    def test_strip_fencing_removes_code_block(self):
        raw = "```markdown\n# hello\nworld\n```"
        assert _strip_fencing(raw) == "# hello\nworld"

    def test_strip_fencing_leaves_plain_text(self):
        raw = "# hello\nworld"
        assert _strip_fencing(raw) == "# hello\nworld"

    def test_looks_like_skill_md_requires_shared_line(self):
        parent = SAMPLE_SKILL_BODY
        good = parent + "\nAlso run pytest.\n"
        assert _looks_like_skill_md(good, parent) is True

        # Completely unrelated body — fails the shared-line anchor.
        bad = "---\nname: other\n---\n\n# Totally Different\nNothing in common here.\nAlso nothing.\n"
        assert _looks_like_skill_md(bad, parent) is False

    def test_looks_like_skill_md_rejects_short_body(self):
        assert _looks_like_skill_md("too short", SAMPLE_SKILL_BODY) is False

    def test_looks_like_skill_md_accepts_when_parent_empty(self):
        body = "a" * 100
        assert _looks_like_skill_md(body, "") is True


# ---------------------------------------------------------------------------
# SkillMutator.should_propose gate
# ---------------------------------------------------------------------------


class TestShouldPropose:
    def test_disabled_returns_false(self):
        mutator, _ = _build_mutator(enabled=False)
        assert mutator.should_propose(_make_diagnostic()) is False

    def test_quality_above_ceiling_returns_false(self):
        mutator, _ = _build_mutator(quality_ceiling=3.0)
        assert mutator.should_propose(_make_diagnostic(quality_score=4.5)) is False

    def test_too_few_suggestions_returns_false(self):
        mutator, _ = _build_mutator(min_suggestions=2)
        assert mutator.should_propose(_make_diagnostic(suggestions=["only one"])) is False

    def test_happy_path_returns_true(self):
        mutator, _ = _build_mutator()
        assert mutator.should_propose(_make_diagnostic()) is True


# ---------------------------------------------------------------------------
# SkillMutator.propose_candidate
# ---------------------------------------------------------------------------


class TestProposeCandidate:
    @pytest.mark.asyncio
    async def test_happy_path_builds_candidate(self):
        mutator, router = _build_mutator()
        skill = _make_skill()

        proposal = await mutator.propose_candidate(
            parent=skill,
            diagnostic=_make_diagnostic(),
            session_id="session-xyz",
        )

        assert proposal is not None
        assert isinstance(proposal, MutationProposal)
        assert proposal.candidate.parent_skill_name == skill.name
        assert proposal.candidate.parent_version == skill.version
        assert proposal.candidate.mutation_strategy == "apply_suggestions"
        assert proposal.candidate.origin_session_id == "session-xyz"
        # Pareto score seeded from the diagnostic's quality_score.
        assert proposal.candidate.pareto_scores == {"code_review": 3.0}
        # Diagnostic key points back to the source under the reflector's convention.
        assert proposal.candidate.diagnostic_keys
        assert proposal.candidate.diagnostic_keys[0].startswith(
            "skill:review-skill:diagnostic:"
        )
        # LLM was asked once.
        router.chat.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_none_when_gate_fails(self):
        mutator, router = _build_mutator(enabled=False)
        proposal = await mutator.propose_candidate(
            parent=_make_skill(),
            diagnostic=_make_diagnostic(),
        )
        assert proposal is None
        router.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_parent_body(self):
        mutator, router = _build_mutator()
        empty_skill = SkillGenome(name="review-skill", body="   \n\n")

        proposal = await mutator.propose_candidate(
            parent=empty_skill,
            diagnostic=_make_diagnostic(),
        )
        assert proposal is None
        router.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_when_llm_raises(self):
        mutator, router = _build_mutator()
        router.chat = AsyncMock(side_effect=RuntimeError("boom"))

        proposal = await mutator.propose_candidate(
            parent=_make_skill(),
            diagnostic=_make_diagnostic(),
        )
        assert proposal is None

    @pytest.mark.asyncio
    async def test_returns_none_when_body_fails_plausibility(self):
        # LLM returns a short, unrelated body — should be rejected.
        mutator, _ = _build_mutator(llm_response="nope")
        proposal = await mutator.propose_candidate(
            parent=_make_skill(),
            diagnostic=_make_diagnostic(),
        )
        assert proposal is None

    @pytest.mark.asyncio
    async def test_strips_code_fencing_from_response(self):
        wrapped = f"```markdown\n{SAMPLE_SKILL_BODY}\nRun pytest.\n```"
        mutator, _ = _build_mutator(llm_response=wrapped)

        proposal = await mutator.propose_candidate(
            parent=_make_skill(),
            diagnostic=_make_diagnostic(),
        )
        assert proposal is not None
        assert "```" not in proposal.candidate.candidate_body


# ---------------------------------------------------------------------------
# ProceduralMemory candidate CRUD (real SQLite)
# ---------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path):
    db_path = tmp_path / "memory.db"
    store = SQLiteStore(str(db_path))
    await store.initialize()
    yield store


class TestProceduralMemoryCandidates:
    @pytest.mark.asyncio
    async def test_insert_get_roundtrip(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            cand = SkillCandidate(
                parent_skill_name="review-skill",
                parent_version=1,
                candidate_body=SAMPLE_SKILL_BODY,
                mutation_strategy="apply_suggestions",
                diagnostic_keys=["skill:review-skill:diagnostic:2026-04-17T12:00:00+00:00"],
                origin_session_id="session-1",
                pareto_scores={"code_review": 3.0},
                notes="test",
            )
            await proc.insert_candidate(cand)

            fetched = await proc.get_candidate(cand.id)
            assert fetched is not None
            assert fetched.parent_skill_name == "review-skill"
            assert fetched.status == "generated"
            assert fetched.pareto_scores == {"code_review": 3.0}
            assert fetched.diagnostic_keys == cand.diagnostic_keys
            assert fetched.origin_session_id == "session-1"

    @pytest.mark.asyncio
    async def test_list_filters_by_parent_and_status(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)

            # Two candidates across two parents.
            await proc.insert_candidate(SkillCandidate(
                parent_skill_name="review-skill",
                parent_version=1,
                candidate_body="body-a",
                mutation_strategy="apply_suggestions",
            ))
            other = SkillCandidate(
                parent_skill_name="debug-skill",
                parent_version=2,
                candidate_body="body-b",
                mutation_strategy="apply_suggestions",
            )
            await proc.insert_candidate(other)

            # Promote one so status filter has something to distinguish.
            assert await proc.update_candidate_status(other.id, "promoted") is True

            # Filter by parent.
            review_only = await proc.list_candidates(parent_skill_name="review-skill")
            assert len(review_only) == 1
            assert review_only[0].parent_skill_name == "review-skill"

            # Filter by status.
            promoted_only = await proc.list_candidates(status="promoted")
            assert len(promoted_only) == 1
            assert promoted_only[0].id == other.id

    @pytest.mark.asyncio
    async def test_update_status_rejects_invalid(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            cand = SkillCandidate(
                parent_skill_name="x",
                parent_version=1,
                candidate_body="b",
                mutation_strategy="apply_suggestions",
            )
            await proc.insert_candidate(cand)
            with pytest.raises(ValueError):
                await proc.update_candidate_status(cand.id, "bogus-status")

    @pytest.mark.asyncio
    async def test_update_status_returns_false_for_unknown_id(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            assert await proc.update_candidate_status("no-such-id", "promoted") is False

    def test_candidate_statuses_include_expected(self):
        for expected in ("generated", "shadow", "promoted", "deprecated", "rolled_back"):
            assert expected in CANDIDATE_STATUSES

    def test_skill_candidate_rejects_invalid_status(self):
        with pytest.raises(ValueError):
            SkillCandidate(
                parent_skill_name="x",
                parent_version=1,
                candidate_body="b",
                mutation_strategy="apply_suggestions",
                status="not-a-status",
            )


# ---------------------------------------------------------------------------
# TaskReflector integration — mutation post-hook
# ---------------------------------------------------------------------------


def _build_reflector_with_mutator(
    *,
    mutator: SkillMutator | None,
    skill: SkillGenome | None = None,
    procedural: AsyncMock | None = None,
):
    if skill is None:
        skill = _make_skill()

    router = MagicMock()
    router.chat = AsyncMock(return_value=MagicMock(text=json.dumps({
        "task_type": "code_review",
        "task_type_confidence": 0.9,
        "instructions_followed": ["read"],
        "instructions_violated": ["skipped tests"],
        "failure_patterns": ["no verify"],
        "success_patterns": ["small diff"],
        "mutation_suggestions": ["add pytest step"],
        "quality_score": 3.0,
    })))

    if procedural is None:
        procedural = AsyncMock()
    procedural.get = AsyncMock(return_value=skill)
    procedural.upsert = AsyncMock()
    procedural.insert_candidate = AsyncMock()

    semantic = AsyncMock()
    semantic.upsert = AsyncMock()

    reflector = TaskReflector(
        router=router,
        model="test-model",
        memory=MagicMock(
            procedural=procedural,
            semantic=semantic,
            relational=None,
            episodic=None,
        ),
        session_id="session-xyz",
        enabled=True,
        visibility="summary",
        mutator=mutator,
    )
    return reflector, {
        "router": router,
        "procedural": procedural,
        "semantic": semantic,
        "skill": skill,
    }


class TestTaskReflectorMutationHook:
    @pytest.mark.asyncio
    async def test_candidate_persisted_when_mutator_wired(self):
        mutator, _ = _build_mutator()
        reflector, mocks = _build_reflector_with_mutator(mutator=mutator)

        received: list[MutationProposal] = []

        async def _cb(proposal: MutationProposal) -> None:
            received.append(proposal)

        reflector.subscribe_mutation(_cb)

        from loom.core.events import ExecutionEnvelopeView, ExecutionNodeView

        env = ExecutionEnvelopeView(
            envelope_id="e1",
            session_id="s1",
            turn_index=2,
            status="completed",
            node_count=1,
            parallel_groups=1,
            elapsed_ms=10.0,
            levels=[["n1"]],
            nodes=[ExecutionNodeView(
                node_id="n1", call_id="c1", action_id="n1",
                tool_name="run_bash", level=0, state="completed",
                trust_level="SAFE", args_preview="ls", duration_ms=5.0,
            )],
        )

        diag = await reflector._reflect_one(
            skill_name="review-skill",
            turn_index=2,
            turn_summary="reviewed the diff",
            envelopes=[env],
            tool_count=1,
        )
        assert diag is not None

        # The post-hook is fire-and-forget — drain pending tasks.
        pending = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task()
            and t.get_name().startswith("mutation_proposal:")
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        mocks["procedural"].insert_candidate.assert_awaited_once()
        stored = mocks["procedural"].insert_candidate.await_args.args[0]
        assert stored.parent_skill_name == "review-skill"
        assert stored.mutation_strategy == "apply_suggestions"

        # Mutation subscriber also saw the proposal.
        assert len(received) == 1
        assert received[0].candidate.id == stored.id

    @pytest.mark.asyncio
    async def test_no_candidate_when_gate_fails(self):
        # quality_ceiling below the diagnostic score → gate rejects.
        mutator, _ = _build_mutator(quality_ceiling=1.0)
        reflector, mocks = _build_reflector_with_mutator(mutator=mutator)

        from loom.core.events import ExecutionEnvelopeView

        diag = await reflector._reflect_one(
            skill_name="review-skill",
            turn_index=2,
            turn_summary="x",
            envelopes=[],
            tool_count=0,
        )
        assert diag is not None

        # Let any stray tasks run before asserting.
        await asyncio.sleep(0)

        mocks["procedural"].insert_candidate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_mutator_means_no_hook(self):
        reflector, mocks = _build_reflector_with_mutator(mutator=None)

        diag = await reflector._reflect_one(
            skill_name="review-skill",
            turn_index=2,
            turn_summary="x",
            envelopes=[],
            tool_count=0,
        )
        assert diag is not None
        mocks["procedural"].insert_candidate.assert_not_awaited()
