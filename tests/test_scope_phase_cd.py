"""
Tests for Issue #45 Phase C + D.

Phase C: Structured confirm (scope-aware CLI prompt)
Phase D: Legacy contraction (exec_auto → grant, impact_scope rename,
         informational constraint filtering, &&/|| scope_unknown)

Coverage:
  1. _format_scope_panel output for different verdicts/diffs
  2. _confirm_tool_cli panel styling (EXPAND_SCOPE vs CONFIRM)
  3. exec_auto grant injection and revocation
  4. Informational constraint filtering in _request_to_grants
  5. &&/|| detection in run_bash resolver
  6. impact_scope rename (ToolDefinition field)
  7. ExecMatcher + exec_auto grant integration
"""

import asyncio
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from loom.core.harness.middleware import BlastRadiusMiddleware, ToolCall, ToolResult
from loom.core.harness.permissions import PermissionContext, ToolCapability, TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.harness.scope import (
    DiffReason,
    ExecMatcher,
    PermissionVerdict,
    ScopeDiff,
    ScopeGrant,
    ScopeRequirement,
    ScopeRequest,
)
from loom.platform.cli.tools import (
    _make_run_bash_resolver,
    _make_write_file_resolver,
)


_WORKSPACE = Path("/tmp/test_workspace")


# ── Helpers ─────────────────────────────────────────────────────────

def _call(tool_name, args=None, trust=TrustLevel.GUARDED, caps=ToolCapability.NONE):
    return ToolCall(
        tool_name=tool_name,
        args=args or {},
        trust_level=trust,
        session_id="test",
        capabilities=caps,
    )


def _ok_result(call):
    return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True, output="ok")


def _make_registry(*tool_defs):
    reg = ToolRegistry()
    for td in tool_defs:
        reg.register(td)
    return reg


def _tool_def(name, trust=TrustLevel.GUARDED, caps=ToolCapability.NONE, scope_resolver=None):
    return ToolDefinition(
        name=name,
        description=f"test {name}",
        trust_level=trust,
        input_schema={},
        executor=AsyncMock(return_value=ToolResult(call_id="x", tool_name=name, success=True)),
        capabilities=caps,
        scope_resolver=scope_resolver,
    )


async def _run_middleware(perm, call, confirm_result=True, registry=None):
    confirm_fn = AsyncMock(return_value=confirm_result)
    handler = AsyncMock(return_value=_ok_result(call))
    mw = BlastRadiusMiddleware(
        perm_ctx=perm, confirm_fn=confirm_fn, registry=registry,
    )
    result = await mw.process(call, handler)
    return result, confirm_fn, handler


# =====================================================================
# 1. Phase C — _format_scope_panel
# =====================================================================

class TestFormatScopePanel:
    """Test _format_scope_panel produces correct Rich markup."""

    def _import_format(self):
        # Import the static method from LoomSession
        from loom.core.session import LoomSession
        return LoomSession._format_scope_panel

    def test_legacy_no_metadata(self):
        fmt = self._import_format()
        call = _call("write_file", {"path": "doc/test.md"}, caps=ToolCapability.MUTATES)
        # No scope metadata → legacy format
        result = fmt(call)
        assert "write_file" in result
        assert "path" in result
        assert "doc/test.md" in result

    def test_confirm_first_time(self):
        fmt = self._import_format()
        call = _call("write_file", {"path": "doc/test.md"}, caps=ToolCapability.MUTATES)
        call.metadata["scope_verdict"] = PermissionVerdict.CONFIRM
        call.metadata["scope_request"] = ScopeRequest(
            tool_name="write_file",
            capabilities=ToolCapability.MUTATES,
            requirements=[
                ScopeRequirement(
                    resource="path", action="write", selector="/tmp/test_workspace/doc",
                    tool_name="write_file", capabilities=ToolCapability.MUTATES,
                ),
            ],
        )
        call.metadata["scope_diff"] = ScopeDiff(
            missing=[call.metadata["scope_request"].requirements[0]],
            covered=[],
            reason=DiffReason.FIRST_TIME,
        )
        result = fmt(call)
        assert "path" in result
        assert "write" in result
        assert "/tmp/test_workspace/doc" in result
        assert "First time" in result

    def test_expand_scope_shows_reason(self):
        fmt = self._import_format()
        call = _call("write_file", {"path": "loom/core.py"}, caps=ToolCapability.MUTATES)
        existing_req = ScopeRequirement(
            resource="path", action="write", selector="/tmp/test_workspace/doc",
            tool_name="write_file", capabilities=ToolCapability.MUTATES,
        )
        new_req = ScopeRequirement(
            resource="path", action="write", selector="/tmp/test_workspace/loom",
            tool_name="write_file", capabilities=ToolCapability.MUTATES,
        )
        call.metadata["scope_verdict"] = PermissionVerdict.EXPAND_SCOPE
        call.metadata["scope_request"] = ScopeRequest(
            tool_name="write_file",
            capabilities=ToolCapability.MUTATES,
            requirements=[new_req],
        )
        call.metadata["scope_diff"] = ScopeDiff(
            missing=[new_req],
            covered=[existing_req],
            reason=DiffReason.SELECTOR_EXPANSION,
        )
        result = fmt(call)
        assert "Expanding beyond" in result
        assert "new:" in result
        assert "covered:" in result

    def test_scope_unknown_warning(self):
        fmt = self._import_format()
        call = _call("run_bash", {"command": "ls | grep foo"}, caps=ToolCapability.EXEC)
        call.metadata["scope_verdict"] = PermissionVerdict.CONFIRM
        call.metadata["scope_request"] = ScopeRequest(
            tool_name="run_bash",
            capabilities=ToolCapability.EXEC,
            requirements=[
                ScopeRequirement(
                    resource="exec", action="execute", selector="workspace",
                    constraints={"scope_unknown": True},
                    tool_name="run_bash", capabilities=ToolCapability.EXEC,
                ),
            ],
            metadata={"scope_unknown": True},
        )
        call.metadata["scope_diff"] = ScopeDiff(
            missing=[call.metadata["scope_request"].requirements[0]],
            covered=[], reason=DiffReason.FIRST_TIME,
        )
        result = fmt(call)
        assert "scope could not be fully resolved" in result


# =====================================================================
# 2. Phase D — exec_auto grant injection
# =====================================================================

class TestExecAutoGrantInjection:
    def test_enable_injects_grant(self):
        perm = PermissionContext(session_id="test")
        assert len(perm.grants) == 0
        perm.enable_exec_auto()
        assert perm.exec_auto is True
        assert len(perm.grants) == 1
        g = perm.grants[0]
        assert g.resource == "exec"
        assert g.action == "execute"
        assert g.selector == "workspace"
        assert g.constraints.get("absolute_paths") == "deny"
        assert g.source == "exec_auto"

    def test_disable_revokes_grant(self):
        perm = PermissionContext(session_id="test")
        perm.enable_exec_auto()
        assert len(perm.grants) == 1
        perm.disable_exec_auto()
        assert perm.exec_auto is False
        assert len(perm.grants) == 0

    def test_disable_only_revokes_exec_auto(self):
        perm = PermissionContext(session_id="test")
        # Add a manual grant first
        perm.grant(ScopeGrant(
            resource="path", action="write", selector="/tmp/doc",
            source="manual_confirm",
        ))
        perm.enable_exec_auto()
        assert len(perm.grants) == 2
        perm.disable_exec_auto()
        # Only the exec_auto grant should be removed
        assert len(perm.grants) == 1
        assert perm.grants[0].resource == "path"

    async def test_exec_auto_grant_allows_workspace_commands(self):
        """exec_auto grant should allow workspace-confined commands via scope-aware path."""
        perm = PermissionContext(session_id="test")
        perm.enable_exec_auto()

        reg = _make_registry(_tool_def(
            "run_bash", caps=ToolCapability.EXEC,
            scope_resolver=_make_run_bash_resolver(_WORKSPACE),
        ))
        call = _call("run_bash", {"command": "pytest tests/"}, caps=ToolCapability.EXEC)
        result, confirm_fn, handler = await _run_middleware(perm, call, registry=reg)

        assert result.success
        confirm_fn.assert_not_called()  # auto-approved via exec_auto grant
        handler.assert_called_once()

    async def test_exec_auto_denies_absolute_path_commands(self):
        """exec_auto grant has absolute_paths=deny — commands with abs paths need confirm."""
        perm = PermissionContext(session_id="test")
        perm.enable_exec_auto()

        reg = _make_registry(_tool_def(
            "run_bash", caps=ToolCapability.EXEC,
            scope_resolver=_make_run_bash_resolver(_WORKSPACE),
        ))
        call = _call("run_bash", {"command": "cat /etc/passwd"}, caps=ToolCapability.EXEC)
        result, confirm_fn, handler = await _run_middleware(
            perm, call, confirm_result=True, registry=reg,
        )

        assert result.success
        # Should need confirmation because absolute path escapes workspace
        confirm_fn.assert_called_once()


# =====================================================================
# 3. Phase D — Informational constraint filtering
# =====================================================================

class TestInformationalConstraintFiltering:
    async def test_scope_unknown_not_copied_to_grant(self):
        """scope_unknown is informational — should not be in grants."""
        perm = PermissionContext(session_id="test")
        reg = _make_registry(_tool_def(
            "run_bash", caps=ToolCapability.EXEC,
            scope_resolver=_make_run_bash_resolver(_WORKSPACE),
        ))
        call = _call("run_bash", {"command": "ls | grep foo"}, caps=ToolCapability.EXEC)
        await _run_middleware(perm, call, confirm_result=True, registry=reg)

        # Grant should exist but without scope_unknown
        assert len(perm.grants) >= 1
        for g in perm.grants:
            assert "scope_unknown" not in g.constraints

    async def test_has_absolute_paths_not_copied_to_grant(self):
        """has_absolute_paths is informational — should not be in grants."""
        perm = PermissionContext(session_id="test")
        reg = _make_registry(_tool_def(
            "run_bash", caps=ToolCapability.EXEC,
            scope_resolver=_make_run_bash_resolver(_WORKSPACE),
        ))
        call = _call("run_bash", {"command": "cat /etc/passwd"}, caps=ToolCapability.EXEC)
        await _run_middleware(perm, call, confirm_result=True, registry=reg)

        for g in perm.grants:
            assert "has_absolute_paths" not in g.constraints

    async def test_actionable_constraints_preserved(self):
        """remaining_budget and max_calls should be preserved in grants."""
        perm = PermissionContext(session_id="test")

        def _budget_resolver(call):
            return ScopeRequest(
                tool_name=call.tool_name,
                capabilities=call.capabilities,
                requirements=[
                    ScopeRequirement(
                        resource="agent", action="spawn", selector="default",
                        constraints={
                            "spawn_count": 1,
                            "remaining_budget": 3,
                            "scope_unknown": True,  # should be filtered
                        },
                        tool_name=call.tool_name, capabilities=call.capabilities,
                    ),
                ],
            )

        reg = _make_registry(_tool_def(
            "spawn_agent", caps=ToolCapability.AGENT_SPAN,
            scope_resolver=_budget_resolver,
        ))
        call = _call("spawn_agent", {"task": "x"}, caps=ToolCapability.AGENT_SPAN)
        await _run_middleware(perm, call, confirm_result=True, registry=reg)

        assert len(perm.grants) >= 1
        g = perm.grants[-1]
        assert g.constraints.get("remaining_budget") == 3
        assert g.constraints.get("spawn_count") == 1
        assert "scope_unknown" not in g.constraints


# =====================================================================
# 4. Phase D — &&/|| in scope_unknown patterns
# =====================================================================

_bash_resolver = _make_run_bash_resolver(_WORKSPACE)


class TestBashResolverExtendedPatterns:
    def test_and_chain_marks_scope_unknown(self):
        call = _call("run_bash", {"command": "cd /tmp && ls"}, caps=ToolCapability.EXEC)
        req = _bash_resolver(call)
        assert req.metadata.get("scope_unknown") is True

    def test_or_chain_marks_scope_unknown(self):
        call = _call("run_bash", {"command": "test -f x || echo missing"}, caps=ToolCapability.EXEC)
        req = _bash_resolver(call)
        assert req.metadata.get("scope_unknown") is True

    def test_simple_command_still_works(self):
        call = _call("run_bash", {"command": "pytest tests/"}, caps=ToolCapability.EXEC)
        req = _bash_resolver(call)
        assert not req.metadata.get("scope_unknown")


# =====================================================================
# 5. Phase D — impact_scope rename
# =====================================================================

class TestImpactScopeRename:
    def test_tool_definition_has_impact_scope(self):
        td = ToolDefinition(
            name="test",
            description="test",
            trust_level=TrustLevel.SAFE,
            input_schema={},
            executor=AsyncMock(),
            impact_scope="filesystem",
        )
        assert td.impact_scope == "filesystem"

    def test_default_impact_scope_is_general(self):
        td = ToolDefinition(
            name="test",
            description="test",
            trust_level=TrustLevel.SAFE,
            input_schema={},
            executor=AsyncMock(),
        )
        assert td.impact_scope == "general"

    def test_old_scope_field_does_not_exist(self):
        """The old 'scope' field should no longer exist on ToolDefinition."""
        td = ToolDefinition(
            name="test",
            description="test",
            trust_level=TrustLevel.SAFE,
            input_schema={},
            executor=AsyncMock(),
        )
        # If the old field still exists, this would return a non-default value
        # after setting it. Since it's removed, hasattr should be False for
        # 'scope' or it should not be in the dataclass fields.
        from dataclasses import fields
        field_names = {f.name for f in fields(td)}
        assert "scope" not in field_names


# =====================================================================
# 6. ExecMatcher + exec_auto grant integration
# =====================================================================

class TestExecMatcherWithExecAutoGrant:
    """Verify ExecMatcher correctly handles exec_auto grants."""

    def test_workspace_grant_covers_workspace_req(self):
        matcher = ExecMatcher()
        grant = ScopeGrant(
            resource="exec", action="execute", selector="workspace",
            constraints={"absolute_paths": "deny"}, source="exec_auto",
        )
        req = ScopeRequirement(
            resource="exec", action="execute", selector="workspace",
            constraints={"has_absolute_paths": False},
        )
        assert matcher.covers(grant, req) is True

    def test_workspace_grant_denies_absolute_paths(self):
        matcher = ExecMatcher()
        grant = ScopeGrant(
            resource="exec", action="execute", selector="workspace",
            constraints={"absolute_paths": "deny"}, source="exec_auto",
        )
        req = ScopeRequirement(
            resource="exec", action="execute", selector="workspace",
            constraints={"has_absolute_paths": True},
        )
        assert matcher.covers(grant, req) is False
