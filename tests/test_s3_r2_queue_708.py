"""R2-3: one trust cohort per delivery, with deferred local ownership."""
import json
from types import SimpleNamespace

import pytest

from loom.autonomy.circadian import state as st
from loom.autonomy.circadian.room import Furniture, RoomConfig, RoomState, room_tick, parse_furniture_output
from tests.test_circadian_room import FakeDaemon, CIRC, at, _circ_state


@pytest.mark.parametrize("external_priority", ["normal", "ambient"])
@pytest.mark.parametrize("fail_first", [False, True])
async def test_local_queue_survives_separation_restart_new_external_and_retry(tmp_path, monkeypatch, external_priority, fail_first):
    monkeypatch.chdir(tmp_path)
    st.set_dir_for_test(tmp_path / "circadian")
    try:
        _circ_state([])
        local = Furniture("cat", "喵吉", ("never",), input_trust="local", allowed_tools=("run_bash",),
            scope_grants=({"resource": "exec", "action": "execute", "selector": "python3 cat.py"},))
        outside = Furniture("window", "窗戶", ("never",), default_priority=external_priority)
        cfg = RoomConfig(enabled=True, min_gap_minutes=45, daily_budget=6, furniture=(local, outside))
        from loom.core.harness.registry import ToolRegistry
        from loom.core.harness.permissions import PermissionContext
        from loom.core.harness.middleware import BlastRadiusMiddleware, ToolCall, ToolResult
        from loom.platform.cli.tools import make_run_bash_tool, external_call_allowed
        from loom.platform.discord.bot import LoomDiscordBot
        from unittest.mock import AsyncMock
        (tmp_path / "cat.py").write_text("# never executed")
        registry = ToolRegistry()
        registry.register(make_run_bash_tool(tmp_path))
        perm = PermissionContext("two-cohorts")
        session = SimpleNamespace(perm=perm, registry=registry)
        async def prove_permission(req):
            tools, source = LoomDiscordBot._apply_chime_permissions(None, req, session)
            middleware = BlastRadiusMiddleware(perm, AsyncMock(return_value=False), registry,
                                               workspace=tmp_path, external_policy=external_call_allowed)
            definition = registry.get("run_bash")
            call = ToolCall(tool_name="run_bash", args={"command": "python3 cat.py"},
                session_id="two-cohorts", origin="chime", trust_level=definition.trust_level,
                capabilities=definition.capabilities)
            executor = AsyncMock(return_value=ToolResult(call_id=call.id, tool_name=call.tool_name, success=True))
            try:
                result = await middleware.process(call, executor)
                assert result.success is (req.input_trust == "local")
                assert executor.await_count == (1 if req.input_trust == "local" else 0)
            finally:
                LoomDiscordBot._revoke_chime_permissions(None, session, tools, source)
            assert not perm.external_inputs and not perm.grants and not perm.boundaries
        # Reconstruct both entries; no new local signal will ever be emitted.
        RoomState(date="2026-09-23", queue=[
            {"source": f.name, "id": "event", "key": f.name, "text": text,
             "priority": f.default_priority, "input_trust": f.input_trust, "at": at(11).isoformat()}
            for f, text in [(local, "LOCAL_CAT"), (outside, "EXTERNAL_RAIN")]]).save()
        count = 0
        async def runner(f, now):
            nonlocal count
            items = []
            if f.name == "window" and now >= at(13):
                count += 1
                items = [{"id": "event", "key": f"new{count}", "text": "NEW_EXTERNAL"}]
            return parse_furniture_output(f, json.dumps({"signals": items}))
        daemon = FakeDaemon(accept=not fail_first)
        await room_tick(daemon, CIRC, at(12), config=cfg, runner=runner)
        assert len(daemon.chimes) == 1
        first = daemon.chimes[0]
        if external_priority == "normal":
            assert first.input_trust == "external" and "LOCAL_CAT" not in first.intent
            assert {q["source"] for q in RoomState.load().queue} >= {"cat"}
            assert "cat:event:cat" not in RoomState.load().seen
        else:
            # Ambient cannot wake alone or suppress an eligible local turn.
            assert first.input_trust == "local" and "EXTERNAL_RAIN" not in first.intent
        if fail_first:
            assert {q["source"] for q in RoomState.load().queue} == {"cat", "window"}
            assert RoomState.load().wakes_today == 0
            assert not RoomState.load().seen
            daemon.accept = True
            await room_tick(daemon, CIRC, at(12, 5), config=cfg, runner=runner)
        await prove_permission(daemon.chimes[-1])
        # Gap still holds. No blanket "next tick" bypass to get care done.
        before = len(daemon.chimes)
        await room_tick(daemon, CIRC, at(12, 10), config=cfg, runner=runner)
        assert len(daemon.chimes) == before
        await room_tick(daemon, CIRC, at(13), config=cfg, runner=runner)
        if external_priority == "normal":
            last = daemon.chimes[-1]
            assert last.input_trust == "local" and "LOCAL_CAT" in last.intent
            assert "EXTERNAL" not in last.intent and last.scope_grants
            assert all(q["source"] != "cat" for q in RoomState.load().queue)
            await prove_permission(last)
        else:
            assert len(daemon.chimes) == before  # external ambient still cannot wake itself
        assert RoomState.load().wakes_today <= 2
    finally:
        st.set_dir_for_test(None)
