"""
Tests for the new Issue #279 telemetry dimensions.
"""
import pytest
from loom.core.infra.telemetry import (
    RuntimeIdentityDimension,
    ContextBudgetDimension,
    SessionTurnsDimension,
    LoadedSkillsDimension,
)
from loom.core.cognition.context import ContextBudget


class TestRuntimeIdentityDimension:
    def test_default_state(self):
        dim = RuntimeIdentityDimension()
        snap = dim.snapshot()
        assert snap["model"] == "unknown"
        assert snap["tier"] == 1

    def test_update_and_summary(self):
        dim = RuntimeIdentityDimension()
        dim.update(model="deepseek-v4-pro", tier=2,
                   tier_models={1: "minimax-m2.7", 2: "deepseek-v4-pro"})
        snap = dim.snapshot()
        assert snap["model"] == "deepseek-v4-pro"
        assert snap["tier"] == 2
        assert snap["tier_models"]["1"] == "minimax-m2.7"
        summary = dim.render_summary()
        assert "deepseek-v4-pro" in summary
        assert "T2" in summary

    def test_detail_includes_tier_models(self):
        dim = RuntimeIdentityDimension()
        dim.update(model="gpt-5", tier=3, tier_models={1: "a", 3: "c"})
        detail = dim.render_detail()
        assert "T1=a" in detail
        assert "T3=c" in detail


class TestContextBudgetDimension:
    def test_no_budget_defaults(self):
        dim = ContextBudgetDimension()
        snap = dim.snapshot()
        assert snap["used"] == 0
        assert snap["total"] == 0

    def test_with_budget(self):
        budget = ContextBudget(total_tokens=100000)
        budget.record_response(input_tokens=30000, output_tokens=2000)
        dim = ContextBudgetDimension(budget=budget)
        snap = dim.snapshot()
        assert snap["used"] == 32000
        assert snap["total"] == 100000
        assert snap["remaining"] == 68000

    def test_anomaly_when_over_threshold(self):
        budget = ContextBudget(total_tokens=100000)
        budget.record_response(input_tokens=85000, output_tokens=1000)
        dim = ContextBudgetDimension(budget=budget)
        assert dim.has_anomaly() is True
        msg = dim.describe_anomaly()
        assert msg is not None
        assert "auto-compact" in msg
        assert "next turn" in msg
        assert "current tool batch" in msg

    def test_no_anomaly_when_under_threshold(self):
        budget = ContextBudget(total_tokens=100000)
        budget.record_response(input_tokens=30000, output_tokens=1000)
        dim = ContextBudgetDimension(budget=budget)
        assert dim.has_anomaly() is False
        assert dim.describe_anomaly() is None

    def test_summary_shows_percentage(self):
        budget = ContextBudget(total_tokens=100000)
        budget.record_response(input_tokens=45000, output_tokens=5000)
        dim = ContextBudgetDimension(budget=budget)
        assert "50%" in dim.render_summary()


class TestSessionTurnsDimension:
    def test_starts_at_zero(self):
        dim = SessionTurnsDimension(turn_index_fn=lambda: 0)
        assert dim.snapshot()["turns"] == 0

    def test_reads_from_lambda(self):
        _calls = [0]
        def _counter():
            _calls[0] += 1
            return _calls[0]
        dim = SessionTurnsDimension(turn_index_fn=_counter)
        assert dim.snapshot()["turns"] == 1
        assert dim.snapshot()["turns"] == 2
        assert dim.snapshot()["turns"] == 3

    def test_detail_shows_current_turn(self):
        dim = SessionTurnsDimension(turn_index_fn=lambda: 7)
        detail = dim.render_detail()
        assert "current turn: 7" in detail


class TestLoadedSkillsDimension:
    def test_empty_by_default(self):
        dim = LoadedSkillsDimension()
        snap = dim.snapshot()
        assert snap["count"] == 0
        assert snap["skills"] == []

    def test_update_and_sort(self):
        dim = LoadedSkillsDimension()
        dim.update(["code_weaver", "deep_researcher", "audio_transcriber"])
        snap = dim.snapshot()
        assert snap["count"] == 3
        assert snap["skills"] == ["audio_transcriber", "code_weaver", "deep_researcher"]

    def test_detail_lists_skills(self):
        dim = LoadedSkillsDimension()
        dim.update(["code_weaver"])
        detail = dim.render_detail()
        assert "code_weaver" in detail
