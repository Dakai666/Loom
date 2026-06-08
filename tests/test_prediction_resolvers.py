"""
Prediction Spine — P0 slice 2: resolver whitelist (epic #528, spec §4/§6).

P0 only admits **mechanically reconcilable** resolvers — given a resolver spec
and a normalized observation, each returns a deterministic verdict
(``matched`` + ``error_score``). No LLM, no free-text "vibe" judging (those are
deferred so I2's ground-truth guarantee cannot slip back to self-narrative).

A resolver kind that is not on the whitelist must raise — P0 refuses to score
anything it cannot judge mechanically.
"""

import pytest

from loom.core.memory.resolvers import resolve, ResolverResult, KNOWN_RESOLVERS


class TestWhitelist:
    def test_known_resolvers_are_the_p0_set(self):
        assert KNOWN_RESOLVERS == {
            "tool_success",
            "final_state",
            "output_contains",
            "output_regex",
            "file_digest_changed",
            "row_count",
            "duration_bucket",
        }

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError):
            resolve({"kind": "vibe_check"}, {"output": "anything"})

    def test_missing_kind_raises(self):
        with pytest.raises(ValueError):
            resolve({}, {})


class TestToolSuccess:
    def test_correct_prediction_scores_zero(self):
        r = resolve({"kind": "tool_success", "expect": True}, {"tool_success": True})
        assert isinstance(r, ResolverResult)
        assert r.matched is True
        assert r.error_score == 0.0

    def test_wrong_prediction_scores_one(self):
        r = resolve({"kind": "tool_success", "expect": True}, {"tool_success": False})
        assert r.matched is False
        assert r.error_score == 1.0


class TestFinalState:
    def test_state_in_expected_set(self):
        r = resolve(
            {"kind": "final_state", "expect_in": ["completed", "ok"]},
            {"final_state": "completed"},
        )
        assert r.matched is True
        assert r.error_score == 0.0

    def test_state_not_in_expected_set(self):
        r = resolve(
            {"kind": "final_state", "expect_in": ["completed"]},
            {"final_state": "denied"},
        )
        assert r.matched is False
        assert r.error_score == 1.0


class TestOutputContains:
    def test_needle_present_as_expected(self):
        r = resolve(
            {"kind": "output_contains", "needle": "PASS", "expect": True},
            {"output": "all tests PASS"},
        )
        assert r.matched is True

    def test_needle_absent_when_expected_present(self):
        r = resolve(
            {"kind": "output_contains", "needle": "PASS", "expect": True},
            {"output": "1 failed"},
        )
        assert r.matched is False

    def test_expect_absent(self):
        r = resolve(
            {"kind": "output_contains", "needle": "ERROR", "expect": False},
            {"output": "clean run"},
        )
        assert r.matched is True


class TestOutputRegex:
    def test_pattern_matches(self):
        r = resolve(
            {"kind": "output_regex", "pattern": r"\d+ passed", "expect": True},
            {"output": "16 passed in 0.1s"},
        )
        assert r.matched is True

    def test_pattern_does_not_match(self):
        r = resolve(
            {"kind": "output_regex", "pattern": r"\d+ passed", "expect": True},
            {"output": "collection error"},
        )
        assert r.matched is False


class TestFileDigestChanged:
    def test_changed_as_expected(self):
        r = resolve(
            {"kind": "file_digest_changed", "expect": True},
            {"digest_before": "aaa", "digest_after": "bbb"},
        )
        assert r.matched is True

    def test_unchanged_when_change_expected(self):
        r = resolve(
            {"kind": "file_digest_changed", "expect": True},
            {"digest_before": "aaa", "digest_after": "aaa"},
        )
        assert r.matched is False


class TestRowCount:
    def test_exact_match_scores_zero(self):
        r = resolve({"kind": "row_count", "expect": 10}, {"row_count": 10})
        assert r.matched is True
        assert r.error_score == 0.0

    def test_within_tolerance_matches(self):
        r = resolve(
            {"kind": "row_count", "expect": 10, "tolerance": 2},
            {"row_count": 12},
        )
        assert r.matched is True

    def test_graded_error_score_outside_tolerance(self):
        r = resolve({"kind": "row_count", "expect": 10}, {"row_count": 15})
        assert r.matched is False
        assert 0.0 < r.error_score <= 1.0


class TestDurationBucket:
    def test_bucket_matches_expectation(self):
        r = resolve(
            {"kind": "duration_bucket", "expect": "fast"},
            {"duration_ms": 200},
        )
        assert r.matched is True

    def test_bucket_misses_expectation(self):
        r = resolve(
            {"kind": "duration_bucket", "expect": "fast"},
            {"duration_ms": 60_000},
        )
        assert r.matched is False
        assert r.error_score == 1.0


class TestMissingObservationField:
    def test_missing_field_is_unresolvable_not_a_silent_pass(self):
        """A resolver that can't see its field must not silently score 0."""
        with pytest.raises(KeyError):
            resolve({"kind": "tool_success", "expect": True}, {})
