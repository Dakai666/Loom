"""Replay 30 days of attended GUARDED permission decisions through the
#704 S3 decision point in 自理 and report who would decide each one.

Reads ``~/.loom/ledger.db`` (decisions) and ``~/.loom/memory.db`` (the
session log, for each call's arguments) read-only. The workspace path
checks run against the live checkout given as the first argument
(default: the current directory). Nothing is executed and no model is
called: a call that would reach Luna is counted as "Luna", and the report
gives both the worst case (Luna abstains on all of them → DK is asked)
and an estimate from the measured false-abstain rate.

Attended means origin discord/cli, or a pre-origin row (#690 S3b) that a
human answered ("user confirmed/denied"); other pre-origin rows are
reported apart, as their attendance is unknown. Scope grants a human gave
during those sessions (leases) are not replayed: worst case.

    uv run python scripts/replay_attended_704_s3.py [/path/to/live/workspace]

``REPLAY_REASON_PREFIX=scope-allow`` replays only the rows that ran on a
grant (#704 S4: what the retired leases used to cover).
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loom.core.harness.approval import ApprovalClassifier, ReviewContext, ReviewTurn  # noqa: E402
from loom.core.harness.middleware import BlastRadiusMiddleware, ToolCall, ToolResult  # noqa: E402
from loom.core.harness.mode import PermissionMode  # noqa: E402
from loom.core.harness.permissions import PermissionContext, ToolCapability, TrustLevel  # noqa: E402
from loom.core.harness.registry import ToolDefinition, ToolRegistry  # noqa: E402
from loom.platform.cli import tools as T  # noqa: E402

DAYS = 30
ATTENDED_ORIGINS = {"discord", "cli", "interactive", "tui"}
PLATFORM_TOOLS = ["send_discord_file", "send_discord_embed", "send_discord_select",
                  "add_discord_reaction", "read_discord_forum"]


def resolvers(ws: Path) -> dict:
    return {
        "write_file": T._make_write_file_resolver(ws),
        "run_bash": T._make_run_bash_resolver(ws),
        "web_search": T._web_search_resolver,
        "fetch_url": T._fetch_url_resolver,
        "spawn_agent": T._spawn_agent_resolver,
        "image_generate": T._make_openai_image_scope_resolver(ws),
        "pursuit_write": T._make_pursuit_write_resolver(),
    }


def caps(name: str) -> ToolCapability:
    if name == "run_bash":
        return ToolCapability.EXEC
    if name == "spawn_agent":
        return ToolCapability.AGENT_SPAN
    return ToolCapability.NETWORK | ToolCapability.MUTATES


def load(days: int = DAYS):
    since = time.time() - days * 86400
    led = sqlite3.connect(f"file:{os.path.expanduser('~/.loom/ledger.db')}?mode=ro", uri=True)
    names = dict(led.execute(
        "SELECT json_extract(payload,'$.tool_call_id'), json_extract(payload,'$.tool_name') "
        "FROM events WHERE event_type='tool_lifecycle' AND timestamp > ?", (since - 3600,)))
    rows = []
    for p, turn in led.execute("SELECT payload, turn_id FROM events WHERE "
                               "event_type='permission_decision' AND timestamp > ?", (since,)):
        d = json.loads(p)
        d["tool"] = names.get(d.get("tool_call_id"))
        d["turn_id"] = turn
        rows.append(d)
    mem = sqlite3.connect(f"file:{os.path.expanduser('~/.loom/memory.db')}?mode=ro", uri=True)
    args = {}
    for (raw,) in mem.execute("SELECT raw_json FROM session_log WHERE role='assistant' "
                              "AND raw_json LIKE '%tool_calls%' AND created_at > datetime('now', ?)",
                              (f"-{days + 1} days",)):
        try:
            for tc in json.loads(raw).get("tool_calls") or []:
                fn = tc.get("function") or {}
                args[tc.get("id")] = json.loads(fn.get("arguments") or "{}")
        except (ValueError, AttributeError):
            continue
    # The turn's real input (#690 S3b stamps ledger_turn_id on messages).
    intents = {}
    for content, meta in mem.execute(
            "SELECT content, metadata FROM session_log WHERE role='user' AND metadata LIKE "
            "'%ledger_turn_id%' AND created_at > datetime('now', ?)", (f"-{days + 1} days",)):
        try:
            intents[json.loads(meta)["ledger_turn_id"]] = content
        except (ValueError, KeyError, TypeError):
            continue
    return rows, args, intents


def attendance(d: dict) -> str:
    origin = d.get("origin") or ""
    reason = d.get("reason") or ""
    if origin in ATTENDED_ORIGINS:
        return "attended"
    if not origin and reason.startswith(("user confirmed", "user denied")):
        return "attended"
    return "unattended" if origin else "unknown"


async def replay(d: dict, call_args: dict, ws: Path, res: dict, intent: str) -> str:
    events: list[str] = []
    luna_called: list[bool] = []

    async def confirm(_c):
        events.append("DK")
        return True

    async def luna(_m):
        luna_called.append(True)

        class R:
            text = '{"verdict":"abstain"}'
        return R()

    async def executor(c):
        return ToolResult(call_id=c.id, tool_name=c.tool_name, success=True, output="ok")

    name = d["tool"]
    reg = ToolRegistry()
    reg.register(ToolDefinition(name=name, description="", input_schema={}, executor=executor,
                                trust_level=TrustLevel.GUARDED, capabilities=caps(name),
                                scope_resolver=res.get(name)))
    perm = PermissionContext(session_id="replay", mode=PermissionMode.SELF)
    perm.declare("platform:discord", PLATFORM_TOOLS)

    class Budget:
        def unavailable(self): return False
        def record(self, _o): pass
        async def take(self, *_a, **_k): return True

    turn = ReviewTurn(intent=intent, origin="discord")
    class Emitter:
        async def emit(self, *_a, **_k): return None

    clf = ApprovalClassifier(workspace=ws, perm=perm, emitter=Emitter(), budget=Budget(), chat=luna,
                             context=lambda: ReviewContext(current=turn, previous=()))
    blast = BlastRadiusMiddleware(perm_ctx=perm, confirm_fn=confirm, registry=reg,
                                  classifier=clf, workspace=ws)
    reasons: list[str] = []
    blast._on_lifecycle_event = lambda _c, _ok, r: reasons.append(r)
    call = ToolCall(tool_name=name, args=dict(call_args), trust_level=TrustLevel.GUARDED,
                    capabilities=caps(name), session_id="replay", origin="discord")
    await blast.process(call, executor)
    note = call.metadata.get("classifier_note") or ""
    if events and not luna_called and os.environ.get("REPLAY_DETAIL"):
        print("ASK", name, note, str(call_args.get("command") or call_args.get("path") or "")[:90],
              "|| INTENT:", intent.replace("\n", " ")[:160])
    if luna_called:
        return "Luna"
    if events:
        return "asks DK (hard guardrail)" if note.startswith("hard") else "asks DK"
    return "no ask"


async def main() -> None:
    ws = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    rows, args, intents = load()
    res = resolvers(ws)
    table: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    before_after: collections.Counter = collections.Counter()
    skipped = collections.Counter()
    for d in rows:
        if d.get("trust_level") != "guarded" or not d.get("tool"):
            continue
        only = os.environ.get("REPLAY_REASON_PREFIX")
        if only and not (d.get("reason") or "").startswith(only):
            continue
        who = attendance(d)
        if who != "attended":
            skipped[who] += 1
            continue
        a = args.get(d.get("tool_call_id"))
        if a is None:
            table[d["tool"]]["no args in session log"] += 1
            continue
        intent = intents.get(d.get("turn_id"))
        if intent is None:
            table[d["tool"]]["no intent recorded"] += 1
            intent = ""
        outcome = await replay(d, a, ws, res, intent)
        table[d["tool"]][outcome] += 1
        # A row nobody was asked about then ran on a grant (a lease or an
        # "auto" a human gave earlier in that session); the replay starts
        # with no grants, so its "asks DK" is the grant not replayed.
        then = "asked DK then" if (d.get("reason") or "").startswith(("user confirmed", "user denied")) \
            else "grant then (not replayed)"
        before_after[(then, outcome.split(" (")[0])] += 1
    cols = ["no ask", "Luna", "asks DK", "asks DK (hard guardrail)", "no args in session log",
            "no intent recorded"]
    print("| 工具 | " + " | ".join(cols) + " |")
    print("|---|" + "---:|" * len(cols))
    total = collections.Counter()
    for tool in sorted(table):
        total.update(table[tool])
        print(f"| `{tool}` | " + " | ".join(str(table[tool][c]) for c in cols) + " |")
    print("| **合計** | " + " | ".join(f"**{total[c]}**" for c in cols) + " |")
    print(f"\nnot replayed: {dict(skipped)} (unattended, or pre-origin rows nobody answered)")
    print("\n| 當時 | S3 之後 | 次數 |\n|---|---|---:|")
    for (then, now), n in sorted(before_after.items()):
        print(f"| {then} | {now} | {n} |")
    print(f"Luna worst case (abstain on all): DK asked {total['Luna'] + total['asks DK'] + total['asks DK (hard guardrail)']} times")


if __name__ == "__main__":
    asyncio.run(main())
