"""
Tests for diff-inventory gate robustness (#554).

The gate must stay a hard fail-safe (spec #493 — never mark a cluster mergeable
on untrustworthy output), but a *single* retry should absorb transient LLM
format jitter so a large near-duplicate cluster isn't permanently stuck on one
malformed response. A clean verdict (even mergeable=false) must NOT trigger a
retry, and two bad responses must still fail safe.
"""

from __future__ import annotations

from loom.core.cognition.consolidation import (
    CandidateCluster,
    KIND_MERGE,
    diff_inventory,
)
from loom.core.memory.semantic import SemanticEntry


def _cluster(*keys: str) -> CandidateCluster:
    return CandidateCluster(
        cluster_id="c1", kind=KIND_MERGE,
        members=[SemanticEntry(key=k, value=f"val-{k}") for k in keys],
    )


class _SequenceLLM:
    """Returns queued responses in order; records how many times it was called."""

    def __init__(self, *responses: str):
        self._responses = list(responses)
        self.calls = 0

    async def __call__(self, messages):
        self.calls += 1
        idx = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[idx]


class TestDiffInventoryRetry:
    async def test_retry_recovers_from_transient_unparseable(self):
        good = ('{"unique_by_key": {"a": "", "b": "extra"}, '
                '"mergeable": true, "rationale": "b subsumes a"}')
        llm = _SequenceLLM("garbage not json", good)
        diff = await diff_inventory(_cluster("a", "b"), llm)
        assert diff.mergeable is True
        assert llm.calls == 2

    async def test_retry_recovers_from_transient_coverage_mismatch(self):
        # First response drops a member key; second covers the cluster.
        bad = '{"unique_by_key": {"a": ""}, "mergeable": true, "rationale": "r"}'
        good = ('{"unique_by_key": {"a": "", "b": ""}, '
                '"mergeable": true, "rationale": "r"}')
        llm = _SequenceLLM(bad, good)
        diff = await diff_inventory(_cluster("a", "b"), llm)
        assert diff.mergeable is True
        assert llm.calls == 2

    async def test_two_bad_responses_still_fail_safe(self):
        llm = _SequenceLLM("garbage", "still garbage")
        diff = await diff_inventory(_cluster("a", "b"), llm)
        assert diff.mergeable is False
        assert llm.calls == 2
        assert "unparseable" in diff.rationale

    async def test_clean_false_verdict_does_not_retry(self):
        # A parseable, covered "both unique" verdict is a real answer — the gate
        # must accept it on the first call, not waste a retry.
        resp = ('{"unique_by_key": {"a": "preference", "b": "complaint"}, '
                '"mergeable": false, "rationale": "distinct insights"}')
        llm = _SequenceLLM(resp, resp)
        diff = await diff_inventory(_cluster("a", "b"), llm)
        assert diff.mergeable is False
        assert llm.calls == 1

    async def test_clean_true_verdict_does_not_retry(self):
        resp = ('{"unique_by_key": {"a": "", "b": "extra"}, '
                '"mergeable": true, "rationale": "r"}')
        llm = _SequenceLLM(resp, resp)
        diff = await diff_inventory(_cluster("a", "b"), llm)
        assert diff.mergeable is True
        assert llm.calls == 1

    async def test_retry_on_transient_exception_then_success(self):
        good = ('{"unique_by_key": {"a": "", "b": "extra"}, '
                '"mergeable": true, "rationale": "r"}')
        calls = {"n": 0}

        async def llm(messages):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient down")
            return good

        diff = await diff_inventory(_cluster("a", "b"), llm)
        assert diff.mergeable is True
        assert calls["n"] == 2

    async def test_failure_rationale_carries_member_count(self):
        # Observability (#554): the fail-safe rationale should say how big the
        # cluster was so a maintainer can tell jitter from a structural limit.
        llm = _SequenceLLM("garbage", "garbage")
        diff = await diff_inventory(_cluster("a", "b", "c"), llm)
        assert diff.mergeable is False
        assert "members=3" in diff.rationale
