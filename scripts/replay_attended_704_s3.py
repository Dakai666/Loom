"""Replay the last 30 days of permission decisions for Loom Agent's everyday
tools (memorize, image_generate, *__understand_image) through the #704 S3
decision point in 自理, and show who would decide each one.

Reads ``~/.loom/ledger.db`` read-only. The ledger keeps no call args, so
each row is replayed with representative args for its tool (an image into
``outputs/``); the decision for these tools does not depend on the text.
A row is treated as attended unless its origin says chime/autonomy/subagent;
rows from before origin was recorded (#690 S3b) count as attended, the
conservative reading. No model is called: a replay that would reach Luna
is reported as such.

    uv run python scripts/replay_attended_704_s3.py
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loom.core.harness.approval import ApprovalClassifier, ReviewContext, ReviewTurn  # noqa: E402
from loom.core.harness.middleware import BlastRadiusMiddleware, ToolCall, ToolResult  # noqa: E402
from loom.core.harness.mode import PermissionMode  # noqa: E402
from loom.core.harness.permissions import PermissionContext, ToolCapability, TrustLevel  # noqa: E402
from loom.core.harness.registry import ToolDefinition, ToolRegistry  # noqa: E402
from loom.platform.cli.tools import _make_openai_image_scope_resolver  # noqa: E402

TOOLS = ("memorize", "image_generate", "minimax_coding__understand_image")
ARGS = {
    "memorize": {"key": "k", "value": "v"},
    "image_generate": {"prompt": "x", "output_path": "outputs/x.png"},
    "minimax_coding__understand_image": {"image_source": "outputs/x.png", "prompt": "這是什麼"},
}
# What the 30-day rows' routines declare (autonomy/circadian/rhythm.toml).
DECLARED_BY_ROUTINES = {"memorize"}


def rows(days: int = 30):
    db = sqlite3.connect(f"file:{os.path.expanduser('~/.loom/ledger.db')}?mode=ro", uri=True)
    since = time.time() - days * 86400
    names = dict(db.execute(
        "SELECT json_extract(payload,'$.tool_call_id'), json_extract(payload,'$.tool_name') "
        "FROM events WHERE event_type='tool_lifecycle' AND timestamp > ?", (since - 3600,)))
    for (p,) in db.execute("SELECT payload FROM events WHERE event_type='permission_decision' "
                           "AND timestamp > ?", (since,)):
        d = json.loads(p)
        d["tool"] = names.get(d.get("tool_call_id"))
        if d["tool"] in TOOLS:
            yield d


async def replay(tool: str, origin: str, ws: Path, *, declared: bool) -> str:
    asked: list[str] = []
    decided: list[str] = []

    async def confirm(_call):
        asked.append("DK")
        return True

    async def luna(_messages):
        asked.append("Luna")

        class R:
            text = '{"verdict":"abstain"}'
        return R()

    async def executor(call):
        return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True, output="ok")

    reg = ToolRegistry()
    caps = ToolCapability.NETWORK | (ToolCapability.MUTATES if tool != "minimax_coding__understand_image" else ToolCapability.NONE)
    reg.register(ToolDefinition(
        name=tool, description="", input_schema={}, executor=executor, trust_level=TrustLevel.GUARDED,
        capabilities=caps,
        scope_resolver=_make_openai_image_scope_resolver(ws) if tool == "image_generate" else None))
    perm = PermissionContext(session_id="replay", mode=PermissionMode.SELF)
    if origin != "attended" and declared and tool in DECLARED_BY_ROUTINES:
        perm.declare("autonomy:routine", [tool], unattended_only=True)
    turn = ReviewTurn(intent="（重播）", origin=origin)

    class _Budget:
        def unavailable(self): return False
        def record(self, _o): pass
        async def take(self, *_a, **_k): return True

    classifier = ApprovalClassifier(workspace=ws, perm=perm, emitter=object(), budget=_Budget(),
                                    chat=luna, context=lambda: ReviewContext(current=turn, previous=()))
    blast = BlastRadiusMiddleware(perm_ctx=perm, confirm_fn=confirm, registry=reg,
                                  classifier=classifier, workspace=ws)
    blast._on_lifecycle_event = lambda _c, _ok, reason: decided.append(reason)
    call = ToolCall(tool_name=tool, args=dict(ARGS[tool]), trust_level=TrustLevel.GUARDED,
                    capabilities=caps, session_id="replay",
                    origin="discord" if origin == "attended" else origin)
    result = await blast.process(call, executor)
    if asked:
        return "+".join(asked)
    if result.metadata.get("unattended_skip"):
        return "skip"
    return f"不問：{decided[-1]}"


async def main() -> None:
    table = collections.Counter()
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        for d in rows():
            origin = d.get("origin") or ""
            then = d.get("reason") or ""
            kind = origin if origin in ("chime", "autonomy", "subagent") else "attended"
            if kind == "attended" and then.startswith("unattended-"):
                kind = then.split("(", 1)[1].split(",", 1)[0]  # a row from before S3b
            # A row that was skipped had no declaration then; replay it so.
            now = await replay(d["tool"], kind, ws, declared=not then.startswith("unattended-"))
            table[(d["tool"], kind, then, now)] += 1
    print("| 工具 | 來源 | 當時 | 次數 | S3 之後（自理） |")
    print("|---|---|---|---:|---|")
    for (tool, kind, then, now), n in sorted(table.items()):
        print(f"| `{tool}` | {kind} | {then} | {n} | {now} |")


if __name__ == "__main__":
    asyncio.run(main())
