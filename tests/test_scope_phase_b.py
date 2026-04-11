"""
Tests for Issue #45 Phase B — scope resolvers + BlastRadiusMiddleware scope-aware path.

Coverage:
  1. Resolver output correctness (write_file, run_bash, fetch_url, web_search, spawn_agent)
  2. BlastRadiusMiddleware scope-aware path: ALLOW, CONFIRM, EXPAND_SCOPE, DENY
  3. Legacy fallback for tools without scope_resolver
  4. Scope metadata written to call.metadata
  5. Grant creation after user confirms
  6. Resolver error fallback to legacy
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from loom.core.harness.middleware import BlastRadiusMiddleware, ToolCall, ToolResult
from loom.core.harness.permissions import PermissionContext, ToolCapability, TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.harness.scope import (
    DiffReason,
    PermissionVerdict,
    ScopeGrant,
    ScopeRequirement,
    ScopeRequest,
)
from loom.platform.cli.tools import (
    _fetch_url_resolver,
    _make_run_bash_resolver,
    _make_write_file_resolver,
    _spawn_agent_resolver,
    _web_search_resolver,
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
    """Run BlastRadiusMiddleware on a call and return the result."""
    confirm_fn = AsyncMock(return_value=confirm_result)
    handler = AsyncMock(return_value=_ok_result(call))
    mw = BlastRadiusMiddleware(
        perm_ctx=perm, confirm_fn=confirm_fn, registry=registry,
    )
    result = await mw.process(call, handler)
    return result, confirm_fn, handler


# =====================================================================
# 1. Resolver output correctness
# =====================================================================

_write_resolver = _make_write_file_resolver(_WORKSPACE)
_bash_resolver = _make_run_bash_resolver(_WORKSPACE)


class TestWriteFileResolver:
    def test_relative_path(self):
        call = _call("write_file", {"path": "doc/test.md"}, caps=ToolCapability.MUTATES)
        req = _write_resolver(call)
        assert req.tool_name == "write_file"
        assert len(req.requirements) == 1
        r = req.requirements[0]
        assert r.resource == "path"
        assert r.action == "write"
        # Selector should be canonical parent directory
        assert ".." not in r.selector
        assert "doc" in r.selector

    def test_absolute_path(self):
        call = _call("write_file", {"path": "/etc/passwd"}, caps=ToolCapability.MUTATES)
        req = _write_resolver(call)
        r = req.requirements[0]
        assert r.resource == "path"
        assert r.action == "write"
        assert "/etc" in r.selector


class TestRunBashResolver:
    def test_simple_command(self):
        call = _call("run_bash", {"command": "ls -la"}, caps=ToolCapability.EXEC)
        req = _bash_resolver(call)
        assert len(req.requirements) == 1
        r = req.requirements[0]
        assert r.resource == "exec"
        assert r.action == "execute"
        assert r.selector == "workspace"
        assert not r.constraints.get("has_absolute_paths")

    def test_absolute_path_detected(self):
        call = _call("run_bash", {"command": "cat /etc/passwd"}, caps=ToolCapability.EXEC)
        req = _bash_resolver(call)
        r = req.requirements[0]
        assert r.constraints.get("has_absolute_paths") is True

    def test_pipe_marks_scope_unknown(self):
        call = _call("run_bash", {"command": "ls | grep foo"}, caps=ToolCapability.EXEC)
        req = _bash_resolver(call)
        assert req.metadata.get("scope_unknown") is True
        assert req.requirements[0].constraints.get("scope_unknown") is True

    def test_subshell_marks_scope_unknown(self):
        call = _call("run_bash", {"command": "echo $(whoami)"}, caps=ToolCapability.EXEC)
        req = _bash_resolver(call)
        assert req.metadata.get("scope_unknown") is True

    def test_variable_expansion_marks_scope_unknown(self):
        call = _call("run_bash", {"command": "cat $HOME/.bashrc"}, caps=ToolCapability.EXEC)
        req = _bash_resolver(call)
        assert req.metadata.get("scope_unknown") is True

    def test_workspace_relative_command(self):
        call = _call("run_bash", {"command": "pytest tests/"}, caps=ToolCapability.EXEC)
        req = _bash_resolver(call)
        r = req.requirements[0]
        assert r.selector == "workspace"
        assert not r.constraints.get("has_absolute_paths")
        assert not req.metadata.get("scope_unknown")


class TestFetchUrlResolver:
    def test_extracts_domain(self):
        call = _call("fetch_url", {"url": "https://docs.python.org/3/library/pathlib.html"})
        req = _fetch_url_resolver(call)
        assert req.requirements[0].resource == "network"
        assert req.requirements[0].action == "connect"
        assert req.requirements[0].selector == "docs.python.org"

    def test_empty_url(self):
        call = _call("fetch_url", {"url": ""})
        req = _fetch_url_resolver(call)
        assert req.requirements[0].selector == ""


class TestWebSearchResolver:
    def test_always_brave_domain(self):
        call = _call("web_search", {"query": "python pathlib"})
        req = _web_search_resolver(call)
        assert req.requirements[0].selector == "api.search.brave.com"


class TestSpawnAgentResolver:
    def test_default_spawn(self):
        call = _call("spawn_agent", {"task": "do something"}, caps=ToolCapability.AGENT_SPAN)
        req = _spawn_agent_resolver(call)
        assert req.requirements[0].resource == "agent"
        assert req.requirements[0].action == "spawn"
        assert req.requirements[0].constraints.get("spawn_count") == 1


# =====================================================================
# 2. BlastRadiusMiddleware scope-aware path
# =====================================================================

class TestBlastRadiusScopeAware:
    """Test the scope-aware authorization path in BlastRadiusMiddleware."""

    def _setup(self, grants=None, scope_resolver=None):
        perm = PermissionContext(session_id="test")
        if grants:
            for g in grants:
                perm.grant(g)
        reg = _make_registry(_tool_def(
            "write_file", caps=ToolCapability.MUTATES,
            scope_resolver=scope_resolver or _make_write_file_resolver(_WORKSPACE),
        ))
        return perm, reg

    async def test_allow_when_scope_covered(self):
        perm, reg = self._setup(grants=[
            ScopeGrant(resource="path", action="write",
                       selector=str((_WORKSPACE / "doc").resolve())),
        ])
        call = _call("write_file", {"path": "doc/test.md"}, caps=ToolCapability.MUTATES)
        result, confirm_fn, handler = await _run_middleware(perm, call, registry=reg)

        assert result.success
        confirm_fn.assert_not_called()  # no prompt needed
        handler.assert_called_once()
        assert call.metadata.get("scope_verdict") == PermissionVerdict.ALLOW

    async def test_confirm_first_time(self):
        perm, reg = self._setup()
        call = _call("write_file", {"path": "doc/test.md"}, caps=ToolCapability.MUTATES)
        result, confirm_fn, handler = await _run_middleware(
            perm, call, confirm_result=True, registry=reg,
        )

        assert result.success
        confirm_fn.assert_called_once()
        handler.assert_called_once()
        assert call.metadata["scope_verdict"] == PermissionVerdict.CONFIRM

    async def test_expand_scope_prompted(self):
        perm, reg = self._setup(grants=[
            ScopeGrant(resource="path", action="write",
                       selector=str((_WORKSPACE / "doc").resolve())),
        ])
        # Write to loom/ → outside existing doc/ grant → EXPAND_SCOPE
        call = _call("write_file", {"path": "loom/core.py"}, caps=ToolCapability.MUTATES)
        result, confirm_fn, handler = await _run_middleware(
            perm, call, confirm_result=True, registry=reg,
        )

        assert result.success
        confirm_fn.assert_called_once()
        assert call.metadata["scope_verdict"] == PermissionVerdict.EXPAND_SCOPE

    async def test_user_denies_scope_confirm(self):
        perm, reg = self._setup()
        call = _call("write_file", {"path": "doc/test.md"}, caps=ToolCapability.MUTATES)
        result, confirm_fn, handler = await _run_middleware(
            perm, call, confirm_result=False, registry=reg,
        )

        assert not result.success
        assert result.failure_type == "permission_denied"
        handler.assert_not_called()
        assert call.metadata.get("user_decision") is False

    async def test_grants_created_after_confirm(self):
        perm, reg = self._setup()
        call = _call("write_file", {"path": "doc/test.md"}, caps=ToolCapability.MUTATES)
        await _run_middleware(perm, call, confirm_result=True, registry=reg)

        # After confirmation, a grant should have been added
        assert len(perm.grants) >= 1
        g = perm.grants[-1]
        assert g.resource == "path"
        assert g.action == "write"
        assert g.source == "manual_confirm"

    async def test_second_call_same_scope_auto_allowed(self):
        perm, reg = self._setup()

        # First call — needs confirmation
        call1 = _call("write_file", {"path": "doc/a.md"}, caps=ToolCapability.MUTATES)
        _, confirm1, _ = await _run_middleware(perm, call1, confirm_result=True, registry=reg)
        confirm1.assert_called_once()

        # Second call — same scope, should be auto-allowed
        call2 = _call("write_file", {"path": "doc/b.md"}, caps=ToolCapability.MUTATES)
        _, confirm2, handler2 = await _run_middleware(perm, call2, registry=reg)
        confirm2.assert_not_called()
        handler2.assert_called_once()


# =====================================================================
# 3. Legacy fallback
# =====================================================================

class TestBlastRadiusLegacyFallback:
    """Tools without scope_resolver use the legacy tool-name authorization."""

    async def test_no_resolver_uses_legacy(self):
        perm = PermissionContext(session_id="test")
        reg = _make_registry(_tool_def("custom_tool", scope_resolver=None))
        call = _call("custom_tool")

        result, confirm_fn, handler = await _run_middleware(
            perm, call, confirm_result=True, registry=reg,
        )

        assert result.success
        confirm_fn.assert_called_once()
        # Legacy path: tool should be pre-authorized now
        assert perm.is_authorized("custom_tool", TrustLevel.GUARDED)
        # No scope metadata
        assert "scope_request" not in call.metadata

    async def test_no_registry_uses_legacy(self):
        perm = PermissionContext(session_id="test")
        call = _call("write_file")

        result, confirm_fn, handler = await _run_middleware(
            perm, call, confirm_result=True, registry=None,
        )

        assert result.success
        confirm_fn.assert_called_once()

    async def test_safe_tool_no_resolver_passes(self):
        perm = PermissionContext(session_id="test")
        reg = _make_registry(_tool_def("read_file", trust=TrustLevel.SAFE, scope_resolver=None))
        call = _call("read_file", trust=TrustLevel.SAFE)

        result, confirm_fn, handler = await _run_middleware(perm, call, registry=reg)

        assert result.success
        confirm_fn.assert_not_called()


# =====================================================================
# 4. Scope metadata in call.metadata
# =====================================================================

class TestScopeMetadata:
    async def test_scope_fields_written(self):
        perm = PermissionContext(session_id="test")
        reg = _make_registry(_tool_def(
            "write_file", caps=ToolCapability.MUTATES,
            scope_resolver=_make_write_file_resolver(_WORKSPACE),
        ))
        call = _call("write_file", {"path": "doc/test.md"}, caps=ToolCapability.MUTATES)
        await _run_middleware(perm, call, confirm_result=True, registry=reg)

        assert "scope_request" in call.metadata
        assert "scope_diff" in call.metadata
        assert "scope_verdict" in call.metadata
        assert isinstance(call.metadata["scope_request"], ScopeRequest)
        assert isinstance(call.metadata["scope_verdict"], PermissionVerdict)

    async def test_user_decision_recorded_on_deny(self):
        perm = PermissionContext(session_id="test")
        reg = _make_registry(_tool_def(
            "write_file", caps=ToolCapability.MUTATES,
            scope_resolver=_make_write_file_resolver(_WORKSPACE),
        ))
        call = _call("write_file", {"path": "doc/test.md"}, caps=ToolCapability.MUTATES)
        await _run_middleware(perm, call, confirm_result=False, registry=reg)

        assert call.metadata.get("user_decision") is False

    async def test_user_decision_recorded_on_confirm(self):
        perm = PermissionContext(session_id="test")
        reg = _make_registry(_tool_def(
            "write_file", caps=ToolCapability.MUTATES,
            scope_resolver=_make_write_file_resolver(_WORKSPACE),
        ))
        call = _call("write_file", {"path": "doc/test.md"}, caps=ToolCapability.MUTATES)
        await _run_middleware(perm, call, confirm_result=True, registry=reg)

        assert call.metadata.get("user_decision") is True


# =====================================================================
# 5. Resolver error fallback
# =====================================================================

class TestResolverErrorFallback:
    async def test_resolver_raises_falls_back_to_legacy(self):
        def _broken_resolver(call):
            raise RuntimeError("resolver crashed")

        perm = PermissionContext(session_id="test")
        reg = _make_registry(_tool_def(
            "write_file", caps=ToolCapability.MUTATES,
            scope_resolver=_broken_resolver,
        ))
        call = _call("write_file", {"path": "doc/test.md"}, caps=ToolCapability.MUTATES)
        result, confirm_fn, handler = await _run_middleware(
            perm, call, confirm_result=True, registry=reg,
        )

        # Should succeed via legacy path
        assert result.success
        confirm_fn.assert_called_once()
        # Legacy pre-authorization (MUTATES but not EXEC/AGENT_SPAN)
        assert perm.is_authorized("write_file", TrustLevel.GUARDED)
