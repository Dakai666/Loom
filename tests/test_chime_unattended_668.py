"""
Issue #668 S1 — a chime turn must never block on a human confirmation.

Before: ``origin="chime"`` was not in ``_UNATTENDED_ORIGINS``, so an
unauthorized call inside a scheduled circadian phase opened a Discord confirm
prompt with no timeout. The chime turn held the session's turn lock while it
waited — measured waits of 27 min to ~12 h — and every later life event on the
same thread queued behind it.

Contract:
  1. Harness: a chime call that would need confirmation is skipped — the
     confirm callback is never invoked and the tool never runs. It is not a
     human DENY: no penalty-box count, and it is marked as an unattended skip.
  2. Calls already covered by the schedule's allowed_tools / scope grants
     still run (S1 must not shrink what a phase is allowed to do).
  3. Interactive origins still prompt (S1 must not silence real confirms).
  4. Discord: the chime turn posts ONE visible notice listing the skipped
     calls, so DK sees what was not done — and posts nothing when no call
     was skipped.
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
    blast = BlastRadiusMiddleware(perm_ctx=perm, registry=reg, confirm_fn=confirm)
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
        perm.authorize("write_file")
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
        async def stream(content, *, origin):
            yield ToolBegin(name="memorize", args={"key": "k"}, call_id="c1")
            yield ToolEnd(
                name="memorize", success=False, output="skipped",
                duration_ms=0.0, call_id="c1", failure_type="permission_denied",
            )
            yield ToolBegin(name="run_bash", args={"command": "ls news/"}, call_id="c2")
            yield ToolEnd(
                name="run_bash", success=False, output="skipped",
                duration_ms=0.0, call_id="c2", failure_type="permission_denied",
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
        async def stream(content, *, origin):
            yield ToolBegin(name="read_file", args={"path": "x"}, call_id="c1")
            yield ToolEnd(
                name="read_file", success=False, output="not found",
                duration_ms=0.0, call_id="c1", failure_type="execution_error",
            )

        bot, channel = _bot_with_session(stream)
        await bot._run_chime_turn(111, _req())
        assert not [t for t in _sent_texts(channel) if "⏭️" in t]
