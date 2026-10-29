"""#650 S1 agent-loop layer: can the main model find paraphrased experience
with at most three ``recall`` calls?

Spec docs/designs/61 r2 §1.2 item 2. The single-call layer (one ``recall``,
asserted in ``tests/test_memory_recall_s1.py``) is the hard gate; this layer
is measured and reported separately and must never be claimed as a
single-call improvement. The model rewrites its own search terms — no hidden
LLM step — and sees exactly the production ``recall`` tool: same schema,
same output formatting, against the synthetic S0 fixture (no live data).

Scoring reads only what the model was actually sent (#662 review): a case is
a hit when a recall output within the budget showed an expected source with
its evidence — a search result block whose visible text carries it, or a
``recall(ref=log:...)`` expansion whose section for that turn carries it.
Evidence cut by formatting or the output budget does not count. Refs are
parsed to their canonical identity (version suffix dropped, turn split at the
last colon) and compared exactly; ``cited`` asks whether the final answer
names an expected ref. ``doc-*`` cases need S2
(documents) and are skipped.

Usage::

    python scripts/memory_s1_agent_loop.py --output report.json [--model M]

Calls the configured LLM (a few dozen small requests).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path

from scripts.memory_s0 import CASES, build_fixture

MAX_RECALLS = 3
SYSTEM = (
    "You are answering a question about your own past experience. Your memory "
    f"is reachable only through the recall tool, and you may call it at most "
    f"{MAX_RECALLS} times. When you are done, answer in one or two sentences and "
    "list the refs you relied on. If you found nothing, say so."
)


class _Facade:
    """The slice of MemoryFacade the recall tool uses."""

    def __init__(self, search):
        self._search = search

    async def search(self, query, *, kind="all", limit=5, domain=None, temporal=None,
                     since=None, until=None, mark_accessed=True):
        return await self._search.recall(
            query, type=kind, limit=limit, domain=domain, temporal=temporal,
            since=since, until=until, mark_accessed=mark_accessed,
        )

    async def mark_accessed(self, results):
        await self._search.mark_accessed(results)

    async def expand_ref(self, ref, mark_accessed=True):
        return await self._search.expand(ref, mark_accessed=mark_accessed)


_REF = re.compile(r"(?:log|mem|skill):[^\s，。、；;,)）\]」]+")


def _canonical(ref: str) -> str:
    """A ref's identity: no version suffix, no trailing punctuation."""
    ref = ref.rstrip(".:")
    if ref.startswith("mem:"):
        return re.sub(r"@[0-9a-f]{8}$", "", ref)
    if ref.startswith("skill:"):
        return re.sub(r"@v\d+$", "", ref)
    return ref


def _split_log(ref: str) -> tuple[str, str] | None:
    """``log:{session}:{turn}`` → (session, turn); sessions may hold colons."""
    if not ref.startswith("log:") or ":" not in ref[4:]:
        return None
    sid, turn = ref[4:].rsplit(":", 1)
    return (sid, turn) if turn.isdigit() else None


def _shown(outputs: list[str]) -> list[tuple[str, str]]:
    """(canonical ref, visible text) for everything the model was shown.

    Search output: one block per result, its header line naming the ref.
    ``log:`` expansion: one entry per turn section, keyed by that turn's ref
    within the expanded session. ``mem:``/``skill:`` read: the whole entry.
    """
    shown: list[tuple[str, str]] = []
    for out in outputs:
        head = out.split("\n", 1)[0]
        if head.startswith("log:") and " — turn " in head:
            parsed = _split_log(head.split(" ", 1)[0])
            if parsed is None:
                continue
            sid = parsed[0]
            for section in out.split("\n── turn ")[1:]:
                turn = re.match(r"\d+", section)
                if turn:
                    shown.append((f"log:{sid}:{turn[0]}", section))
            continue
        if head.startswith(("mem:", "skill:")):
            refs = _REF.findall(head)  # a mem:/skill: ref read: the whole entry
            if refs:
                shown.append((_canonical(refs[0]), out))
            continue
        for block in out.split("\n\n"):
            first = block.split("\n", 1)[0]
            if not first.startswith("["):
                continue  # coverage / budget / notice lines
            refs = _REF.findall(first)
            if refs:
                shown.append((_canonical(refs[-1]), block))
    return shown


def _visible_hit(expected: list[dict], outputs: list[str]) -> str | None:
    """How an expected source was seen with its evidence, or None."""
    for gold in expected:
        for ref, text in _shown(outputs):
            if ref == gold["ref"] and all(f in text for f in gold["contains"]):
                return "search" if text.startswith("[") else "ref read"
    return None


def _cited(answer: str, expected: list[dict]) -> bool:
    named = {_canonical(r) for r in _REF.findall(answer)}
    return any(g["ref"] in named for g in expected)


async def _run_case(case, router, model, tool, facade: _Facade) -> dict:
    from loom.core.harness.middleware import ToolCall
    from loom.core.harness.permissions import TrustLevel

    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": case["query"]}]
    schema = [tool.to_openai_schema()]  # the shape the session sends
    calls: list[dict] = []
    outputs: list[str] = []  # exactly what the model was sent, successful reads
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
                if ok:
                    outputs.append(content)
            messages.append(router.format_tool_result(model, tu.id, content, ok))
    elapsed = time.perf_counter() - t0

    via = _visible_hit(case["expected"], outputs)
    return {
        "id": case["id"], "query": case["query"], "recall_calls": len(calls),
        "calls": calls, "hit_within_budget": bool(via) if case["expected"] else None,
        "hit_via": via, "cited": _cited(answer, case["expected"]),
        "seconds": round(elapsed, 2), "answer": answer, "outputs": outputs,
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
