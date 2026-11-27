"""#650 S3 PR-A: child experience survives without a semantic fact.

All sources, credentials and homes are synthetic. Episodic remains enabled.
"""
from __future__ import annotations

from loom.core.harness.permissions import PermissionContext

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from loom.core.agent.subagent import SubAgentConfig, SubAgentResult, run_subagent
from loom.core.cognition.providers import LLMResponse, ToolUse
from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.ledger import reset_turn_id, set_turn_id
from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.search import MemorySearch
from loom.core.memory.semantic import SemanticMemory
from loom.core.memory.session_log import SessionLog
from loom.core.memory.store import SQLiteStore


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    yield


def response(names=(), text="child answer"):
    uses = [ToolUse(id=f"call-{i}", name=name, args={}) for i, name in enumerate(names)]
    raw = {"role": "assistant", "content": text}
    if uses:
        raw["tool_calls"] = [{"id": u.id, "type": "function", "function": {
            "name": u.name, "arguments": "{}",
        }} for u in uses]
    return LLMResponse(text=text, tool_uses=uses,
                       stop_reason="tool_use" if uses else "end_turn", raw_message=raw)


class Router:
    def __init__(self, replies):
        self.replies = iter(replies)

    async def stream_chat(self, **kwargs):
        item = next(self.replies)
        if isinstance(item, BaseException):
            raise item
        yield "", item

    def format_tool_result(self, model, call_id, content, success=True):
        return {"role": "tool", "tool_call_id": call_id, "content": content}


def tools():
    registry = ToolRegistry()
    for name, failure in [("ok", None), ("fail", "execution_error"),
                          ("denied", "permission_denied"), ("late", "timeout")]:
        async def execute(call, failure=failure):
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=failure is None,
                              output="completed" if failure is None else None,
                              error=f"quartz_failure {failure}" if failure else None,
                              failure_type=failure)
        registry.register(ToolDefinition(name=name, description=name,
                          input_schema={"type": "object", "properties": {}},
                          trust_level=TrustLevel.SAFE, executor=execute))
    return registry


async def child(db, tmp_path, replies, *, agent="child", max_turns=10, registry=None):
    return await run_subagent(
        SubAgentConfig(task="inspect quartz_failure", model="test", agent_id=agent,
                       max_turns=max_turns),
        router=Router(replies), episodic=EpisodicMemory(db),
        semantic=SemanticMemory(db), procedural=ProceduralMemory(db),
        session_log=SessionLog(db), tool_registry=registry or tools(),
        parent_session_id="parent", workspace=tmp_path,
    )


async def rows(db, sid="parent:child"):
    cur = await db.execute(
        "SELECT turn_index,role,content,raw_json,metadata FROM session_log "
        "WHERE session_id=? ORDER BY id", (sid,),
    )
    return [(ti, role, content, raw, json.loads(meta))
            for ti, role, content, raw, meta in await cur.fetchall()]


async def test_child_failure_reopens_recall_and_colon_ref_without_facts(tmp_path):
    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    token = set_turn_id("turn_parent")
    first = response(["fail"], text="checking quartz")
    try:
        async with store.connect() as db:
            result = await child(db, tmp_path, [first, response()])
            assert result.success
            saved = await rows(db)
            assert [(r[0], r[1]) for r in saved] == [
                (1, "user"), (1, "assistant"), (1, "tool"), (2, "assistant"),
            ]
            assert json.loads(saved[1][3]) == first.raw_message
            for row in saved:
                assert row[4]["agent_id"] == "child"
                assert row[4]["parent_session_id"] == "parent"
                assert row[4]["ledger_turn_id"] == "turn_parent"
            meta = saved[2][4]
            assert meta["tool_call_id"] == "call-0"
            assert meta["tool_name"] == "fail"
            assert meta["success"] is False
            assert meta["failure_type"] == "execution_error"
            assert meta["duration_ms"] >= 0
            # PR-D freezes episodic; the child source remains in session_log.
            assert await EpisodicMemory(db).read_session("parent:child") == []
            assert saved[-1][0] == 2
            assert await SessionLog(db).get_session("parent:child") is None
    finally:
        reset_turn_id(token)
    async with store.connect() as db:
        assert await SemanticMemory(db).count() == 0
        cur = await db.execute("SELECT COUNT(*) FROM action_records")
        assert (await cur.fetchone())[0] == 0
        search = MemorySearch(SemanticMemory(db), ProceduralMemory(db))
        found = await search.recall("quartz_failure", mark_accessed=False)
        assert "log:parent:child:1" in [r.ref for r in found]
        expanded = await search.expand("log:parent:child:1", mark_accessed=False)
        assert expanded.found
        assert "inspect quartz_failure" in expanded.text
        assert "execution_error" in expanded.text
        assert "stored preview only — no action record" in expanded.text


@pytest.mark.parametrize("names", [
    ["ok", "fail", "missing", "denied", "late"],
    ["missing"],
])
async def test_each_tool_result_is_saved_once_including_unknown(tmp_path, names):
    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    async with store.connect() as db:
        await child(db, tmp_path, [response(names), response()])
        saved = [r for r in await rows(db) if r[1] == "tool"]
        assert [r[4]["tool_name"] for r in saved] == names
        assert len({r[4]["tool_call_id"] for r in saved}) == len(names)
        assert {r[0] for r in saved} == {1}
        expected = {"ok": None, "fail": "execution_error", "missing": "tool_not_found",
                    "denied": "permission_denied", "late": "timeout"}
        for row in saved:
            name = row[4]["tool_name"]
            assert row[4]["success"] == (name == "ok")
            assert row[4].get("failure_type") == expected[name]
            assert row[4]["duration_ms"] >= 0
        assert await EpisodicMemory(db).read_session("parent:child") == []


@pytest.mark.parametrize("exit_kind", ["max_turns", "exception", "cancel"])
async def test_child_finalizes_metadata_and_completed_rows_on_every_exit(tmp_path, exit_kind):
    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    async with store.connect() as db:
        if exit_kind == "max_turns":
            result = await child(db, tmp_path, [response(["fail"])], max_turns=1)
            assert not result.success
        elif exit_kind == "exception":
            # #648 P4 review F2: a model-call exception is a failure result,
            # not a raise; the rows are finalized the same way.
            result = await child(db, tmp_path, [response(["fail"]), RuntimeError("provider failed")])
            assert not result.success and "RuntimeError" in result.error
        else:
            with pytest.raises(asyncio.CancelledError):
                await child(db, tmp_path, [response(["fail"]), asyncio.CancelledError()])
        saved = await rows(db)
        assert [r[1] for r in saved] == ["user", "assistant", "tool"]
        assert {r[0] for r in saved} == {1}
        assert all(r[4]["agent_id"] == "child" for r in saved)
        assert await SessionLog(db).get_session("parent:child") is None


async def test_child_scope_time_limit_redaction_and_tool_heavy_history(tmp_path):
    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    async with store.connect() as db:
        log = SessionLog(db)
        await log.create_session("parent", "test")
        await log.log_message("parent", 1, "user", "parent-only")
        # Many rows exercise turn grouping and explicit limits without a new reader.
        await child(db, tmp_path, [response(["ok"] * 110), response()], agent="sibling")
        secret = 'password="synthetic-secret-12345678"'
        await child(db, tmp_path, [response(text=secret)])
        since = datetime.now(UTC) - timedelta(minutes=1)
        scoped = await log.messages_between(since, session_id="parent:child", limit=1)
        assert len(scoped) == 1 and scoped[0]["session_id"] == "parent:child"
        assert await log.messages_between(datetime.now(UTC) + timedelta(days=1),
                                          session_id="parent:child") == []
        loaded = await log.load_messages("parent:child")
        assert "synthetic-secret-12345678" not in json.dumps(loaded)
        expanded = await MemorySearch(SemanticMemory(db), ProceduralMemory(db)).expand(
            "log:parent:child:1", mark_accessed=False)
        assert expanded.found and "[REDACTED]" in expanded.text
        assert "synthetic-secret" not in expanded.text
        assert "sibling" not in expanded.text and "parent-only" not in expanded.text
        sibling_rows = await rows(db, "parent:sibling")
        assert len([r for r in sibling_rows if r[1] == "tool"]) == 110


async def test_spawn_agent_injects_the_instance_session_log(tmp_path, monkeypatch):
    import loom.core.agent.subagent as module
    from loom.platform.cli.tools import make_spawn_agent_tool
    captured = {}

    async def run(config, **kwargs):
        captured.update(kwargs)
        return SubAgentResult(success=True, output="done", agent_id="child", turns_used=1, tool_calls=0)

    monkeypatch.setattr(module, "run_subagent", run)
    handle = object()
    parent = SimpleNamespace(
        model="test", router=object(), registry=ToolRegistry(), session_id="parent",
        workspace=tmp_path, perm=PermissionContext(session_id="parent"),
        _memory=SimpleNamespace(episodic=object(), semantic=object(), procedural=object(), session_log=handle),
    )
    result = await make_spawn_agent_tool(parent).executor(
        ToolCall(id="spawn", tool_name="spawn_agent", args={"task": "inspect"},
                 trust_level=TrustLevel.GUARDED, session_id="parent"))
    assert result.success
    assert captured["session_log"] is handle


@pytest.mark.parametrize("parallel", [False, True])
async def test_parent_outcomes_persist_in_both_dispatch_paths(tmp_path, monkeypatch, parallel):
    from loom.core import session as module
    from loom.core.session import LoomSession
    from rich.prompt import Confirm

    router = Router([response(["ok", "fail", "missing"]), response()])
    router.provider_instances = lambda: []
    router.native_max_tokens = lambda model: None
    router.get_provider = MagicMock()
    router.get_provider.return_value.native_context_window.return_value = 128_000
    monkeypatch.setattr(module, "build_router", lambda *a, **k: router)
    monkeypatch.setattr(module, "_load_loom_config", lambda: {})
    monkeypatch.setattr(module, "_load_env", lambda *a, **k: {})
    monkeypatch.setattr(module, "build_embedding_provider", lambda *a, **k: None)
    monkeypatch.setattr(Confirm, "ask", lambda *a, **k: True)
    session = LoomSession(model="test", db_path=str(tmp_path / "memory.db"), workspace=tmp_path)
    await session.start()
    session._judge_mode = "off"
    session._all_authorized = lambda uses: parallel
    for tool in tools()._tools.values():
        session.registry.register(tool)
    try:
        from loom.core.events import ToolEnd
        durations = {}
        async for event in session.stream_turn("inspect outcomes"):
            if isinstance(event, ToolEnd):
                durations[event.call_id] = event.duration_ms
        # Session's pre-existing log scheduling is fire-and-forget.
        await asyncio.sleep(0.05)
        saved = [r for r in await rows(session._db, session.session_id) if r[1] == "tool"]
        assert [r[4]["tool_name"] for r in saved] == ["ok", "fail", "missing"]
        for row, failure in zip(saved, [None, "execution_error", "tool_not_found"]):
            assert row[4]["success"] == (failure is None)
            assert row[4].get("failure_type") == failure
            assert row[4]["duration_ms"] == durations[row[4]["tool_call_id"]]
            assert row[4]["tool_call_id"].startswith("call-")
            assert row[4]["ledger_turn_id"]
    finally:
        await session.stop()


@pytest.mark.parametrize("content", ["canonical-only-text", [{"type": "text", "text": "canonical-only-text"}]])
async def test_assistant_text_remains_searchable_when_response_text_is_absent(tmp_path, content):
    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    reply = LLMResponse(text=None, tool_uses=[], stop_reason="end_turn",
                        raw_message={"role": "assistant", "content": content})
    async with store.connect() as db:
        await child(db, tmp_path, [reply])
        saved = await rows(db)
        assert saved[1][2] == "canonical-only-text"
        assert json.loads(saved[1][3]) == reply.raw_message
        found = await MemorySearch(SemanticMemory(db), ProceduralMemory(db)).recall(
            "canonical-only-text", mark_accessed=False)
        assert [r.ref for r in found] == ["log:parent:child:1"]


async def test_task_cancellation_flushes_already_completed_child_trace(tmp_path, monkeypatch):
    waiting_for_model = asyncio.Event()
    original = Router.stream_chat
    calls = 0

    async def stream(self, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            async for item in original(self, **kwargs):
                yield item
        else:
            waiting_for_model.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(Router, "stream_chat", stream)
    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    async with store.connect() as db:
        task = asyncio.create_task(child(db, tmp_path, [response(["fail"])]))
        try:
            await asyncio.wait_for(waiting_for_model.wait(), timeout=2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert [r[1] for r in await rows(db)] == ["user", "assistant", "tool"]
        assert await SessionLog(db).get_session("parent:child") is None


async def test_child_never_becomes_the_resume_or_sessions_listing_target(tmp_path):
    """F1: a newer child must not replace the crashed parent's resume target."""
    from loom.core.memory.pulse import MemoryPulse

    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    async with store.connect() as db:
        log = SessionLog(db)
        await log.create_session("parent", "test")
        await log.update_session("parent", 1, "2000-01-01T00:00:00+00:00", "main conversation")
        await child(db, tmp_path, [response(["fail"]), response()])
        assert [s["session_id"] for s in await log.list_sessions(limit=1)] == ["parent"]
        assert [s["session_id"] for s in await log.list_sessions(limit=20)] == ["parent"]
        pulse = MemoryPulse(db=db, semantic=SemanticMemory(db), session_id="next-main",
                            session_started_at=datetime.now(UTC), pending_buffer=[])
        previous = await pulse._previous_active_session()
        assert previous is not None and previous[0] == "parent"
        # No listing entry is needed to expand the durable child source.
        expanded = await MemorySearch(SemanticMemory(db), ProceduralMemory(db)).expand(
            "log:parent:child:1", mark_accessed=False)
        assert expanded.found and "execution_error" in expanded.text


@pytest.mark.parametrize("parent,sibling", [
    ("abc", "abcd"),
    ("abc", "ABC"),
    ("a%_!\\b", "aZZx!\\b"),
])
async def test_delete_parent_cascades_to_child_sources_and_indexes_only(tmp_path, parent, sibling):
    """F2: include log-only descendants; treat wildcard characters as literal ids."""
    from loom.core.memory.recall_index import sync_terms

    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    async with store.connect() as db:
        log = SessionLog(db)
        owned = [parent, f"{parent}:child", f"{parent}:child:grandchild"]
        kept = [sibling, f"{sibling}:child"]
        for sid in owned + kept:
            # Grandchild intentionally has log rows but no sessions row.
            if sid != owned[-1]:
                await log.create_session(sid, "test")
            await log.log_message(sid, 1, "user", "旅行 cascade_probe")
        await sync_terms(db)
        cur = await db.execute("SELECT id FROM session_log WHERE session_id IN (?,?,?)", owned)
        owned_rowids = [r[0] for r in await cur.fetchall()]
        cur = await db.execute("SELECT id FROM session_log WHERE session_id IN (?,?)", kept)
        kept_rowids = [r[0] for r in await cur.fetchall()]
        await log.delete_session(parent)
        cur = await db.execute("SELECT DISTINCT session_id FROM session_log ORDER BY session_id")
        assert [r[0] for r in await cur.fetchall()] == sorted(kept)
        cur = await db.execute("SELECT session_id FROM sessions ORDER BY session_id")
        assert [r[0] for r in await cur.fetchall()] == sorted(kept)
        cur = await db.execute("SELECT rowid FROM session_log_terms ORDER BY rowid")
        indexed = {r[0] for r in await cur.fetchall()}
        assert not indexed.intersection(owned_rowids)
        assert indexed.issuperset(kept_rowids)
        search = MemorySearch(SemanticMemory(db), ProceduralMemory(db))
        for query in ["旅行", "cascade_probe"]:
            assert {r.ref for r in await search.recall(query, limit=20, mark_accessed=False)} == {
                f"log:{sid}:1" for sid in kept
            }
        for sid in owned:
            assert not (await search.expand(f"log:{sid}:1", mark_accessed=False)).found
