"""
Issue #668 S1 — a chime turn must never block on a human confirmation.

Before: ``origin="chime"`` was not in ``_UNATTENDED_ORIGINS``, so an
unauthorized call inside a scheduled circadian phase opened a Discord confirm
prompt with no timeout. The chime turn held the session's turn lock while it
waited — measured waits of 27 min to ~12 h — and every later life event on the
same thread queued behind it.

Contract (PR #673 review added 5-7):
  1. Harness: a chime call that would need confirmation is skipped — the
     confirm callback is never invoked and the tool never runs. It is not a
     human DENY: no penalty-box count, and it is marked as an unattended skip.
  2. Calls already covered by the schedule's allowed_tools / scope grants
     still run (S1 must not shrink what a phase is allowed to do).
  3. Interactive origins still prompt (S1 must not silence real confirms).
  4. Discord: the chime turn posts ONE visible notice listing the skipped
     calls, so DK sees what was not done — and posts nothing when no call
     was skipped, including real policy / guard denials.
  5. load_skill's precondition-check approval is a confirm too: an
     unattended turn never prompts for it; the checks just aren't mounted.
  6. A thread left in /pause (hitl_mode) must not park an unattended turn on
     TurnPaused — nobody would resume it. Interactive turns still pause.
  7. An unauthorized exec call is a skip, never a "probe, then re-issue"
     hint (/auto, the only thing probing could unlock, is retired: #704 S3).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from loom.core.events import ToolBegin, ToolEnd
from loom.core.harness.middleware import BlastRadiusMiddleware, ToolCall, ToolResult
from loom.core.harness.permissions import PermissionContext, ToolCapability, TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.harness.scope import ScopeGrant, ScopeRequest, ScopeRequirement


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _write_resolver(call: ToolCall) -> ScopeRequest:
    return ScopeRequest(
        tool_name="write_file",
        capabilities=ToolCapability.MUTATES,
        requirements=[
            ScopeRequirement(
                resource="path", action="write",
                selector=call.args["path"], tool_name="write_file",
            )
        ],
    )


def _setup(*, scoped: bool):
    ran: list[str] = []

    async def handler(call: ToolCall) -> ToolResult:
        ran.append(call.tool_name)
        return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True, output="ok")

    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="write_file", description="", input_schema={}, executor=handler,
        trust_level=TrustLevel.GUARDED, capabilities=ToolCapability.MUTATES,
        scope_resolver=_write_resolver if scoped else None,
    ))
    perm = PermissionContext(session_id="t")
    confirm = AsyncMock(return_value=True)
    import tempfile
    from pathlib import Path
    blast = BlastRadiusMiddleware(perm_ctx=perm, registry=reg, confirm_fn=confirm,
                                  workspace=Path(tempfile.mkdtemp()))
    return blast, handler, perm, confirm, ran


def _call(origin: str, path: str = "journal/today.md") -> ToolCall:
    return ToolCall(
        tool_name="write_file", args={"path": path},
        trust_level=TrustLevel.GUARDED, session_id="t", origin=origin,
        capabilities=ToolCapability.MUTATES,
    )


class TestChimeIsUnattended:
    async def test_scoped_call_skips_without_prompting(self):
        blast, handler, perm, confirm, ran = _setup(scoped=True)
        result = await blast.process(_call("chime"), handler)
        assert not result.success
        assert result.failure_type == "permission_denied"
        assert result.metadata.get("unattended_skip") is True
        confirm.assert_not_called()
        assert ran == []
        assert perm.recent_denies == 0  # not a human deny → no penalty box

    async def test_legacy_call_skips_without_prompting(self):
        blast, handler, perm, confirm, ran = _setup(scoped=False)
        result = await blast.process(_call("chime"), handler)
        assert not result.success
        assert result.metadata.get("unattended_skip") is True
        confirm.assert_not_called()
        assert ran == []
        assert perm.recent_denies == 0

    async def test_out_of_scope_expand_also_skips(self):
        blast, handler, perm, confirm, ran = _setup(scoped=True)
        perm.grant(ScopeGrant(resource="path", action="write", selector="news"))
        result = await blast.process(_call("chime", "Agent.md"), handler)
        assert not result.success
        assert result.metadata.get("unattended_skip") is True
        confirm.assert_not_called()
        assert ran == []

    async def test_schedule_granted_call_still_runs(self):
        blast, handler, perm, confirm, ran = _setup(scoped=True)
        perm.grant(ScopeGrant(resource="path", action="write", selector="news"))
        result = await blast.process(_call("chime", "news"), handler)
        assert result.success
        assert ran == ["write_file"]
        confirm.assert_not_called()

    async def test_allowed_tools_name_bridge_still_runs(self):
        blast, handler, perm, confirm, ran = _setup(scoped=False)
        perm.declare("test:boundary", ["write_file"])
        result = await blast.process(_call("chime"), handler)
        assert result.success
        assert ran == ["write_file"]
        confirm.assert_not_called()

    async def test_interactive_origin_still_prompts(self):
        blast, handler, perm, confirm, ran = _setup(scoped=True)
        result = await blast.process(_call("discord"), handler)
        confirm.assert_awaited_once()
        assert result.success
        assert ran == ["write_file"]


# ---------------------------------------------------------------------------
# Discord chime turn — one visible skip notice
# ---------------------------------------------------------------------------


def _bot_with_session(stream_impl):
    from loom.platform.discord.bot import LoomDiscordBot

    bot = LoomDiscordBot.__new__(LoomDiscordBot)
    bot._sessions = {}
    bot._running_turns = {}
    bot._chime_pending = {}
    bot._chime_dispatcher = {}
    bot._client = MagicMock()

    class _TypingCM:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False

    channel = MagicMock()
    channel.typing = MagicMock(return_value=_TypingCM())
    channel.send = AsyncMock(return_value=MagicMock())
    bot._client.get_channel = MagicMock(return_value=channel)

    session = MagicMock()
    session.perm.session_authorized = set()
    session._tier_models = {}
    session.stream_turn = stream_impl
    bot._sessions[111] = session
    return bot, channel


def _req():
    from loom.autonomy.chime import ChimeRequest

    return ChimeRequest(
        schedule_name="dawn", intent="wake up",
        fired_at=datetime.now(timezone.utc),
        target={"type": "discord_thread", "id": "111"},
    )


def _sent_texts(channel) -> list[str]:
    return [c.args[0] for c in channel.send.await_args_list if c.args]


class TestChimeSkipNotice:
    async def test_skipped_calls_produce_one_notice(self):
        async def stream(content, *, origin, **_kw):
            yield ToolBegin(name="memorize", args={"key": "k"}, call_id="c1")
            yield ToolEnd(
                name="memorize", success=False, output="skipped",
                duration_ms=0.0, call_id="c1", unattended_skip=True,
            )
            yield ToolBegin(name="run_bash", args={"command": "ls news/"}, call_id="c2")
            yield ToolEnd(
                name="run_bash", success=False, output="skipped",
                duration_ms=0.0, call_id="c2", unattended_skip=True,
            )
            yield ToolBegin(name="read_file", args={"path": "x"}, call_id="c3")
            yield ToolEnd(
                name="read_file", success=True, output="ok",
                duration_ms=0.0, call_id="c3",
            )

        bot, channel = _bot_with_session(stream)
        await bot._run_chime_turn(111, _req())

        notices = [t for t in _sent_texts(channel) if "⏭️" in t]
        assert len(notices) == 1
        assert "memorize" in notices[0]
        assert "run_bash" in notices[0]
        assert "ls news/" in notices[0]
        assert "read_file" not in notices[0]

    async def test_no_notice_when_nothing_skipped(self):
        async def stream(content, *, origin, **_kw):
            yield ToolBegin(name="read_file", args={"path": "x"}, call_id="c1")
            yield ToolEnd(
                name="read_file", success=False, output="not found",
                duration_ms=0.0, call_id="c1",
            )
            # A real policy / guard denial is NOT an unattended skip and must
            # not be labelled "未執行，不算拒絕" (#673 review P2).
            yield ToolBegin(name="write_file", args={"path": "x"}, call_id="c2")
            yield ToolEnd(
                name="write_file", success=False, output="denied by policy",
                duration_ms=0.0, call_id="c2", unattended_skip=False,
            )

        bot, channel = _bot_with_session(stream)
        await bot._run_chime_turn(111, _req())
        assert not [t for t in _sent_texts(channel) if "⏭️" in t]


# ---------------------------------------------------------------------------
# 7. Probe-and-retry wording only when probing could authorize
# ---------------------------------------------------------------------------


def _exec_setup():
    async def handler(call: ToolCall) -> ToolResult:
        return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True, output="ok")

    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="shell", description="", input_schema={}, executor=handler,
        trust_level=TrustLevel.GUARDED, capabilities=ToolCapability.EXEC,
    ))
    perm = PermissionContext(session_id="t")
    blast = BlastRadiusMiddleware(
        perm_ctx=perm, registry=reg, confirm_fn=AsyncMock(return_value=True),
    )
    call = ToolCall(
        tool_name="shell", args={"command": "ls"}, trust_level=TrustLevel.GUARDED,
        session_id="t", origin="chime", capabilities=ToolCapability.EXEC,
    )
    call.metadata["trajectory_anomaly"] = True
    return blast, handler, call


class TestProbeHintOnlyWhenItCouldHelp:
    async def test_unauthorized_exec_gets_skip_not_retry_hint(self):
        blast, handler, call = _exec_setup()
        result = await blast.process(call, handler)
        assert result.metadata.get("unattended_skip") is True
        assert "re-issue" not in result.error
        assert "Do not retry" in result.error


# ---------------------------------------------------------------------------
# 5. load_skill precondition-check approval
# ---------------------------------------------------------------------------


def _load_skill_tool(tmp_path, confirm):
    from loom.core.memory.procedural import SkillGenome
    from loom.platform.cli.skill_tools import make_load_skill_tool

    skill = SkillGenome(
        name="probe-skill", body="# probe-skill\n\nbody",
        precondition_check_refs=[{
            "ref": "checks.py:check", "applies_to": ["run_bash"],
            "description": "guard",
        }],
    )
    (tmp_path / "probe-skill").mkdir()
    (tmp_path / "probe-skill" / "SKILL.md").write_text(skill.body)
    procedural = MagicMock()
    procedural.get = AsyncMock(return_value=skill)
    procedural.list_active = AsyncMock(return_value=[])
    manager = MagicMock()
    manager.mount = MagicMock(return_value=[])
    tool = make_load_skill_tool(
        procedural=procedural, skills_dirs=[tmp_path],
        skill_check_manager=manager, semantic=None, confirm_fn=confirm,
    )
    return tool, manager


def _load_call(origin: str) -> ToolCall:
    return ToolCall(
        tool_name="load_skill", args={"name": "probe-skill"},
        trust_level=TrustLevel.SAFE, session_id="t", origin=origin,
    )


class TestLoadSkillApprovalUnattended:
    async def test_chime_load_skill_never_prompts_for_checks(self, tmp_path):
        confirm = AsyncMock(return_value=True)
        tool, manager = _load_skill_tool(tmp_path, confirm)
        result = await tool.executor(_load_call("chime"))
        assert result.success  # the skill body still loads
        confirm.assert_not_called()
        manager.mount.assert_not_called()

    async def test_attended_load_after_chime_skip_still_asks(self, tmp_path):
        # Same session: chime skips approval, then DK's turn must still be
        # able to approve — body dedup must not swallow it (#673 review r2).
        confirm = AsyncMock(return_value=False)
        tool, manager = _load_skill_tool(tmp_path, confirm)
        first = await tool.executor(_load_call("chime"))
        assert "precondition_checks_pending" in first.output
        confirm.assert_not_called()
        await tool.executor(_load_call("discord"))
        confirm.assert_awaited_once()
        # Once answered, it's settled: a further load is plain dedup.
        third = await tool.executor(_load_call("discord"))
        assert "already loaded" in third.output
        confirm.assert_awaited_once()

    async def test_interactive_load_skill_still_asks(self, tmp_path):
        confirm = AsyncMock(return_value=False)
        tool, manager = _load_skill_tool(tmp_path, confirm)
        await tool.executor(_load_call("discord"))
        confirm.assert_awaited_once()


# ---------------------------------------------------------------------------
# 6. HITL auto-pause vs unattended turns (real stream_turn)
# ---------------------------------------------------------------------------

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import loom as loom_pkg
from loom.core.events import TurnDone, TurnPaused


@pytest.fixture
def _isolate_default_registry():
    registry = loom_pkg._get_default_registry()
    original = dict(registry._tools)
    registry._tools.clear()
    try:
        yield
    finally:
        registry._tools.clear()
        registry._tools.update(original)


async def _hitl_session(monkeypatch, tmp_path: Path):
    """A started session in /pause (hitl_mode) whose model makes one SAFE
    tool call, then ends the turn."""
    from loom.core import session as session_module
    from loom.core.cognition.providers import LLMResponse, ToolUse
    from loom.core.session import LoomSession

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(session_module, "build_router", lambda *a, **k: MagicMock())
    monkeypatch.setattr(session_module, "_load_loom_config", lambda: {})
    monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {})
    monkeypatch.setattr(session_module, "build_embedding_provider", lambda env, cfg: None)
    from rich.prompt import Confirm
    monkeypatch.setattr(Confirm, "ask", lambda *a, **k: True)

    session = LoomSession(model="gpt-test", db_path=str(tmp_path / "loom.db"), workspace=workspace)
    await session.start()

    async def ping(call: ToolCall) -> ToolResult:
        return ToolResult(call.id, call.tool_name, True, output="pong")

    session.registry.register(ToolDefinition(
        name="ping", description="ping", trust_level=TrustLevel.SAFE,
        input_schema={"type": "object", "properties": {}}, executor=ping,
    ))

    steps = iter([[{}], []])

    async def stream_chat(**kwargs):
        calls = next(steps, [])
        uses = [ToolUse(id=f"call-{i}", name="ping", args=a) for i, a in enumerate(calls)]
        yield "", LLMResponse(
            text=None if uses else "done", tool_uses=uses,
            stop_reason="tool_use" if uses else "end_turn",
            input_tokens=10, output_tokens=5,
            raw_message={
                "role": "assistant", "content": None if uses else "done",
                **({"tool_calls": [
                    {"id": u.id, "type": "function",
                     "function": {"name": u.name, "arguments": json.dumps(u.args)}} for u in uses
                ]} if uses else {}),
            },
        )

    session.router = SimpleNamespace(
        stream_chat=stream_chat,
        native_max_tokens=lambda model: None,
        format_tool_result=lambda model, tid, content, success=True: {
            "role": "tool", "tool_call_id": tid, "content": content,
        },
    )
    session._judge_mode = "off"
    session.hitl_mode = True
    return session


@pytest.mark.usefixtures("_isolate_default_registry")
class TestHitlPauseUnattended:
    async def test_chime_turn_does_not_park_on_hitl_pause(self, monkeypatch, tmp_path):
        session = await _hitl_session(monkeypatch, tmp_path)
        try:
            async def run():
                return [e async for e in session.stream_turn("wake", origin="chime")]
            events = await asyncio.wait_for(run(), timeout=5)
            assert not any(isinstance(e, TurnPaused) for e in events)
            assert any(isinstance(e, TurnDone) for e in events)
            assert session.hitl_mode is True  # DK's /pause setting untouched
        finally:
            await session.stop()

    async def test_interactive_turn_still_pauses(self, monkeypatch, tmp_path):
        session = await _hitl_session(monkeypatch, tmp_path)
        try:
            gen = session.stream_turn("hi", origin="discord")
            paused = False
            async def first_pause():
                nonlocal paused
                async for e in gen:
                    if isinstance(e, TurnPaused):
                        paused = True
                        return
            await asyncio.wait_for(first_pause(), timeout=5)
            assert paused
            await gen.aclose()
        finally:
            await session.stop()


# ---------------------------------------------------------------------------
# send_discord_select blocks on a human pick — skip it when unattended
# ---------------------------------------------------------------------------


class TestSelectMenuUnattended:
    def _tool(self):
        from loom.platform.discord.tools import make_send_discord_select_tool

        client = MagicMock()
        channel = MagicMock()
        channel.send = AsyncMock()
        client.get_channel = MagicMock(return_value=channel)
        return make_send_discord_select_tool(client, 111), channel

    def _call(self, origin):
        return ToolCall(
            tool_name="send_discord_select",
            args={"title": "pick", "options": [
                {"label": "a", "value": "a"}, {"label": "b", "value": "b"},
            ]},
            trust_level=TrustLevel.SAFE, session_id="t", origin=origin,
        )

    async def test_chime_select_is_skipped_without_posting(self):
        tool, channel = self._tool()
        result = await asyncio.wait_for(tool.executor(self._call("chime")), timeout=2)
        assert not result.success
        assert result.metadata.get("unattended_skip") is True
        channel.send.assert_not_called()

    async def test_interactive_select_still_posts(self):
        tool, channel = self._tool()
        task = asyncio.ensure_future(tool.executor(self._call("discord")))
        for _ in range(50):
            if channel.send.await_count:
                break
            await asyncio.sleep(0)
        assert channel.send.await_count == 1
        task.cancel()
