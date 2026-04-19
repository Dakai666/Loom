"""Tests for BatchDiagnostic and SkillMutator.from_batch_diagnostic (Issue #120 PR 4)."""
from __future__ import annotations

from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock

import pytest

from loom.core.cognition.task_reflector import BatchDiagnostic, TaskDiagnostic
from loom.core.memory.procedural import SkillCandidate, SkillGenome
from loom.core.memory.store import SQLiteStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_diagnostic(
    skill_name: str = "test-skill",
    quality_score: float = 2.5,
    suggestions: list[str] | None = None,
    violations: list[str] | None = None,
    failures: list[str] | None = None,
) -> TaskDiagnostic:
    return TaskDiagnostic(
        skill_name=skill_name,
        session_id="sess-1",
        turn_index=1,
        task_type="general",
        task_type_confidence=0.9,
        instructions_followed=["step A"],
        instructions_violated=[] if violations is None else violations,
        failure_patterns=["skipped step B"] if failures is None else failures,
        success_patterns=[],
        mutation_suggestions=["add step B after step A"] if suggestions is None else suggestions,
        quality_score=quality_score,
    )


_GENOME_BODY = (
    "---\nname: test-skill\n---\n\n# Test Skill\n\n"
    "## Core Principles\n\n1. Always verify inputs before proceeding.\n"
    "2. Log every significant decision for audit purposes.\n\n"
    "## Workflow\n\n### Step One: Validate inputs\nCheck all required fields.\n"
)
_LLM_BODY = (
    "---\nname: test-skill\n---\n\n# Test Skill\n\n"
    "## Core Principles\n\n1. Always verify inputs before proceeding.\n"
    "2. Log every significant decision for audit purposes.\n\n"
    "## Workflow\n\n### Step One: Validate inputs\nCheck all required fields.\n"
    "### Step Two: New step added by mutator\nDo the thing properly.\n"
)


def _make_genome(body: str | None = None) -> SkillGenome:
    return SkillGenome(name="test-skill", body=body if body is not None else _GENOME_BODY)


# ---------------------------------------------------------------------------
# BatchDiagnostic properties
# ---------------------------------------------------------------------------

class TestBatchDiagnostic:
    def test_avg_quality_score(self):
        batch = BatchDiagnostic(
            skill_name="s",
            diagnostics=[
                _make_diagnostic(quality_score=2.0),
                _make_diagnostic(quality_score=4.0),
            ],
            pass_rate=0.5,
        )
        assert batch.avg_quality_score == pytest.approx(3.0)

    def test_avg_quality_score_empty(self):
        batch = BatchDiagnostic(skill_name="s", diagnostics=[], pass_rate=0.0)
        assert batch.avg_quality_score == 0.0

    def test_improvement_none_when_no_previous(self):
        batch = BatchDiagnostic(skill_name="s", diagnostics=[], pass_rate=0.8)
        assert batch.improvement is None

    def test_improvement_calculated(self):
        batch = BatchDiagnostic(
            skill_name="s", diagnostics=[], pass_rate=0.9, previous_pass_rate=0.65
        )
        assert batch.improvement == pytest.approx(0.25)

    def test_aggregated_suggestions_deduplicated(self):
        d1 = _make_diagnostic(suggestions=["fix A", "fix B"])
        d2 = _make_diagnostic(suggestions=["fix B", "fix C"])
        batch = BatchDiagnostic(skill_name="s", diagnostics=[d1, d2], pass_rate=0.5)
        suggestions = batch.aggregated_suggestions
        assert suggestions == ["fix A", "fix B", "fix C"]

    def test_aggregated_violations_deduplicated(self):
        d1 = _make_diagnostic(violations=["skipped check 1", "skipped check 2"])
        d2 = _make_diagnostic(violations=["skipped check 2", "skipped check 3"])
        batch = BatchDiagnostic(skill_name="s", diagnostics=[d1, d2], pass_rate=0.5)
        assert batch.aggregated_violations == [
            "skipped check 1",
            "skipped check 2",
            "skipped check 3",
        ]

    def test_aggregated_failures_deduplicated(self):
        d1 = _make_diagnostic(failures=["infinite loop", "bad cast"])
        d2 = _make_diagnostic(failures=["bad cast", "silent swallow"])
        batch = BatchDiagnostic(skill_name="s", diagnostics=[d1, d2], pass_rate=0.5)
        assert batch.aggregated_failures == [
            "infinite loop",
            "bad cast",
            "silent swallow",
        ]

    def test_aggregated_violations_empty_when_no_diagnostics(self):
        batch = BatchDiagnostic(skill_name="s", diagnostics=[], pass_rate=0.0)
        assert batch.aggregated_violations == []
        assert batch.aggregated_failures == []

    def test_one_line_summary_with_improvement(self):
        batch = BatchDiagnostic(
            skill_name="my-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.9,
            previous_pass_rate=0.6,
        )
        summary = batch.one_line_summary()
        assert "my-skill" in summary
        assert "90%" in summary
        assert "+30%" in summary

    def test_one_line_summary_no_improvement(self):
        batch = BatchDiagnostic(
            skill_name="my-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.7,
        )
        summary = batch.one_line_summary()
        assert "+" not in summary


# ---------------------------------------------------------------------------
# SkillMutator.from_batch_diagnostic
# ---------------------------------------------------------------------------

class TestFromBatchDiagnostic:
    def _make_mutator(self, enabled: bool = True, llm_body: str | None = None):
        from loom.core.cognition.skill_mutator import SkillMutator

        router = MagicMock()
        mock_response = MagicMock()
        mock_response.text = llm_body or _LLM_BODY
        router.chat = AsyncMock(return_value=mock_response)

        return SkillMutator(router=router, model="test-model", enabled=enabled)

    async def test_returns_none_when_disabled(self):
        mutator = self._make_mutator(enabled=False)
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.5,
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is None

    async def test_returns_none_when_no_suggestions(self):
        mutator = self._make_mutator()
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic(suggestions=[])],
            pass_rate=0.5,
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is None

    async def test_returns_none_when_empty_parent_body(self):
        mutator = self._make_mutator()
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.5,
        )
        result = await mutator.from_batch_diagnostic(_make_genome(body="   "), batch)
        assert result is None

    async def test_fast_track_set_when_improvement_above_threshold(self):
        mutator = self._make_mutator()
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.9,
            previous_pass_rate=0.65,  # improvement = 0.25 ≥ 0.20
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is not None
        assert result.candidate.fast_track is True

    async def test_fast_track_false_when_improvement_below_threshold(self):
        mutator = self._make_mutator()
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.75,
            previous_pass_rate=0.65,  # improvement = 0.10 < 0.20
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is not None
        assert result.candidate.fast_track is False

    async def test_fast_track_false_when_no_previous(self):
        mutator = self._make_mutator()
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.9,
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is not None
        assert result.candidate.fast_track is False

    async def test_mutation_strategy_is_batch(self):
        mutator = self._make_mutator()
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.5,
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is not None
        assert result.candidate.mutation_strategy == "batch_meta_skill_engineer"

    async def test_pass_rate_in_pareto_scores(self):
        mutator = self._make_mutator()
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.75,
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is not None
        assert result.candidate.pareto_scores.get("pass_rate") == pytest.approx(0.75)

    async def test_llm_error_returns_none(self):
        from loom.core.cognition.skill_mutator import SkillMutator

        router = MagicMock()
        router.chat = AsyncMock(side_effect=RuntimeError("network down"))
        mutator = SkillMutator(router=router, model="test-model", enabled=True)

        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.5,
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is None

    async def test_aggregated_violations_and_failures_appear_in_prompt(self):
        """The batch rewrite prompt should receive violations/failures, not empty lists."""
        from loom.core.cognition.skill_mutator import SkillMutator

        router = MagicMock()
        mock_response = MagicMock()
        mock_response.text = _LLM_BODY
        router.chat = AsyncMock(return_value=mock_response)
        mutator = SkillMutator(router=router, model="test-model", enabled=True)

        d1 = _make_diagnostic(violations=["ignored rule 1"], failures=["loop"])
        d2 = _make_diagnostic(violations=["ignored rule 2"], failures=["loop", "bad cast"])
        batch = BatchDiagnostic(
            skill_name="test-skill", diagnostics=[d1, d2], pass_rate=0.5,
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is not None

        call_args = router.chat.await_args
        prompt = call_args.kwargs["messages"][0]["content"]
        assert "ignored rule 1" in prompt
        assert "ignored rule 2" in prompt
        assert "bad cast" in prompt
        assert "loop" in prompt

    async def test_fast_track_threshold_configurable(self):
        """fast_track should respect a custom threshold passed to the constructor."""
        from loom.core.cognition.skill_mutator import SkillMutator

        router = MagicMock()
        mock_response = MagicMock()
        mock_response.text = _LLM_BODY
        router.chat = AsyncMock(return_value=mock_response)
        mutator = SkillMutator(
            router=router, model="test-model", enabled=True,
            fast_track_threshold=0.05,  # very lenient
        )
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.75,
            previous_pass_rate=0.65,  # improvement = 0.10 ≥ 0.05
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is not None
        assert result.candidate.fast_track is True

    async def test_fast_track_threshold_strict_rejects_small_improvement(self):
        from loom.core.cognition.skill_mutator import SkillMutator

        router = MagicMock()
        mock_response = MagicMock()
        mock_response.text = _LLM_BODY
        router.chat = AsyncMock(return_value=mock_response)
        mutator = SkillMutator(
            router=router, model="test-model", enabled=True,
            fast_track_threshold=0.50,  # very strict
        )
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.75,
            previous_pass_rate=0.50,  # improvement = 0.25 < 0.50
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is not None
        assert result.candidate.fast_track is False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def db_conn(tmp_path):
    store = SQLiteStore(str(tmp_path / "test.db"))
    await store.initialize()
    async with store.connect() as conn:
        yield conn


# ---------------------------------------------------------------------------
# SkillGenome.maturity_tag + SkillCandidate.fast_track DB roundtrip
# ---------------------------------------------------------------------------

class TestSchemaExtensions:
    async def test_maturity_tag_roundtrip(self, db_conn):
        from loom.core.memory.procedural import ProceduralMemory

        proc = ProceduralMemory(db_conn)
        skill = SkillGenome(name="roundtrip-skill", body="# test", maturity_tag="mature")
        await proc.upsert(skill)

        fetched = await proc.get("roundtrip-skill")
        assert fetched is not None
        assert fetched.maturity_tag == "mature"

    async def test_update_maturity_tag(self, db_conn):
        from loom.core.memory.procedural import ProceduralMemory

        proc = ProceduralMemory(db_conn)
        skill = SkillGenome(name="mt-skill", body="# test")
        await proc.upsert(skill)

        updated = await proc.update_maturity_tag("mt-skill", "needs_improvement")
        assert updated is True

        fetched = await proc.get("mt-skill")
        assert fetched is not None
        assert fetched.maturity_tag == "needs_improvement"

    async def test_update_maturity_tag_clear(self, db_conn):
        from loom.core.memory.procedural import ProceduralMemory

        proc = ProceduralMemory(db_conn)
        skill = SkillGenome(name="mt2-skill", body="# test", maturity_tag="mature")
        await proc.upsert(skill)

        await proc.update_maturity_tag("mt2-skill", None)
        fetched = await proc.get("mt2-skill")
        assert fetched is not None
        assert fetched.maturity_tag is None

    async def test_candidate_fast_track_roundtrip(self, db_conn):
        from loom.core.memory.procedural import ProceduralMemory

        proc = ProceduralMemory(db_conn)
        skill = SkillGenome(name="ft-skill", body="# test")
        await proc.upsert(skill)

        candidate = SkillCandidate(
            parent_skill_name="ft-skill",
            parent_version=1,
            candidate_body="# improved",
            mutation_strategy="batch_meta_skill_engineer",
            fast_track=True,
        )
        await proc.insert_candidate(candidate)

        fetched = await proc.get_candidate(candidate.id)
        assert fetched is not None
        assert fetched.fast_track is True

    async def test_candidate_fast_track_default_false(self, db_conn):
        from loom.core.memory.procedural import ProceduralMemory

        proc = ProceduralMemory(db_conn)
        skill = SkillGenome(name="ft2-skill", body="# test")
        await proc.upsert(skill)

        candidate = SkillCandidate(
            parent_skill_name="ft2-skill",
            parent_version=1,
            candidate_body="# improved",
            mutation_strategy="apply_suggestions",
        )
        await proc.insert_candidate(candidate)

        fetched = await proc.get_candidate(candidate.id)
        assert fetched is not None
        assert fetched.fast_track is False


# ---------------------------------------------------------------------------
# Agent tools: generate_skill_candidate_from_batch + set_skill_maturity
# ---------------------------------------------------------------------------


def _make_tool_call(tool_name: str, args: dict):
    from loom.core.harness.middleware import ToolCall
    from loom.core.harness.permissions import TrustLevel

    return ToolCall(
        id=f"call-{tool_name}",
        tool_name=tool_name,
        args=args,
        trust_level=TrustLevel.GUARDED,
        session_id="test-sess",
    )


class TestGenerateSkillCandidateFromBatchTool:
    async def _make_env(self, db_conn, enabled: bool = True):
        from loom.core.cognition.skill_mutator import SkillMutator
        from loom.core.memory.procedural import ProceduralMemory
        from loom.platform.cli.tools import make_generate_skill_candidate_from_batch_tool

        proc = ProceduralMemory(db_conn)
        parent = SkillGenome(name="batch-skill", body=_GENOME_BODY)
        await proc.upsert(parent)

        router = MagicMock()
        mock_response = MagicMock()
        mock_response.text = _LLM_BODY
        router.chat = AsyncMock(return_value=mock_response)
        mutator = SkillMutator(router=router, model="test-model", enabled=enabled)

        tool = make_generate_skill_candidate_from_batch_tool(
            mutator, proc, session_id="test-sess",
        )
        return proc, mutator, tool

    async def test_generates_and_persists_candidate(self, db_conn):
        proc, _, tool = await self._make_env(db_conn)
        call = _make_tool_call("generate_skill_candidate_from_batch", {
            "skill_name": "batch-skill",
            "pass_rate": 0.9,
            "previous_pass_rate": 0.6,
            "mutation_suggestions": ["add verification step"],
            "instructions_violated": ["skipped audit"],
            "failure_patterns": ["silent swallow"],
        })
        result = await tool.executor(call)
        assert result.success is True
        candidate_id = result.metadata["candidate_id"]
        assert result.metadata["fast_track"] is True
        assert result.metadata["mutation_strategy"] == "batch_meta_skill_engineer"

        fetched = await proc.get_candidate(candidate_id)
        assert fetched is not None
        assert fetched.parent_skill_name == "batch-skill"
        assert fetched.fast_track is True

    async def test_missing_skill_name_fails(self, db_conn):
        _, _, tool = await self._make_env(db_conn)
        call = _make_tool_call("generate_skill_candidate_from_batch", {
            "pass_rate": 0.5,
            "mutation_suggestions": ["x"],
        })
        result = await tool.executor(call)
        assert result.success is False
        assert "skill_name" in result.error

    async def test_unknown_skill_fails(self, db_conn):
        _, _, tool = await self._make_env(db_conn)
        call = _make_tool_call("generate_skill_candidate_from_batch", {
            "skill_name": "does-not-exist",
            "pass_rate": 0.5,
            "mutation_suggestions": ["x"],
        })
        result = await tool.executor(call)
        assert result.success is False
        assert "not found" in result.error

    async def test_empty_suggestions_fails(self, db_conn):
        _, _, tool = await self._make_env(db_conn)
        call = _make_tool_call("generate_skill_candidate_from_batch", {
            "skill_name": "batch-skill",
            "pass_rate": 0.5,
            "mutation_suggestions": [],
        })
        result = await tool.executor(call)
        assert result.success is False
        assert "mutation_suggestions" in result.error

    async def test_mutator_disabled_returns_error(self, db_conn):
        _, _, tool = await self._make_env(db_conn, enabled=False)
        call = _make_tool_call("generate_skill_candidate_from_batch", {
            "skill_name": "batch-skill",
            "pass_rate": 0.5,
            "mutation_suggestions": ["x"],
        })
        result = await tool.executor(call)
        assert result.success is False
        assert "no candidate" in result.error.lower()


class TestSetSkillMaturityTool:
    async def _make_env(self, db_conn):
        from loom.core.memory.procedural import ProceduralMemory
        from loom.platform.cli.tools import make_set_skill_maturity_tool

        proc = ProceduralMemory(db_conn)
        await proc.upsert(SkillGenome(name="mature-skill", body="# x"))
        return proc, make_set_skill_maturity_tool(proc)

    async def test_sets_mature_tag(self, db_conn):
        proc, tool = await self._make_env(db_conn)
        call = _make_tool_call("set_skill_maturity", {
            "skill_name": "mature-skill", "tag": "mature",
        })
        result = await tool.executor(call)
        assert result.success is True
        fetched = await proc.get("mature-skill")
        assert fetched.maturity_tag == "mature"

    async def test_clears_tag_when_tag_is_clear(self, db_conn):
        proc, tool = await self._make_env(db_conn)
        await proc.update_maturity_tag("mature-skill", "mature")

        call = _make_tool_call("set_skill_maturity", {
            "skill_name": "mature-skill", "tag": "clear",
        })
        result = await tool.executor(call)
        assert result.success is True
        fetched = await proc.get("mature-skill")
        assert fetched.maturity_tag is None

    async def test_rejects_unknown_tag(self, db_conn):
        _, tool = await self._make_env(db_conn)
        call = _make_tool_call("set_skill_maturity", {
            "skill_name": "mature-skill", "tag": "bogus",
        })
        result = await tool.executor(call)
        assert result.success is False
        assert "mature" in result.error

    async def test_unknown_skill_fails(self, db_conn):
        _, tool = await self._make_env(db_conn)
        call = _make_tool_call("set_skill_maturity", {
            "skill_name": "does-not-exist", "tag": "mature",
        })
        result = await tool.executor(call)
        assert result.success is False
        assert "not found" in result.error
