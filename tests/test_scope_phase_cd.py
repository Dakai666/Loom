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
        # Stateless helper extracted to session_support (re-exported by session).
        from loom.core.session import _format_scope_panel
        return _format_scope_panel

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

# TestExecAutoGrantInjection retired with /auto (#704 S3).


# TestInformationalConstraintFiltering: confirms create no grants since #704 S4.


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
# 6. ExecMatcher + workspace exec grant
# =====================================================================

class TestExecMatcherWithWorkspaceGrant:
    """Verify ExecMatcher handles a workspace exec grant."""

    def test_workspace_grant_covers_workspace_req(self):
        matcher = ExecMatcher()
        grant = ScopeGrant(
            resource="exec", action="execute", selector="workspace",
            constraints={"absolute_paths": "deny"}, source="autonomy:test",
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
            constraints={"absolute_paths": "deny"}, source="autonomy:test",
        )
        req = ScopeRequirement(
            resource="exec", action="execute", selector="workspace",
            constraints={"has_absolute_paths": True},
        )
        assert matcher.covers(grant, req) is False
