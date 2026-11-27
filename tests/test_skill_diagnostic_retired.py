"""Retirement contract for #675; all sessions and stores are synthetic."""

import asyncio
import importlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from loom.core.cognition.providers import LLMResponse, ToolUse
from loom.core.events import TurnDone, TurnDropped
from loom.core.memory.procedural import SkillGenome
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.store import SQLiteStore
from loom.core.session import LoomSession


@pytest.mark.parametrize("module", [
    "loom.core.cognition.task_reflector", "loom.core.cognition.self_reflection",
    "loom.core.cognition.counter_factual",
])
def test_retired_modules_are_gone(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_retired_public_surfaces_are_gone():
    from loom.autonomy import __all__ as autonomy_exports
    from loom.platform.cli.main import cli

    assert "run_self_reflection" not in autonomy_exports
    assert not hasattr(LoomSession, "subscribe_diagnostic")
    assert "diagnostic" not in cli.commands


async def _session(monkeypatch, tmp_path, mode):
    from loom.core import session as core_session
    from loom.core.config import load_loom_config
    from rich.prompt import Confirm

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    (workspace / "loom.toml").write_text(
        '[reflection]\nauto_reflect = true\nvisibility = "verbose"\n'
        '[cognition.tiers]\n1 = "gpt-test"\n2 = "gpt-deep"\n',
        encoding="utf-8",
    )
    assert load_loom_config()["reflection"]["auto_reflect"] is True
    monkeypatch.setattr(core_session, "_load_loom_config", load_loom_config)
    monkeypatch.setattr(core_session, "_load_env", lambda project_root=None: {})
    monkeypatch.setattr(core_session, "build_embedding_provider", lambda *a: None)
    monkeypatch.setattr(Confirm, "ask", lambda *a, **k: True)
    router = MagicMock()
    router.get_provider.return_value.native_context_window.return_value = 128_000
    router.chat = AsyncMock(return_value=LLMResponse(text=json.dumps({
        "task_type": "quick_qa", "task_type_confidence": 1,
        "quality_score": 1, "instructions_followed": [],
        "instructions_violated": [], "failure_patterns": [],
        "success_patterns": [], "skill_edit_suggestions": [],
    }), tool_uses=[], stop_reason="end_turn"))
    monkeypatch.setattr(core_session, "build_router", lambda *a, **k: router)
    db_path = tmp_path / "memory.db"
    store = SQLiteStore(str(db_path))
    await store.initialize()
    async with store.connect() as conn:
        await SemanticMemory(conn).upsert(SemanticEntry(
            id="legacy", key="skill:deep:diagnostic:old", value="historical diagnostic",
            source="task_reflector:old-session",
        ))
    session = LoomSession(model="gpt-test", db_path=str(db_path), workspace=workspace)
    await session.start()
    session._judge_mode = "off"
    await session._memory.procedural.upsert(SkillGenome(
        name="deep", body="Do careful work", confidence=0.4,
        success_rate=0.45, usage_count=4, model_tier=2,
    ))
    session._skill_tier_snapshot["deep"] = 2
    seen_models = []
    step = 0

    async def stream_chat(**kwargs):
        nonlocal step
        seen_models.append(kwargs["model"])
        step += 1
        if step == 1:
            uses = [ToolUse(id="load-1", name="load_skill", args={"name": "deep"})]
        elif step == 2 and mode == "normal":
            uses = [ToolUse(id="unload-1", name="unload_skill", args={"name": "deep"})]
        else:
            uses = []
        reason = "tool_use" if uses else ("unexpected_stop" if mode == "drop" else "end_turn")
        yield "", LLMResponse(
            text=None if uses else "done", tool_uses=uses, stop_reason=reason,
            input_tokens=10, output_tokens=5,
            raw_message={"role": "assistant", "content": None if uses else "done",
                         **({"tool_calls": [{"id": u.id, "type": "function", "function": {
                             "name": u.name, "arguments": json.dumps(u.args),
                         }} for u in uses]} if uses else {})},
        )

    router.stream_chat = stream_chat
    router.native_max_tokens = lambda model: None
    router.format_tool_result = lambda model, tid, content, success=True: {
        "role": "tool", "tool_call_id": tid, "content": content,
    }
    return session, router, seen_models


@pytest.mark.parametrize("mode", ["normal", "drop"])
async def test_turn_boundaries_keep_tier_and_ledger_without_diagnostics(monkeypatch, tmp_path, mode):
    session, router, seen_models = await _session(monkeypatch, tmp_path, mode)
    stopped = False
    try:
        before = await session._memory.procedural.get("deep")
        events = []
        async for event in session.stream_turn("Use the deep skill"):
            events.append(event)
        assert any(isinstance(e, TurnDone) for e in events)
        if mode == "drop":
            assert any(isinstance(e, TurnDropped) for e in events)
        assert seen_models[:2] == ["gpt-test", "gpt-deep"]
        assert session._skill_outcome_tracker.activated_skills == []
        assert session._compute_skill_max_tier() == 0
        await asyncio.sleep(0)
        after = await session._memory.procedural.get("deep")
        assert (after.confidence, after.success_rate, after.usage_count) == (
            before.confidence, before.success_rate, before.usage_count,
        )
        assert session._ledger_store is not None
        ledger = await session._ledger_store.events.where(session_id=session.session_id).order_by("timestamp").all()
        skill_events = [(e.payload.get("tool_name"), e.payload.get("phase"))
                        for e in ledger if e.event_type == "tool_lifecycle"
                        and e.payload.get("skill_id") == "deep"]
        assert skill_events[:2] == [("load_skill", "BEGIN"), ("load_skill", "END")]
        if mode == "normal":
            assert skill_events[2:] == [("unload_skill", "BEGIN"), ("unload_skill", "END")]
        cur = await session._db.execute(
            "SELECT id, value FROM semantic_entries WHERE key GLOB 'skill:*:diagnostic:*' "
            "OR source='self_reflection' OR source='skill_evolution'"
        )
        assert await cur.fetchall() == [("legacy", "historical diagnostic")]
        assert not [t for t in asyncio.all_tasks() if t.get_name().startswith((
            "task_reflect:", "behavioural_triples:",
        ))]
        # A manual downgrade on the next turn must not re-escalate from stale
        # activation state (the model's next response issues no load_skill).
        session._set_sticky_tier(1, reason="user downgrade", source="user")
        async for _ in session.stream_turn("Continue without loading a skill"):
            pass
        assert seen_models[-1] == "gpt-test"
        await session.stop()
        stopped = True
        router.chat.assert_not_awaited()  # includes stop's retired evolution hook
    finally:
        if not stopped:
            await session.stop()
