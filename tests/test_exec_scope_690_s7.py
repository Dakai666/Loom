"""
Contract: a name-level grant never authorizes shell (#690 S7).

Before S7 the #525 legacy bridge honoured ``allowed_tools = ["run_bash"]`` on
CONFIRM, so any command passed in a dawn/pet phase turn. Now a phase or
schedule authorizes shell only through ``exec`` scope grants: either the
explicit ``workspace`` / ``*`` selectors, or a narrow command shape whose
selector is the argv prefix a command must start with. Anything else in an
unattended turn is skipped with a notice (#668 S1), never executed.

The tests drive ``BlastRadiusMiddleware`` with the real ``run_bash`` scope
resolver and a stub executor, so "not executed" means the executor never ran.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from loom.core.harness.middleware import BlastRadiusMiddleware, ToolCall, ToolResult
from loom.core.harness.permissions import PermissionContext, ToolCapability, TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.harness.scope import ScopeGrant, ScopeRequest, ScopeRequirement
from loom.platform.cli.tools import _make_run_bash_resolver

PET = "python3 skills/pet-cat/pet.py"


def _setup(tmp_path: Path, *, grants: list[ScopeGrant] = (), name_authorized=True):
    ran: list[str] = []

    async def executor(call: ToolCall) -> ToolResult:
        ran.append(call.args["command"])
        return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True, output="ok")

    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="run_bash", description="", input_schema={}, executor=executor,
        trust_level=TrustLevel.GUARDED, capabilities=ToolCapability.EXEC,
        scope_resolver=_make_run_bash_resolver(tmp_path),
    ))
    perm = PermissionContext(session_id="t")
    if name_authorized:
        perm.authorize("run_bash")  # what allowed_tools = ["run_bash"] does
    for g in grants:
        perm.grant(g)
    confirm = AsyncMock(return_value=True)
    blast = BlastRadiusMiddleware(perm_ctx=perm, registry=reg, confirm_fn=confirm)
    return blast, executor, ran, confirm


def _bash(command: str, *, origin="chime", trust=TrustLevel.GUARDED) -> ToolCall:
    return ToolCall(
        tool_name="run_bash", args={"command": command},
        trust_level=trust, capabilities=ToolCapability.EXEC,
        session_id="t", origin=origin,
    )


def _shape(selector: str, **constraints) -> ScopeGrant:
    return ScopeGrant(
        resource="exec", action="execute", selector=selector,
        constraints=constraints, source="autonomy:pet",
    )


def _assert_skipped(result: ToolResult, ran: list[str]) -> None:
    assert ran == []
    assert not result.success
    assert result.metadata.get("unattended_skip") is True
    assert result.error.startswith("Skipped (not executed)")


class TestNameGrantNoLongerCoversShell:
    async def test_allowed_tools_run_bash_alone_is_skipped(self, tmp_path):
        blast, executor, ran, _ = _setup(tmp_path)
        result = await blast.process(_bash("date"), executor)
        _assert_skipped(result, ran)

    @pytest.mark.parametrize("origin", ["autonomy", "subagent"])
    async def test_other_unattended_origins_are_skipped_too(self, tmp_path, origin):
        # A sub-agent's registry authorizes every whitelisted tool by name, so
        # spawn_agent(tools=["run_bash"]) was another way to blanket shell.
        blast, executor, ran, _ = _setup(tmp_path)
        result = await blast.process(_bash("date", origin=origin), executor)
        _assert_skipped(result, ran)

    async def test_resolver_failure_does_not_fall_back_to_the_name(self, tmp_path):
        # A non-string command makes the scope resolver raise, which routes the
        # call to the legacy path. The name grant must not authorize it there.
        blast, executor, ran, _ = _setup(tmp_path)
        call = _bash("date")
        call.args["command"] = ["rm", "-rf", "x"]
        result = await blast.process(call, executor)
        assert ran == []
        assert result.metadata.get("unattended_skip") is True

    async def test_attended_turn_asks_the_human_instead(self, tmp_path):
        blast, executor, ran, confirm = _setup(tmp_path)
        result = await blast.process(_bash("date", origin="discord"), executor)
        assert result.success
        assert ran == ["date"]
        confirm.assert_awaited_once()

    async def test_name_bridge_still_covers_non_exec_tools(self, tmp_path):
        # S7 narrows shell only; a scoped non-exec tool authorized by name
        # with no grant for its resource keeps the #525 bridge.
        async def executor(call):
            return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True)

        def resolver(call):
            return ScopeRequest(
                tool_name="web_search", capabilities=ToolCapability.NETWORK,
                requirements=[ScopeRequirement(
                    resource="network", action="connect", selector="example.com",
                )],
            )

        reg = ToolRegistry()
        reg.register(ToolDefinition(
            name="web_search", description="", input_schema={}, executor=executor,
            trust_level=TrustLevel.GUARDED, capabilities=ToolCapability.NETWORK,
            scope_resolver=resolver,
        ))
        perm = PermissionContext(session_id="t")
        perm.authorize("web_search")
        blast = BlastRadiusMiddleware(perm_ctx=perm, registry=reg, confirm_fn=AsyncMock())
        call = ToolCall(
            tool_name="web_search", args={}, trust_level=TrustLevel.GUARDED,
            capabilities=ToolCapability.NETWORK, session_id="t", origin="chime",
        )
        assert (await blast.process(call, executor)).success


class TestNarrowCommandShape:
    @pytest.mark.parametrize("command", [
        f"{PET} status",
        f"{PET} buy bowl_refill_pack",
        f"  {PET}   play  ",
        f"{PET} 'status'",
    ])
    async def test_command_inside_the_shape_runs(self, tmp_path, command):
        blast, executor, ran, _ = _setup(tmp_path, grants=[_shape(PET)])
        result = await blast.process(_bash(command), executor)
        assert result.success
        assert ran == [command]

    async def test_shape_with_quoted_argument_matches(self, tmp_path):
        shape = 'date "+%Y-%m-%d %H:%M:%S %Z (%A)"'
        blast, executor, ran, _ = _setup(tmp_path, grants=[_shape(shape)])
        result = await blast.process(_bash(shape), executor)
        assert result.success

    @pytest.mark.parametrize("command", [
        "cat ~/.loom/auth.json",
        "python3 skills/pet-cat/other.py status",
        "python3 skills/pet-cat/pet.pyx status",
        "python3",
        "rm -rf skills/pet-cat",
    ])
    async def test_command_outside_the_shape_is_skipped(self, tmp_path, command):
        blast, executor, ran, _ = _setup(tmp_path, grants=[_shape(PET)])
        _assert_skipped(await blast.process(_bash(command), executor), ran)

    async def test_skip_holds_without_the_name_grant(self, tmp_path):
        blast, executor, ran, _ = _setup(
            tmp_path, grants=[_shape(PET)], name_authorized=False,
        )
        _assert_skipped(await blast.process(_bash("date"), executor), ran)

    @pytest.mark.parametrize("tail", [
        "; rm -rf x",
        " && rm -rf x",
        " || rm -rf x",
        " | sh",
        " & rm -rf x",
        " $(rm -rf x)",
        " `rm -rf x`",
        " ${HOME}",
        " $HOME",
        "\nrm -rf x",
        " > skills/pet-cat/state.json",
        " < /etc/passwd",
        " <<EOF",
        " (rm -rf x)",
        " \\; rm -rf x",
        ';rm -rf x',
    ])
    async def test_chained_or_expanded_command_cannot_ride_a_shape(self, tmp_path, tail):
        blast, executor, ran, _ = _setup(tmp_path, grants=[_shape(PET)])
        _assert_skipped(await blast.process(_bash(f"{PET} status{tail}"), executor), ran)

    async def test_cd_prefix_does_not_match_a_shape(self, tmp_path):
        # The live phases used `cd skills/pet-cat && python3 pet.py status`.
        # That is a chain; it must be rewritten to the cd-free form.
        blast, executor, ran, _ = _setup(
            tmp_path, grants=[_shape(PET), _shape("python3 pet.py")],
        )
        result = await blast.process(
            _bash("cd skills/pet-cat && python3 pet.py status"), executor,
        )
        _assert_skipped(result, ran)

    async def test_an_unparseable_shape_covers_nothing(self, tmp_path):
        blast, executor, ran, _ = _setup(tmp_path, grants=[_shape(f"{PET} status; ls")])
        _assert_skipped(await blast.process(_bash(f"{PET} status; ls"), executor), ran)


class TestDenyAndCeilingsStillWin:
    async def test_absolute_paths_deny_on_a_shape_is_enforced(self, tmp_path):
        blast, executor, ran, _ = _setup(
            tmp_path, grants=[_shape(PET, absolute_paths="deny")],
        )
        result = await blast.process(_bash(f"{PET} status /etc/passwd"), executor)
        _assert_skipped(result, ran)

    async def test_max_calls_ceiling_is_enforced(self, tmp_path):
        blast, executor, ran, _ = _setup(tmp_path, grants=[_shape(PET, max_calls=1)])
        assert (await blast.process(_bash(f"{PET} status"), executor)).success
        second = await blast.process(_bash(f"{PET} play"), executor)
        assert ran == [f"{PET} status"]
        assert second.metadata.get("unattended_skip") is True

    async def test_critical_trust_is_never_covered_by_a_shape(self, tmp_path):
        blast, executor, ran, _ = _setup(tmp_path, grants=[_shape(PET)])
        result = await blast.process(
            _bash(f"{PET} status", trust=TrustLevel.CRITICAL), executor,
        )
        _assert_skipped(result, ran)

    async def test_scope_deny_verdict_beats_a_matching_shape(self, tmp_path):
        from loom.core.harness.scope import PermissionVerdict

        class _PolicyDeny(PermissionContext):
            def evaluate(self, request, trust_level):
                return PermissionVerdict.DENY

        blast, executor, ran, _ = _setup(tmp_path)
        perm = _PolicyDeny(session_id="t")
        perm.authorize("run_bash")
        perm.grant(_shape(PET))
        blast = BlastRadiusMiddleware(
            perm_ctx=perm, registry=blast._registry, confirm_fn=AsyncMock(),
        )
        result = await blast.process(_bash(f"{PET} status"), executor)
        assert ran == []
        assert result.error == "Permission denied by scope policy."
