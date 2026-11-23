"""tool_lifecycle END contract — every BEGIN gets an END (#690 S3a).

Ledger evidence: forum post / forum read calls logged BEGIN with no END.
Their ToolResult.output is a dict; the END payload builder assumed a
string (``output + "|"`` in the digest, ``output[:200]`` in the summary),
raised TypeError, and the emit's except swallowed it.

Invariants asserted here:
- one BEGIN and one END per call, whatever the output shape or outcome
- END's result_summary is the text the model sees (``str(output)``),
  capped at 200 chars
- result_digest tracks that same text

Only external I/O is stubbed (the Discord client). The ledger is real.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio

from loom.core.harness.middleware import (
    LifecycleGateMiddleware,
    LifecycleMiddleware,
    MiddlewarePipeline,
    ToolCall,
    ToolResult,
)
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.ledger import (
    LedgerEmitter,
    LedgerStore,
    async_correlation_scope,
    async_turn_scope,
)
from loom.platform.discord.tools import (
    make_create_discord_forum_post_tool,
    make_read_discord_forum_tool,
)

TURN = "turn_s3a"


@pytest_asyncio.fixture
async def ledger(tmp_path: Path):
    s = LedgerStore(db_path=tmp_path / "ledger.db", blob_dir=tmp_path / "blobs")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
def emitter(ledger: LedgerStore) -> LedgerEmitter:
    return LedgerEmitter(ledger, session_id="sess_s3a")


async def _run(tool: ToolDefinition, emitter: LedgerEmitter, args: dict) -> ToolResult:
    reg = ToolRegistry()
    reg.register(tool)
    # Outer + inner lifecycle layers, as session.py and subagent.py wire them.
    pipeline = MiddlewarePipeline([
        LifecycleMiddleware(registry=reg, ledger_emitter=emitter),
        LifecycleGateMiddleware(registry=reg),
    ])
    call = ToolCall(
        tool_name=tool.name, args=args,
        trust_level=TrustLevel.SAFE, session_id="sess_s3a",
    )
    async with async_turn_scope(TURN), async_correlation_scope("c1"):
        return await pipeline.execute(call, tool.executor)


async def _lifecycle(ledger: LedgerStore) -> tuple[list[dict], list[dict]]:
    rows = [r for r in await ledger.fetch_by_turn(TURN) if r.event_type == "tool_lifecycle"]
    begins = [r.payload for r in rows if r.payload["phase"] == "BEGIN"]
    ends = [r.payload for r in rows if r.payload["phase"] == "END"]
    return begins, ends


def _tool(name: str, result: Any) -> ToolDefinition:
    """A tool that returns ``result`` (a ToolResult factory) or raises it."""
    async def executor(call: ToolCall) -> ToolResult:
        if isinstance(result, BaseException):
            raise result
        return result(call)

    return ToolDefinition(
        name=name, description="s3a probe", trust_level=TrustLevel.SAFE,
        input_schema={"type": "object"}, executor=executor,
    )


def _ok(output: Any):
    return lambda call: ToolResult(call.id, call.tool_name, True, output=output)


# ---------------------------------------------------------------------------
# Every BEGIN gets an END — across output shapes and outcomes
# ---------------------------------------------------------------------------

_LONG = {"posts": [{"title": "x" * 50, "thread_id": i} for i in range(20)]}

CASES = {
    "str": _ok("plain text"),
    "dict": _ok({"thread_id": 999, "title": "晨報"}),
    "long-dict": _ok(_LONG),
    "list": _ok([1, 2, 3]),
    "none": _ok(None),
    "failure": lambda call: ToolResult(
        call.id, call.tool_name, False, output={"partial": True}, error="boom",
    ),
    "raises": RuntimeError("executor blew up"),
}


@pytest.mark.parametrize("case", list(CASES))
async def test_every_begin_gets_an_end(case, ledger, emitter) -> None:
    await _run(_tool("probe", CASES[case]), emitter, {"v": 1})

    begins, ends = await _lifecycle(ledger)
    assert len(begins) == 1
    assert len(ends) == 1, f"{case}: BEGIN logged without END"
    assert ends[0]["tool_call_id"] == begins[0]["tool_call_id"]


@pytest.mark.parametrize("output", [
    {"thread_id": 999, "title": "晨報"},
    _LONG,
    [1, 2, 3],
    "plain text",
])
async def test_summary_is_the_text_the_model_sees(output, ledger, emitter) -> None:
    await _run(_tool("probe", _ok(output)), emitter, {})

    _, (end,) = await _lifecycle(ledger)
    # The model receives str(output) (session.py); the ledger summary
    # must describe that same text, not a different serialization.
    assert end["result_summary"] == str(output)[:200]
    assert end["error"] is None


async def test_digest_tracks_structured_output(ledger, emitter) -> None:
    await _run(_tool("a", _ok({"thread_id": 1})), emitter, {})
    await _run(_tool("b", _ok({"thread_id": 1})), emitter, {})
    await _run(_tool("c", _ok({"thread_id": 2})), emitter, {})

    _, ends = await _lifecycle(ledger)
    digests = {e["tool_name"]: e["result_digest"] for e in ends}
    assert digests["a"] == digests["b"]
    assert digests["a"] != digests["c"]
    assert digests["a"].startswith("sha256:")


# ---------------------------------------------------------------------------
# The real forum tools (Discord client stubbed)
# ---------------------------------------------------------------------------


async def test_forum_post_logs_end(tmp_path: Path, ledger, emitter) -> None:
    thread = MagicMock(id=999, jump_url="https://discord.com/channels/1/2/999")
    thread.send = AsyncMock()
    forum = MagicMock(spec=discord.ForumChannel)
    forum.available_tags = []
    forum.create_thread = AsyncMock(return_value=MagicMock(thread=thread))
    client = MagicMock()
    client.get_channel = MagicMock(return_value=forum)

    tool = make_create_discord_forum_post_tool(client, tmp_path, default_forum_id=1515)
    result = await _run(tool, emitter, {"title": "晨報", "content": "今天的新聞"})

    assert result.success, result.error
    _, ends = await _lifecycle(ledger)
    assert len(ends) == 1
    assert ends[0]["result_summary"] == str(result.output)[:200]


async def test_forum_read_logs_end(ledger, emitter) -> None:
    thread = MagicMock(spec=discord.Thread)
    thread.configure_mock(
        id=7, name="晨報", jump_url="https://discord.com/channels/1/2/7",
        created_at=None, archived=False,
    )
    forum = MagicMock(spec=discord.ForumChannel)
    forum.id = 1515
    forum.threads = [thread]

    async def no_archived(*a, **kw):
        return
        yield  # pragma: no cover

    forum.archived_threads = no_archived
    client = MagicMock()
    client.get_channel = MagicMock(return_value=forum)

    tool = make_read_discord_forum_tool(client, default_forum_id=1515)
    result = await _run(tool, emitter, {})

    assert result.success, result.error
    assert isinstance(result.output, dict)
    _, ends = await _lifecycle(ledger)
    assert len(ends) == 1
    assert ends[0]["result_summary"] == str(result.output)[:200]
