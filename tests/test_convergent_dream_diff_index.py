"""
Tests for index-addressed diff inventory (#603).

The 2026-09-30 dream pass deferred every large merge cluster (7–42 members) as
a tool failure. Two structural causes, both in the old wire contract that made
the LLM echo every long fact key back verbatim as a JSON key:

  - echo corruption: one 14-member cluster came back with
    ``20260723:010406`` for ``20260723T010406`` → coverage miss → fail safe;
  - output bloat: N long keys + N descriptions overflow the dream ``llm_fn``
    budget (``max_tokens=2048``) → truncated JSON → "unparseable".

Contract: members are numbered ``[1]..[N]`` in the prompt; the LLM answers with
``unique_by_index`` and code maps indices back to keys, so
``DiffInventory.unique_by_key`` (consumed downstream) stays keyed by real keys.
The gate remains a hard fail-safe: an index set that is not exactly 1..N is
untrustworthy. A correctly-echoed legacy ``unique_by_key`` answer is still
honoured, under the same strict coverage check.
"""

from __future__ import annotations

import json

from loom.core.cognition.consolidation import (
    CandidateCluster,
    DIFF_ERROR,
    DIFF_OK,
    KIND_MERGE,
    diff_inventory,
)
from loom.core.memory.semantic import SemanticEntry


def _cluster(*keys: str) -> CandidateCluster:
    return CandidateCluster(
        cluster_id="c1", kind=KIND_MERGE,
        members=[SemanticEntry(key=k, value=f"val-{k}") for k in keys],
    )


class _Seq:
    """Returns queued responses in order; records calls and prompts."""

    def __init__(self, *responses: str):
        self._r = list(responses)
        self.calls = 0
        self.prompts: list[list[dict]] = []

    async def __call__(self, messages):
        self.calls += 1
        self.prompts.append(messages)
        return self._r[min(self.calls - 1, len(self._r) - 1)]


def _by_index(ubi: dict, relation: str = "duplicate") -> str:
    return json.dumps(
        {"unique_by_index": ubi, "relation": relation, "rationale": "r"},
    )


# Real-shaped keys from the 2026-09-30 report (the 14-member cluster).
_LONG_KEYS = [
    f"session:0ebd7dca:202607{d:02d}T010459:fact:{d:012x}" for d in range(1, 15)
]


class TestPromptContract:
    async def test_members_numbered_and_index_contract_requested(self):
        llm = _Seq(_by_index({"1": "", "2": ""}))
        await diff_inventory(_cluster("a", "b"), llm)
        system = llm.prompts[0][0]["content"]
        user = llm.prompts[0][-1]["content"]
        assert "unique_by_index" in system
        assert "[1]" in user and "[2]" in user
        # Original keys stay visible as context (dates / provenance matter for
        # the snapshot-vs-facet judgement).
        assert 'key="a"' in user and 'key="b"' in user


class TestIndexMapping:
    async def test_indices_map_back_to_real_keys(self):
        llm = _Seq(_by_index({"1": "", "2": "extra detail"}))
        diff = await diff_inventory(_cluster("a", "b"), llm)
        assert diff.status == DIFF_OK
        assert diff.mergeable is True
        assert diff.unique_by_key == {"a": "", "b": "extra detail"}
        assert llm.calls == 1

    async def test_integer_indices_accepted(self):
        raw = '{"unique_by_index": {"1": "", "2": "x"}, "relation": "extends", "rationale": "r"}'
        # JSON object keys are always strings, but tolerate whitespace/int-ish.
        raw = raw.replace('"1"', '" 1"')
        diff = await diff_inventory(_cluster("a", "b"), _Seq(raw))
        assert diff.status == DIFF_OK
        assert diff.unique_by_key == {"a": "", "b": "x"}

    async def test_large_cluster_with_long_keys_covered_on_first_call(self):
        # The exact cluster that failed on 2026-09-30: under the index
        # contract the LLM never re-types a key, so echo corruption can't
        # cause a coverage miss.
        ubi = {str(i): "" for i in range(1, len(_LONG_KEYS) + 1)}
        llm = _Seq(_by_index(ubi))
        diff = await diff_inventory(_cluster(*_LONG_KEYS), llm)
        assert diff.status == DIFF_OK
        assert set(diff.unique_by_key) == set(_LONG_KEYS)
        assert llm.calls == 1


class TestIndexFailSafe:
    async def test_missing_index_fails_safe(self):
        bad = _by_index({"1": ""})
        diff = await diff_inventory(_cluster("a", "b"), _Seq(bad, bad))
        assert diff.status == DIFF_ERROR
        assert diff.mergeable is False

    async def test_out_of_range_index_fails_safe(self):
        bad = _by_index({"1": "", "2": "", "3": "invented"})
        diff = await diff_inventory(_cluster("a", "b"), _Seq(bad, bad))
        assert diff.status == DIFF_ERROR

    async def test_non_numeric_index_fails_safe(self):
        bad = _by_index({"1": "", "b": ""})
        diff = await diff_inventory(_cluster("a", "b"), _Seq(bad, bad))
        assert diff.status == DIFF_ERROR

    async def test_index_miss_retries_then_recovers(self):
        bad = _by_index({"1": ""})
        good = _by_index({"1": "", "2": ""})
        llm = _Seq(bad, good)
        diff = await diff_inventory(_cluster("a", "b"), llm)
        assert diff.status == DIFF_OK
        assert llm.calls == 2


class TestLegacyKeyContract:
    async def test_correct_key_echo_still_honoured(self):
        raw = '{"unique_by_key": {"a": "", "b": "x"}, "relation": "duplicate", "rationale": "r"}'
        diff = await diff_inventory(_cluster("a", "b"), _Seq(raw))
        assert diff.status == DIFF_OK
        assert diff.unique_by_key == {"a": "", "b": "x"}

    async def test_corrupted_key_echo_still_fails_safe(self):
        # The 2026-09-30 corruption (T → :) must not be "fuzzily" accepted.
        echoed = [k.replace("T010459", ":010459") if i == 2 else k
                  for i, k in enumerate(_LONG_KEYS)]
        raw = json.dumps({"unique_by_key": {k: "" for k in echoed},
                          "relation": "duplicate", "rationale": "r"})
        diff = await diff_inventory(_cluster(*_LONG_KEYS), _Seq(raw, raw))
        assert diff.status == DIFF_ERROR


class TestTruncationObservability:
    async def test_unparseable_reports_output_size(self):
        # A truncated JSON body (budget overflow) must be distinguishable from
        # jitter in the report: record how much the model actually produced.
        truncated = '{"unique_by_index": {"1": "a long description that got cut'
        diff = await diff_inventory(_cluster("a", "b"), _Seq(truncated, truncated))
        assert diff.status == DIFF_ERROR
        assert "unparseable" in diff.rationale
        assert f"raw_chars={len(truncated)}" in diff.rationale
