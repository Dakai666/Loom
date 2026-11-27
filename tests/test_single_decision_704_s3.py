"""
Contract: #704 S3 — one decision point; no name-level authorization.

- ``/auto`` (exec_auto) is retired on every frontend.
- A tool name never authorizes by itself. ``allowed_tools`` (a routine's
  or the Discord platform's) becomes a *declaration* in a boundary
  (``PermissionContext.declare``): the call still goes through the
  permission-mode decision point, where the hard guardrails and the
  program's scope ceilings hold. A declaration never covers EXEC, never
  crosses a declared scope (EXPAND_SCOPE), never covers CRITICAL.
- A program's declaration covers only that program's unattended calls; the
  attended turn that preempts a chime does not inherit it.
- A human "allow once" on a tool without a scope resolver covers that one
  call, not the rest of the session.

Same harness as the S1/S2 tests; nothing reaches the network.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from loom.core.harness.middleware import BlastRadiusMiddleware, ToolCall, ToolResult
from loom.core.harness.mode import PermissionMode
from loom.core.harness.permissions import PermissionContext, ToolCapability, TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.harness.scope import ConfirmDecision, ScopeGrant
from loom.core.ledger.correlation import turn_scope
from loom.platform.cli.tools import (
    _fetch_url_resolver,
    _make_run_bash_resolver,
    _make_write_file_resolver,
    _spawn_agent_resolver,
)

from tests.test_approval_classifier_690_s6 import (  # noqa: F401  (fixtures)
    FakeModel,
    _no_network,
    ledger,
)
from tests.test_permission_mode_704 import _decisions, _env


def _register(env, name, *, resolver=None, caps=ToolCapability.NETWORK, trust=TrustLevel.GUARDED):
    env.blast._registry.register(ToolDefinition(
        name=name, description="", input_schema={}, executor=env.executor,
        trust_level=trust, capabilities=caps, scope_resolver=resolver,
    ))


async def _call(env, name, args, *, origin="chime", caps=ToolCapability.NETWORK,
                trust=TrustLevel.GUARDED) -> ToolResult:
    call = ToolCall(tool_name=name, args=args, trust_level=trust, capabilities=caps,
                    session_id="t", origin=origin)
    env.turn.calls.append((call.id, name, dict(args)))
    env.confirm.reset_mock()
    with turn_scope("turn_test"):
        result = await env.blast.process(call, env.executor)
        await asyncio.sleep(0)
    return result


def _skipped(r: ToolResult) -> bool:
    return not r.success and r.metadata.get("unattended_skip") is True


# ---------------------------------------------------------------------------
# /auto and name-level authorization are gone
# ---------------------------------------------------------------------------


class TestRetired:
    def test_permission_context_has_no_name_authorization(self):
        perm = PermissionContext(session_id="x")
        for gone in ("exec_auto", "enable_exec_auto", "disable_exec_auto", "authorize",
                     "revoke", "is_authorized", "session_authorized", "attended_authorized"):
            assert not hasattr(perm, gone), gone

    def test_middleware_has_no_exec_auto_or_bridge(self):
        for gone in ("_exec_auto_approved", "_exec_auto_blocked_only_by_probe", "_name_authorized"):
            assert not hasattr(BlastRadiusMiddleware, gone), gone

    async def test_cli_auto_says_retired(self, capsys):
        from loom.platform.cli import main as cli_main
        session = SimpleNamespace(perm=PermissionContext(session_id="x"), _strict_sandbox=True)
        await cli_main._handle_slash("/auto", session)
        out = capsys.readouterr().out
        assert "/mode" in out and not hasattr(session.perm, "exec_auto")

    def test_discord_auto_says_retired(self):
        pytest.importorskip("discord")
        from loom.platform.discord.bot import LoomDiscordBot
        bot = LoomDiscordBot.__new__(LoomDiscordBot)
        out = bot._cmd_auto(SimpleNamespace(perm=PermissionContext(session_id="x"), _strict_sandbox=True))
        assert "/mode" in out


# ---------------------------------------------------------------------------
# Declarations go through the decision point
# ---------------------------------------------------------------------------


class TestDeclarations:
    @pytest.mark.parametrize("mode", list(PermissionMode))
    async def test_declared_tool_runs_in_its_program(self, tmp_path, ledger, mode):
        env = _env(tmp_path, ledger, mode=mode)
        _register(env, "memorize", caps=ToolCapability.MUTATES)
        env.perm.declare("autonomy:dawn", ["memorize"], unattended_only=True)
        r = await _call(env, "memorize", {"content": "x"}, caps=ToolCapability.MUTATES)
        assert r.success and env.confirm.await_count == 0
        last = (await _decisions(ledger))[-1]
        assert (last["decision_source"], last["reason_code"]) == ("program", "autonomy:dawn")

    async def test_undeclared_tool_skips_unattended(self, tmp_path, ledger):
        env = _env(tmp_path, ledger)
        _register(env, "memorize", caps=ToolCapability.MUTATES)
        env.perm.declare("autonomy:dawn", ["recall"], unattended_only=True)
        r = await _call(env, "memorize", {"content": "x"}, caps=ToolCapability.MUTATES)
        assert _skipped(r)

    async def test_program_declaration_is_not_seen_by_an_attended_turn(self, tmp_path, ledger):
        env = _env(tmp_path, ledger, mode=PermissionMode.SAFE)
        _register(env, "memorize", caps=ToolCapability.MUTATES)
        env.perm.declare("autonomy:dawn", ["memorize"], unattended_only=True)
        await _call(env, "memorize", {"content": "x"}, origin="discord", caps=ToolCapability.MUTATES)
        assert env.confirm.await_count == 1

    async def test_platform_declaration_covers_attended_and_unattended(self, tmp_path, ledger):
        env = _env(tmp_path, ledger, mode=PermissionMode.SAFE)
        _register(env, "send_discord_file")
        env.perm.declare("platform:discord", ["send_discord_file"])
        for origin in ("discord", "chime"):
            r = await _call(env, "send_discord_file", {"path": "x"}, origin=origin)
            assert r.success and env.confirm.await_count == 0

    async def test_withdraw(self, tmp_path, ledger):
        env = _env(tmp_path, ledger)
        _register(env, "memorize", caps=ToolCapability.MUTATES)
        env.perm.declare("autonomy:dawn", ["memorize"], unattended_only=True)
        env.perm.withdraw("autonomy:dawn")
        assert _skipped(await _call(env, "memorize", {"content": "x"}, caps=ToolCapability.MUTATES))

    async def test_never_covers_exec(self, tmp_path, ledger):
        env = _env(tmp_path, ledger)
        env.perm.declare("autonomy:room", ["run_bash"], unattended_only=True)
        r = await _call(env, "run_bash", {"command": "python3 skills/pet-cat/pet.py status"},
                        caps=ToolCapability.EXEC)
        assert _skipped(r)

    async def test_never_covers_critical(self, tmp_path, ledger):
        env = _env(tmp_path, ledger)
        _register(env, "nuke", trust=TrustLevel.CRITICAL)
        env.perm.declare("autonomy:x", ["nuke"], unattended_only=True)
        assert _skipped(await _call(env, "nuke", {}, trust=TrustLevel.CRITICAL))

    async def test_never_crosses_a_declared_scope(self, tmp_path, ledger):
        """#525: a routine's path grant is its ceiling; naming write_file
        does not widen it."""
        env = _env(tmp_path, ledger, grants=[ScopeGrant(
            resource="path", action="write", selector="news", source="autonomy:shared_learning")])
        env.perm.declare("autonomy:shared_learning", ["write_file"], unattended_only=True)
        r = await env.run("write_file", {"path": "notes/x.md", "content": "x"}, origin="chime")
        assert _skipped(r)
        assert (await env.run("write_file", {"path": "news/x.md", "content": "x"}, origin="chime")).success

    @pytest.mark.parametrize("path", [".env", "loom.toml", "../outside.md", "skills/pet-cat/checks.py"])
    async def test_never_crosses_a_hard_guardrail(self, tmp_path, ledger, path):
        """The old bridge let a named write_file with no path grant write
        anywhere; a declaration still meets the hard guardrails."""
        env = _env(tmp_path, ledger)
        env.perm.declare("autonomy:x", ["write_file"], unattended_only=True)
        r = await env.run("write_file", {"path": path, "content": "x"}, origin="chime")
        assert _skipped(r)

    async def test_scope_aware_tool_without_a_grant_runs_when_declared(self, tmp_path, ledger):
        """curiosity declares fetch_url / spawn_agent and no grant for
        their resource: the declaration is the program's boundary for it."""
        env = _env(tmp_path, ledger)
        _register(env, "fetch_url", resolver=_fetch_url_resolver)
        _register(env, "spawn_agent", resolver=_spawn_agent_resolver, caps=ToolCapability.AGENT_SPAN)
        env.perm.declare("autonomy:curiosity", ["fetch_url", "spawn_agent"], unattended_only=True)
        assert (await _call(env, "fetch_url", {"url": "https://example.com/a"})).success
        assert (await _call(env, "spawn_agent", {"task": "x"}, caps=ToolCapability.AGENT_SPAN)).success

    async def test_declared_search_with_a_secret_query_skips(self, tmp_path, ledger):
        from loom.extensibility.mcp_client import make_mcp_effect_scope_resolver
        env = _env(tmp_path, ledger)
        _register(env, "minimax_coding__web_search",
                  resolver=make_mcp_effect_scope_resolver("minimax_coding", "public_search"),
                  caps=ToolCapability.NETWORK | ToolCapability.READ_PROBE)
        env.perm.declare("autonomy:x", ["minimax_coding__web_search"], unattended_only=True)
        r = await _call(env, "minimax_coding__web_search", {"query": "api_key sk-proj-abcdefghij"})
        assert _skipped(r)


class TestAllowOnce:
    async def test_confirm_on_a_legacy_tool_covers_one_call(self, tmp_path, ledger):
        env = _env(tmp_path, ledger, mode=PermissionMode.SAFE,
                   confirm_result=ConfirmDecision.ONCE)
        _register(env, "memorize", caps=ToolCapability.MUTATES)
        for _ in range(2):
            r = await _call(env, "memorize", {"content": "x"}, origin="discord", caps=ToolCapability.MUTATES)
            assert r.success and env.confirm.await_count == 1


class TestRunsUnasked:
    def test_parallel_dispatch_view(self):
        perm = PermissionContext(session_id="x")
        perm.declare("platform:discord", ["send_discord_file"])
        perm.declare("autonomy:dawn", ["memorize"], unattended_only=True)
        assert perm.runs_unasked("read_file", TrustLevel.SAFE)
        assert perm.runs_unasked("send_discord_file", TrustLevel.GUARDED)
        assert not perm.runs_unasked("memorize", TrustLevel.GUARDED)  # attended view
        assert perm.runs_unasked("memorize", TrustLevel.GUARDED, attended=False)
        assert not perm.runs_unasked("send_discord_file", TrustLevel.CRITICAL)


def test_subagent_declares_only_what_its_parent_declared():
    from loom.core.agent.subagent import subagent_declarations
    child = ["read_file", "memorize", "send_discord_file", "write_file"]
    assert subagent_declarations(child, parent_declared={"memorize"}) == ["memorize"]
