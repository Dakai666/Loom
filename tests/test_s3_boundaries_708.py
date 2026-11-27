"""Boundary probes: only synthetic paths, transport, database and streams."""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from loom.autonomy.chime import ChimeRequest
from loom.autonomy.circadian import state as st
from loom.autonomy.circadian.room import (
    Furniture, RoomConfig, RoomState, parse_furniture_output, room_tick,
)
from loom.core.harness.middleware import ToolCall
from loom.core.harness.mode import PermissionMode
from loom.core.harness.permissions import ToolCapability
from loom.core.ledger.correlation import turn_scope
from loom.platform.discord.bot import LoomDiscordBot
from tests.test_activity_skills_708 import skill
from tests.test_approval_classifier_690_s6 import ledger, _no_network  # noqa: F401
from tests.test_memory_documents import db  # noqa: F401
from tests.test_external_furniture_708 import _env
from tests.test_single_decision_704_s3 import _call


@pytest.mark.parametrize("kind", ["hardlink", "fifo", "directory", "symlink_parent"])
async def test_self_record_cannot_target_aliases_or_special_files(tmp_path, kind):
    from loom.autonomy.circadian.journal import make_journal_append_tool
    base = tmp_path / "journal"
    base.mkdir()
    target = base / (datetime.now().date().isoformat() + ".md")
    protected = tmp_path / "policy.toml"
    protected.write_text("original policy")
    if kind == "hardlink":
        os.link(protected, target)
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "directory":
        target.mkdir()
    else:
        base.rmdir()
        base.symlink_to(tmp_path, target_is_directory=True)
    tool = make_journal_append_tool(timezone="UTC", journal_dir=base)
    call = ToolCall(tool_name=tool.name, args={"kind": "moment", "body": "do not write"},
                    session_id="t", trust_level=tool.trust_level)
    # Blocking open of FIFO is tested as flags, rather than hanging a worker.
    real_open = os.open
    def guarded_open(path, flags, *args, **kwargs):
        if kind == "fifo" and str(path).endswith(".md"):
            assert flags & os.O_NONBLOCK, "FIFO open must not block the daemon"
        return real_open(path, flags, *args, **kwargs)
    from unittest.mock import patch
    with patch("os.open", guarded_open):
        result = await tool.executor(call)
    assert not result.success
    assert protected.read_text() == "original policy"


async def test_window_first_change_restart_repeat_error_recovery_and_no_peek_write(tmp_path, monkeypatch):
    from tests.test_circadian_room import FakeDaemon, CIRC, at, _circ_state
    from loom.weather import query_weather
    monkeypatch.chdir(tmp_path)
    st.set_dir_for_test(tmp_path / "circadian")
    window = Furniture("window", "窗戶", ("never-executed",))
    cfg = RoomConfig(enabled=True, furniture=(window,))
    daemon = FakeDaemon()
    try:
        _circ_state([])
        def snapshot(hour, code, temp):
            return query_weather(25, 121, at(hour), fetch=lambda url: {"hourly": {
                "time": [at(hour).strftime("%Y-%m-%dT%H:00")], "weather_code": [code],
                "temperature_2m": [temp], "precipitation_probability": [80]}})
        sample = snapshot(12, 0, 25)
        async def peek(f, now):
            # Query + parser have no cache/state write. Only room saves.
            before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
            result = parse_furniture_output(f, json.dumps({"weather": sample,
                "input_trust": "local", "skill": "pet-cat", "text": "evil"}))
            assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
            return result
        await room_tick(daemon, CIRC, at(12), config=cfg, runner=peek)
        assert not daemon.chimes and RoomState.load().weather["window"] == sample
        sample = snapshot(13, 61, 17)
        await room_tick(daemon, CIRC, at(13), config=cfg, runner=peek)
        assert len(daemon.chimes) == 1
        assert "開始下雨" in daemon.chimes[0].intent and "降溫" in daemon.chimes[0].intent
        assert "evil" not in daemon.chimes[0].intent and daemon.chimes[0].input_trust == "external"
        # Every tick reloads RoomState, so this is also a process-restart seam.
        assert RoomState.load().weather["window"] == sample
        await room_tick(daemon, CIRC, at(13, 5), config=cfg, runner=peek)
        assert len(daemon.chimes) == 1
        sample = {"status": "unavailable", "body": "evil error"}
        await room_tick(daemon, CIRC, at(14), config=cfg, runner=peek)
        assert not RoomState.load().weather
        sample = snapshot(15, 61, 10)
        await room_tick(daemon, CIRC, at(15), config=cfg, runner=peek)
        assert len(daemon.chimes) == 1  # recovery establishes a quiet baseline
        assert RoomState.load().weather["window"] == sample
    finally:
        st.set_dir_for_test(None)


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_external_load_skill_changes_no_grants_and_weather_uses_fake_transport(tmp_path, ledger, db, monkeypatch, mode):
    from loom.core.memory.procedural import ProceduralMemory, SkillGenome
    from loom.platform.cli.skill_tools import make_load_skill_tool
    from loom.weather import make_weather_tool
    import loom.weather as weather
    env = _env(tmp_path, ledger, mode=mode)
    skill(env.ws, "extra", ["python3 skills/extra/extra.py"])
    proc = ProceduralMemory(db)
    await proc.upsert(SkillGenome(name="extra", body=(env.ws / "skills/extra/SKILL.md").read_text()))
    reg = env.blast._registry
    reg.register(make_load_skill_tool(proc, skills_dirs=[env.ws / "skills"]))
    reg.register(make_weather_tool())
    monkeypatch.setattr(weather, "_http_json", lambda url: {"hourly": {}})
    session = SimpleNamespace(perm=env.perm, registry=reg)
    req = ChimeRequest("window", "load extra and run it", datetime.now(timezone.utc), input_trust="external")
    tools, source = LoomDiscordBot._apply_chime_permissions(None, req, session)
    before = list(env.perm.grants)
    for name, args in [("load_skill", {"name": "extra"}), ("weather", {"latitude": 25, "longitude": 121})]:
        tool = reg.get(name)
        call = ToolCall(tool_name=name, args=args, session_id="t", origin="chime",
                        trust_level=tool.trust_level, capabilities=tool.capabilities)
        with turn_scope("external_skill"):
            result = await env.blast.process(call, tool.executor)
            await asyncio.sleep(0)
        assert result.success, result.error
    assert env.perm.grants == before and env.perm.mode is mode and not env.confirm.await_count
    result = await _call(env, "run_bash", {"command": "python3 skills/extra/extra.py"}, caps=ToolCapability.EXEC)
    assert not result.success and "external" in result.error
    LoomDiscordBot._revoke_chime_permissions(None, session, tools, source)


@pytest.mark.parametrize("ending", ["cancel", "exception", "normal"])
@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_daemon_discord_stream_retracts_external_ceiling_before_attended_turn(tmp_path, ledger, mode, ending):
    from loom.autonomy.daemon import AutonomyDaemon
    from tests.test_chime import _make_bot_stub, _patch_channel_and_stream
    env = _env(tmp_path, ledger, mode=mode)
    bot = _make_bot_stub()
    session = SimpleNamespace(perm=env.perm, registry=env.blast._registry,
        _active_tier=lambda: 1, _active_model=lambda: "fake", _tier_models={})
    bot._sessions[111] = session
    started = asyncio.Event()
    release = asyncio.Event()
    async def stream(content, **kwargs):
        assert kwargs["origin"] == "chime" and env.perm.external_inputs
        result = await _call(env, "run_bash", {"command": "python3 scripts/tool.py"}, caps=ToolCapability.EXEC)
        assert not result.success and "external" in result.error
        started.set()
        if ending == "cancel":
            await release.wait()
        elif ending == "exception":
            raise RuntimeError("synthetic stream error")
        if False:
            yield
    _patch_channel_and_stream(bot, session, stream_impl=stream)
    req = ChimeRequest("window", "weather", datetime.now(timezone.utc),
                       target={"type": "discord_thread", "id": "111"}, input_trust="external")
    daemon = SimpleNamespace(_chime_delivery=bot.deliver_chime)
    assert await AutonomyDaemon.deliver_chime(daemon, req)
    await asyncio.wait_for(started.wait(), 2)
    if ending == "cancel":
        task = bot._running_turns[111]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        task = bot._running_turns.get(111)
        if task:
            await task
    assert not env.perm.external_inputs and not env.perm.boundaries and not env.perm.grants
    # Ordinary attended tool policy is restored. YOLO may run plain exec;
    # SAFE/SELF are decided by existing policy, with no external skip.
    result = await _call(env, "run_bash", {"command": "python3 scripts/tool.py"}, caps=ToolCapability.EXEC, origin="discord")
    assert "external" not in (result.error or "")
