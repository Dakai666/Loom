"""
Prediction Spine — resolver whitelist (epic #528, spec docs/designs/58 §4/§6).

A resolver mechanically judges whether a prediction came true, given a
*normalized observation* extracted from runtime ground truth (action_records /
session_log — slice 3 does the extraction). Each resolver is a pure function
of ``(resolver_spec, observation)`` returning a deterministic ``ResolverResult``.

P0 admits **only mechanical** resolvers (this whitelist). Free-text "vibe"
judging is deliberately excluded: it would let ground truth slip back to LLM
self-narrative and break I2. An unknown resolver kind raises — P0 refuses to
score what it cannot judge mechanically.

A resolver whose observation is missing the field it needs raises ``KeyError``
rather than silently scoring 0.0 — an unobservable bet is *unresolved*, not
*correct* (no-silent-pass; pairs with I4's "unreconciled ≠ verified").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

KNOWN_RESOLVERS = {
    "tool_success",
    "final_state",
    "output_contains",
    "output_regex",
    "file_digest_changed",
    "row_count",
    "duration_bucket",
}

# Resolver kinds whose required fields the action observation surface does NOT
# capture yet (#569 Q3: before/after file digests need a pre-action snapshot —
# a real lifecycle slice, deferred). The predict tool refuses these at write
# time: a bet that can never settle rots ``pending`` forever, and that silent
# rot is the exact failure mode that starved the explicit path (spec 59 §8).
ACTION_UNOBSERVABLE_RESOLVERS = frozenset({"file_digest_changed"})

# duration_bucket thresholds (ms). A bet names the bucket it expects.
_DURATION_BUCKETS = (("fast", 1_000.0), ("medium", 10_000.0))  # else "slow"
# Ordered bucket scale — distance between predicted/actual grades the error so a
# one-bucket miss is half-wrong, not as wrong as a two-bucket miss (#528 §6).
_DURATION_ORDER = ("fast", "medium", "slow")


@dataclass
class ResolverResult:
    matched: bool          # did the prediction come true?
    error_score: float     # 0.0 = perfectly predicted, 1.0 = fully wrong
    detail: str = ""


def _binary(matched: bool, detail: str = "") -> ResolverResult:
    return ResolverResult(matched=matched, error_score=0.0 if matched else 1.0, detail=detail)


def _bucket_for(duration_ms: float) -> str:
    for name, ceiling in _DURATION_BUCKETS:
        if duration_ms < ceiling:
            return name
    return "slow"


def resolve(resolver: dict, observation: dict) -> ResolverResult:
    """Judge a prediction mechanically. See module docstring for contract."""
    kind = resolver.get("kind")
    if kind not in KNOWN_RESOLVERS:
        raise ValueError(
            f"unknown resolver kind {kind!r}; P0 whitelist is {sorted(KNOWN_RESOLVERS)}"
        )

    if kind == "tool_success":
        actual = observation["tool_success"]  # KeyError if unobserved
        return _binary(actual == resolver.get("expect", True), f"tool_success={actual}")

    if kind == "final_state":
        actual = observation["final_state"]
        expect_in = resolver.get("expect_in") or []
        if not expect_in:
            # Fail-fast: an empty expect_in would silently judge every state
            # wrong (x in [] is always False). A resolver spec that forgot its
            # set is a write-time mistake, not a 100%-error prediction (絲絲 review).
            raise ValueError("final_state resolver requires a non-empty expect_in")
        return _binary(actual in expect_in, f"final_state={actual}")

    if kind == "output_contains":
        output = observation["output"]
        present = resolver["needle"] in output
        # A miss against a truncated capture proves nothing either way — the
        # needle may live past the cap. Unresolvable, never a false "absent"
        # (#569, spec 59 §9.1). A hit in the prefix is a hit in the full text.
        if not present and observation.get("output_truncated"):
            raise KeyError("needle not found in truncated output capture")
        return _binary(present == resolver.get("expect", True), f"present={present}")

    if kind == "output_regex":
        output = observation["output"]
        hit = re.search(resolver["pattern"], output) is not None
        if not hit and observation.get("output_truncated"):
            raise KeyError("pattern not found in truncated output capture")
        return _binary(hit == resolver.get("expect", True), f"regex_hit={hit}")

    if kind == "file_digest_changed":
        changed = observation["digest_before"] != observation["digest_after"]
        return _binary(changed == resolver.get("expect", True), f"changed={changed}")

    if kind == "row_count":
        expect = resolver["expect"]
        actual = observation["row_count"]
        tolerance = resolver.get("tolerance", 0)
        delta = abs(actual - expect)
        matched = delta <= tolerance
        error_score = 0.0 if matched else min(1.0, delta / max(1, abs(expect)))
        return ResolverResult(matched, error_score, f"expect={expect} actual={actual}")

    # duration_bucket — graded by ordinal bucket distance (was binary). A flat
    # match/no-match collapses the latency heartbeat to a near-constant 0.0
    # corpus that can't carry the §8 non-triviality gate; distance/range keeps
    # ``matched`` exact-hit only while letting error_score grade the magnitude.
    actual_bucket = _bucket_for(observation["duration_ms"])
    expect_bucket = resolver["expect"]
    distance = abs(
        _DURATION_ORDER.index(actual_bucket) - _DURATION_ORDER.index(expect_bucket)
    )
    error_score = distance / (len(_DURATION_ORDER) - 1)
    return ResolverResult(
        matched=actual_bucket == expect_bucket,
        error_score=error_score,
        detail=f"bucket={actual_bucket} expect={expect_bucket}",
    )
