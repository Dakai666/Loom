"""Self-review batches that yield no verdict at all are split and retried.

A reasoning model can spend the whole ``max_tokens`` budget thinking about a
large batch and return empty text (seen live: 10 distill neighbourhoods, 19k
prompt chars → stop_reason=max_tokens, 0 chars; the convergent dream lost a
whole batch the same way on 2026-09-28). Halving the batch keeps every item
reviewable; a single item that still yields nothing is deferred as before.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from loom.core.cognition.consolidation import (
    VERDICT_APPROVE,
    VERDICT_DEFER,
    _review_batch,
)


@dataclass
class _Item:
    cluster_id: str


def _render(item) -> str:
    return f'neighbourhood_id="{item.cluster_id}"'


def _llm(*, max_items: int, calls: list):
    """Answers properly for batches of ≤ max_items, returns empty text otherwise."""
    async def fn(messages):
        ids = [line.split('"')[1] for line in messages[-1]["content"].splitlines()
               if line.startswith('neighbourhood_id="')]
        calls.append(len(ids))
        if len(ids) > max_items:
            return ""                       # budget spent on reasoning
        return json.dumps([{"cluster_id": i, "verdict": "approve", "reason": "ok"} for i in ids])
    return fn


async def _review(items, llm):
    return await _review_batch(items, llm, system="s", render=_render)


async def test_empty_answer_splits_until_every_item_is_reviewed():
    calls: list = []
    items = [_Item(f"c{i}") for i in range(10)]
    decisions = await _review(items, _llm(max_items=3, calls=calls))
    assert [d.cluster_id for d in decisions] == [i.cluster_id for i in items]
    assert all(d.verdict == VERDICT_APPROVE for d in decisions)
    assert calls[0] == 10 and sum(c for c in calls if c <= 3) == 10


async def test_a_single_item_with_no_answer_is_deferred_not_retried_forever():
    calls: list = []
    (d,) = await _review([_Item("c0")], _llm(max_items=0, calls=calls))
    assert d.verdict == VERDICT_DEFER
    assert calls == [1]


async def test_partial_answers_are_not_split():
    calls: list = []

    async def fn(messages):
        calls.append(1)
        return json.dumps([{"cluster_id": "c0", "verdict": "approve", "reason": "ok"}])
    decisions = await _review([_Item("c0"), _Item("c1")], fn)
    assert [d.verdict for d in decisions] == [VERDICT_APPROVE, VERDICT_DEFER]
    assert calls == [1]


async def test_provider_errors_are_not_split():
    calls: list = []

    async def fn(messages):
        calls.append(1)
        raise RuntimeError("provider down")
    decisions = await _review([_Item(f"c{i}") for i in range(4)], fn)
    assert all(d.verdict == VERDICT_DEFER for d in decisions)
    assert calls == [1]


async def test_a_veto_inside_a_split_batch_still_holds():
    async def fn(messages):
        ids = [line.split('"')[1] for line in messages[-1]["content"].splitlines()
               if line.startswith('neighbourhood_id="')]
        if len(ids) > 1:
            return "not json"
        verdict = "skip" if ids[0] == "c1" else "approve"
        return json.dumps([{"cluster_id": ids[0], "verdict": verdict, "reason": "r"}])
    decisions = await _review([_Item("c0"), _Item("c1"), _Item("c2")], fn)
    assert [d.verdict for d in decisions] == ["approve", "skip", "approve"]


def test_distill_reviews_in_small_batches():
    import inspect

    from loom.core.cognition.distillation import run_distill_dream
    default = inspect.signature(run_distill_dream).parameters["review_batch_size"].default
    assert default <= 5
