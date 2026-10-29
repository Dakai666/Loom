"""#650 S1 agent-loop layer: can the main model find paraphrased experience
with at most three ``recall`` calls?

Spec docs/designs/61 r2 §1.2 item 2. The single-call layer (one ``recall``,
asserted in ``tests/test_memory_recall_s1.py``) is the hard gate; this layer
is measured and reported separately and must never be claimed as a
single-call improvement. The model rewrites its own search terms — no hidden
LLM step — and sees exactly the production ``recall`` tool: same schema,
same output formatting, against the synthetic S0 fixture (no live data).

A case is a hit when some recall output within the budget showed an expected
source with its evidence: a search result (the S0 ``evaluate`` rule), or a
``recall(ref=log:...)`` expansion whose section for that turn carries the
evidence. ``cited`` additionally asks whether the final answer named an
expected ref. ``doc-*`` cases need S2
(documents) and are skipped.

Usage::

    python scripts/memory_s1_agent_loop.py --output report.json [--model M]

Calls the configured LLM (a few dozen small requests).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path

from scripts.memory_s0 import CASES, build_fixture, evaluate

MAX_RECALLS = 3
SYSTEM = (
    "You are answering a question about your own past experience. Your memory "
    f"is reachable only through the recall tool, and you may call it at most "
    f"{MAX_RECALLS} times. When you are done, answer in one or two sentences and "
    "list the refs you relied on. If you found nothing, say so."
)


class _Facade:
    """The slice of MemoryFacade the recall tool uses, recording results."""

    def __init__(self, search):
        self._search = search
        self.seen: list[list] = []
        self.expanded: list[str] = []

    async def search(self, query, *, kind="all", limit=5, domain=None, temporal=None,
                     since=None, until=None, mark_accessed=True):
        found = await self._search.recall(
            query, type=kind, limit=limit, domain=domain, temporal=temporal,
            since=since, until=until, mark_accessed=mark_accessed,
        )
        self.seen.append(list(found))
        return found

    async def mark_accessed(self, results):
        await self._search.mark_accessed(results)

    async def expand_ref(self, ref, mark_accessed=True):
        out = await self._search.expand(ref, mark_accessed=mark_accessed)
        if out.found:
            self.expanded.append(out.text)
        return out


def _expansion_shows(gold: dict, expansions: list[str]) -> bool:
    """Did a log: expansion show *gold*'s turn with all its evidence?"""
    if not gold["ref"].startswith("log:"):
        return False
    sid, turn = gold["ref"][4:].rsplit(":", 1)
    for text in expansions:
        if not text.startswith(f"log:{sid}:"):
            continue
        for section in text.split("\n── turn ")[1:]:
            if section.split(" ", 1)[0].split(":", 1)[0] == turn and all(
                    fragment in section for fragment in gold["contains"]):
                return True
    return False


async def _run_case(case, router, model, tool, facade: _Facade) -> dict:
    from loom.core.harness.middleware import ToolCall
    from loom.core.harness.permissions import TrustLevel

    facade.seen.clear()
    facade.expanded.clear()
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": case["query"]}]
    schema = [tool.to_openai_schema()]  # the shape the session sends
    calls: list[dict] = []
    answer = ""
    t0 = time.perf_counter()
    for _ in range(MAX_RECALLS + 2):
        resp = await router.chat(model, messages, tools=schema, max_tokens=4096)
        messages.append(resp.raw_message)
        if not resp.tool_uses:
            answer = resp.text or ""
            break
        for tu in resp.tool_uses:
            if len(calls) >= MAX_RECALLS:
                content, ok = f"recall budget ({MAX_RECALLS} calls) used up — answer now.", False
            else:
                result = await tool.executor(ToolCall(
                    id=tu.id, tool_name="recall", args=tu.args,
                    trust_level=TrustLevel.SAFE, session_id="s1-agent-loop"))
                content = str(result.output) if result.success else (result.error or "")
                ok = result.success
                calls.append({"args": tu.args, "success": ok})
            messages.append(router.format_tool_result(model, tu.id, content, ok))
    elapsed = time.perf_counter() - t0

    normalized = [[{"ref": r.ref, "excerpt": r.value} for r in found] for found in facade.seen]
    by_search = any(evaluate(case, n)["hit_at_5"] for n in normalized)
    by_expansion = any(_expansion_shows(g, facade.expanded) for g in case["expected"])
    hit = (by_search or by_expansion) if case["expected"] else None
    expected_refs = {g["ref"] for g in case["expected"]}
    return {
        "id": case["id"], "query": case["query"], "recall_calls": len(calls),
        "calls": calls, "hit_within_budget": hit,
        "hit_via": ("search" if by_search else "expansion" if by_expansion else None),
        "cited": bool(expected_refs) and any(r in answer for r in expected_refs),
        "seconds": round(elapsed, 2), "answer": answer,
    }


async def main_async(output: Path, model: str | None) -> None:
    from loom.core.cognition.router import get_default_model
    from loom.core.memory.procedural import ProceduralMemory
    from loom.core.memory.search import MemorySearch
    from loom.core.memory.semantic import SemanticMemory
    from loom.core.session import build_router
    from loom.platform.cli.tools import make_recall_tool
    from tests.test_memory_recall_s1 import SINGLE_CALL_LAYER

    model = model or get_default_model()
    router = build_router(active_model=model)
    fixture = json.loads(CASES.read_text())
    cases = [c for c in fixture["cases"]
             if c["id"] not in SINGLE_CALL_LAYER and not c["id"].startswith("doc-")]
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        store = await build_fixture(Path(tmp) / "fixture.db", fixture)
        async with store.connect() as db:
            facade = _Facade(MemorySearch(SemanticMemory(db), ProceduralMemory(db)))
            tool = make_recall_tool(facade)  # type: ignore[arg-type]
            for case in cases:
                row = await _run_case(case, router, model, tool, facade)
                rows.append(row)
                print(f"{row['id']:<28} hit={row['hit_within_budget']!s:<5} "
                      f"cited={row['cited']!s:<5} calls={row['recall_calls']} {row['seconds']}s")
    hits = sum(1 for r in rows if r["hit_within_budget"])
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": model, "max_recalls": MAX_RECALLS, "cases": len(rows),
        "hit_within_budget": hits, "cited": sum(r["cited"] for r in rows),
        "mean_recall_calls": round(sum(r["recall_calls"] for r in rows) / max(len(rows), 1), 2),
        "rows": rows,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nagent-loop layer: {hits}/{len(rows)} found within {MAX_RECALLS} recalls, "
          f"{report['cited']} cited; mean {report['mean_recall_calls']} calls ({model})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model")
    args = parser.parse_args()
    asyncio.run(main_async(args.output, args.model))


if __name__ == "__main__":
    main()
