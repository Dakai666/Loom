"""#650 S3 PR-D: retire producers, preserve sources and context management."""
import asyncio
import importlib.util
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from loom.core.cognition.providers import LLMResponse, ToolUse
from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolDefinition
from loom.core.infra.telemetry import AgentTelemetryTracker
from loom.core.memory.episodic import EpisodicMemory, EpisodicEntry
from loom.core.memory.procedural import SkillGenome
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.store import SQLiteStore
from loom.core.session import LoomSession
from loom.platform.cli.tools import make_agent_health_tool


@pytest_asyncio.fixture
async def session(tmp_path, monkeypatch, request):
    import loom
    from loom.core import session as module
    from rich.prompt import Confirm
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    registry = loom._get_default_registry()
    original = dict(registry._tools)
    registry._tools.clear()
    cfg = {"memory": {"compress_session": True, "episodic_compress_threshold": 1,
                      "episodic_retention_days": 1, "governance": {"episodic_ttl_days": 1}},
           "telemetry": {"dimensions": ["tool_call", "memory_compression"]}}
    router = MagicMock()
    router.provider_instances.return_value = []
    router.get_provider.return_value.native_context_window.return_value = 128_000
    router.native_max_tokens = lambda model: None
    router.chat = AsyncMock(return_value=LLMResponse(text="FACT: unwanted extraction", tool_uses=[], stop_reason="end_turn"))
    router.format_tool_result = lambda model, cid, content, success=True: {
        "role": "tool", "tool_call_id": cid, "content": content}
    step = 0
    async def stream_chat(**kwargs):
        nonlocal step
        step += 1
        uses = [ToolUse(id="failed-call", name="tracked_failure", args={})] if step == 1 else []
        yield "", LLMResponse(text="attempt" if uses else "done", tool_uses=uses,
            stop_reason="tool_use" if uses else "end_turn", raw_message={"role": "assistant",
            "content": "attempt" if uses else "done", **({"tool_calls": [{"id": "failed-call", "type": "function",
                "function": {"name": "tracked_failure", "arguments": "{}"}}]} if uses else {})})
    router.stream_chat = stream_chat
    monkeypatch.setattr(module, "build_router", lambda *a, **k: router)
    monkeypatch.setattr(module, "_load_loom_config", lambda: cfg)
    monkeypatch.setattr(module, "_load_env", lambda *a, **k: {})
    monkeypatch.setattr(module, "build_embedding_provider", lambda *a, **k: None)
    monkeypatch.setattr(Confirm, "ask", lambda *a, **k: True)
    db_path = tmp_path / "memory.db"
    # Seed a legacy DB before startup (the False case is a truly empty DB).
    if getattr(request.node, "callspec", None) and request.node.callspec.params.get("legacy", False):
        store = SQLiteStore(str(db_path))
        await store.initialize()
        async with store.connect() as db:
            await EpisodicMemory(db).write(EpisodicEntry(id="legacy-episode", session_id="old-child",
                event_type="tool_result", content="legacy failure", metadata={"success": False},
                created_at=datetime(2000, 1, 1, tzinfo=UTC)))
    s = LoomSession(model="test", db_path=str(db_path), workspace=tmp_path)
    await s.start()
    s._judge_mode = "off"
    async def passthrough(view, **kwargs):
        return view
    s._author_envelope_outcome_summary = passthrough
    async def fail(call):
        return ToolResult(call_id=call.id, tool_name=call.tool_name, success=False,
                          error="synthetic failure", failure_type="execution_error")
    s.registry.register(ToolDefinition(name="tracked_failure", description="fail", trust_level=TrustLevel.SAFE,
                                      input_schema={"type": "object", "properties": {}}, executor=fail))
    await s._memory.procedural.upsert(SkillGenome(name="tracked_failure", body="synthetic"))
    try:
        yield s, router
    finally:
        await s.stop()
        registry._tools.clear()
        registry._tools.update(original)


@pytest.mark.parametrize("legacy", [False, True])
async def test_turn_failure_shutdown_preserve_episodic_and_raw_sources(session, legacy):
    s, router = session
    db = s._db
    # Session facts/stubs are historical data, not cleanup targets.
    await s._memory.semantic.upsert(SemanticEntry(key="old:fact", value="historical fact", source="session:old"))
    await s._memory.semantic.upsert(SemanticEntry(key="old:stub", value="redirect", source="session:old",
                                               metadata={"redirected_to": "old:fact"}))
    cur = await db.execute("SELECT * FROM episodic_entries ORDER BY id")
    before = await cur.fetchall()
    assert len(before) == int(legacy)
    async for _ in s.stream_turn("try the tracked tool"):
        pass
    await asyncio.sleep(0.05)
    # All turn_end tasks had a chance to run. No extractor or counterfactual.
    await s.stop()
    router.chat.assert_not_awaited()
    async with s._store.connect() as reopened:
        cur = await reopened.execute("SELECT * FROM episodic_entries ORDER BY id")
        assert await cur.fetchall() == before
        cur = await reopened.execute("SELECT COUNT(*) FROM session_log WHERE session_id=?", (s.session_id,))
        assert (await cur.fetchone())[0] >= 3
        cur = await reopened.execute("SELECT COUNT(*) FROM action_records WHERE session_id=?", (s.session_id,))
        assert (await cur.fetchone())[0] == 1
        assert await SemanticMemory(reopened).get("old:fact") is not None
        assert (await SemanticMemory(reopened).get("old:stub")).metadata["redirected_to"] == "old:fact"
        cur = await reopened.execute("SELECT COUNT(*) FROM semantic_entries WHERE source LIKE 'counter_factual:%'")
        assert (await cur.fetchone())[0] == 0
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith(("memory_compaction:", "cfr:"))]


async def test_subagent_no_longer_writes_episodic(session, tmp_path):
    from loom.core.agent.subagent import SubAgentConfig, run_subagent
    s, router = session
    # The child loop uses the same response shape and fails once.
    await run_subagent(SubAgentConfig(task="try", model="test", agent_id="child"),
        router=router, episodic=s._memory.episodic, semantic=s._memory.semantic,
        procedural=s._memory.procedural, tool_registry=s.registry, parent_session_id=s.session_id,
        workspace=tmp_path, session_log=s._memory.session_log)
    assert await s._memory.episodic.read_session(f"{s.session_id}:child") == []
    assert await s._memory.session_log.read_trace(f"{s.session_id}:child")
    router.chat.assert_not_awaited()


async def test_legacy_config_cannot_revive_producers_and_notes_migration(session, caplog):
    s, _ = session
    notes = [r.message for r in caplog.get_records("setup") if "retired memory producer settings ignored" in r.message]
    assert len(notes) == 1 and "memory.compress_session" in notes[0]
    for name in ("_compact_memory", "force_compact", "_compaction_subscriber_loop", "_compaction_task", "_compaction_lock", "_pending_compactions"):
        assert not hasattr(s, name)
    assert s._telemetry.get("memory_compression") is None


def test_retired_public_surfaces_are_gone():
    from loom.core import session as module, events
    from loom.platform.cli import main
    assert not hasattr(module, "compress_session")
    assert not hasattr(main, "compress_session")
    assert not hasattr(events, "CompressDone")
    assert importlib.util.find_spec("loom.core.cognition.counter_factual") is None


async def test_context_compaction_remains_available(session, monkeypatch):
    s, router = session
    s.messages = [s.messages[0]] + [m for i in range(4) for m in (
        {"role": "user", "content": f"question {i}"}, {"role": "assistant", "content": "answer"})]
    router.chat.return_value = LLMResponse(text="context summary", tool_uses=[], stop_reason="end_turn")
    # Exercise the real Discord /compact dispatch, not only method presence.
    from loom.platform.discord import bot as discord_module
    bot = discord_module.LoomDiscordBot(model="test", db_path="synthetic-unused.db")
    send = AsyncMock(return_value=MagicMock())
    edit = AsyncMock()
    monkeypatch.setattr(discord_module, "_safe_send", send)
    monkeypatch.setattr(discord_module, "_safe_edit", edit)
    await bot._handle_slash(MagicMock(), "/compact", s, is_thread=True)
    router.chat.assert_awaited_once()
    edit.assert_awaited_once_with(send.return_value, "✅ Context compacted.")
    assert any("context summary" in str(m["content"]) for m in s.messages)


async def test_legacy_telemetry_readable_but_never_written(session):
    s, _ = session
    db = s._db
    tracker = AgentTelemetryTracker(db, "fresh", dimensions=("tool_call", "memory_compression"))
    await tracker.ensure_table()
    await db.execute("INSERT INTO agent_telemetry(dimension,session_id,payload,updated_at) VALUES(?,?,?,?)",
                     ("memory_compression", "old", '{"runs":3,"facts_total":2}', "2000-01-01"))
    await db.commit()
    assert tracker.get("memory_compression") is None
    tracker.get("tool_call").record("fail", success=False, duration_ms=1.0)
    tracker.mark_dirty()
    await tracker.flush()
    cur = await db.execute("SELECT session_id,payload FROM agent_telemetry WHERE dimension='memory_compression'")
    assert await cur.fetchall() == [("old", '{"runs":3,"facts_total":2}')]
    result = await make_agent_health_tool(tracker).executor(ToolCall(tool_name="agent_health", session_id="s",
        trust_level=TrustLevel.SAFE, args={"dimension": "memory_compression"}))
    assert "legacy" in result.output.lower() and "facts_total" in result.output
    assert tracker.get("tool_call").snapshot()["tools"]["fail"]["failure"] == 1


async def test_discord_resume_no_extraction_and_context_command_kept(session, monkeypatch):
    from loom.platform.discord.bot import LoomDiscordBot
    from loom.core import session as module
    s, _ = session
    bot = LoomDiscordBot(model="test", db_path="synthetic-unused.db")
    assert not hasattr(bot, "_daily_compaction_loop")
    assert not hasattr(bot, "_run_daily_compaction")
    bot._thread_map = {"123": "existing"}
    bot._save_thread_map = MagicMock()
    bot._client.get_channel = MagicMock(return_value=None)
    fake = MagicMock()
    fake.session_id = "existing"
    fake._pipeline._middlewares = []
    fake._loom_config = {"task_write": {"discord_reminder": False}}
    fake.start = AsyncMock()
    fake.force_compact = AsyncMock(side_effect=AssertionError("retired"))
    monkeypatch.setattr(module, "LoomSession", MagicMock(return_value=fake))
    await bot._start_session(123)
    fake.force_compact.assert_not_awaited()
    assert callable(s._smart_compact)


@pytest.mark.parametrize("profile", ["loom.toml.example", "examples/profiles/loom.toml.example.assistant",
    "examples/profiles/loom.toml.example.autonomous", "examples/profiles/loom.toml.example.secure"])
def test_example_configs_do_not_advertise_retired_producers(profile):
    from pathlib import Path
    import tomllib
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / profile).read_text())
    memory = config.get("memory", {})
    assert not set(memory) & {"compress_session", "episodic_compress_threshold", "episodic_retention_days"}
    assert "episodic_ttl_days" not in memory.get("governance", {})
    assert "memory_compression" not in config.get("telemetry", {}).get("dimensions", [])
