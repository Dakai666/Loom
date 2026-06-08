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

# duration_bucket thresholds (ms). A bet names the bucket it expects.
_DURATION_BUCKETS = (("fast", 1_000.0), ("medium", 10_000.0))  # else "slow"


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
        return _binary(present == resolver.get("expect", True), f"present={present}")

    if kind == "output_regex":
        output = observation["output"]
        hit = re.search(resolver["pattern"], output) is not None
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

    # duration_bucket
    actual_bucket = _bucket_for(observation["duration_ms"])
    return _binary(actual_bucket == resolver["expect"], f"bucket={actual_bucket}")
