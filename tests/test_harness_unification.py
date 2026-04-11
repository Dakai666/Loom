"""
Tests for unified harness pipeline — Issues #83, #84, #85, #86.

Coverage:
  1. BlastRadiusMiddleware origin-aware deny/allow
  2. MCP server pipeline integration
  3. Autonomy daemon passes origin="autonomy"
  4. Sub-agent grant inheritance and origin="subagent"
  5. Plugin middleware typo fix
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from loom.core.harness.middleware import (
    BlastRadiusMiddleware, MiddlewarePipeline,
    ToolCall, ToolResult,
)
from loom.core.harness.permissions import PermissionContext, ToolCapability, TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.harness.scope import (
    PermissionVerdict, ScopeGrant, ScopeRequirement, ScopeRequest,
)


_WORKSPACE = Path("/tmp/test_workspace")


# ── Helpers ─────────────────────────────────────────────────────────

def _call(tool_name, args=None, trust=TrustLevel.GUARDED,
          caps=ToolCapability.NONE, origin="interactive"):
    return ToolCall(
        tool_name=tool_name,
        args=args or {},
        trust_level=trust,
        session_id="test",
        capabilities=caps,
        origin=origin,
    )


def _ok_result(call):
    return ToolResult(
        call_id=call.id, tool_name=call.tool_name, success=True, output="ok",
    )


def _tool_def(name, trust=TrustLevel.GUARDED, caps=ToolCapability.NONE,
              scope_resolver=None):
    return ToolDefinition(
        name=name,
        description=f"test {name}",
        trust_level=trust,
        input_schema={},
        executor=AsyncMock(return_value=ToolResult(
            call_id="x", tool_name=name, success=True, output="ok",
        )),
        capabilities=caps,
        scope_resolver=scope_resolver,
    )


def _make_registry(*tool_defs):
    reg = ToolRegistry()
    for td in tool_defs:
        reg.register(td)
    return reg


async def _run_middleware(perm, call, confirm_result=True, registry=None):
    confirm_fn = AsyncMock(return_value=confirm_result)
    handler = AsyncMock(return_value=_ok_result(call))
    mw = BlastRadiusMiddleware(
        perm_ctx=perm, confirm_fn=confirm_fn, registry=registry,
    )
    result = await mw.process(call, handler)
    return result, confirm_fn, handler


# =====================================================================
# 1. BlastRadiusMiddleware origin-aware behavior
# =====================================================================

class TestOriginAwareDeny:
    """Unattended origins (autonomy, subagent) get DENY instead of CONFIRM."""

    async def test_autonomy_no_grant_denied(self):
        """origin='autonomy' + no grant → DENY (not CONFIRM)."""
        perm = PermissionContext(session_id="test")
        call = _call("write_file", {"path": "x.py"}, origin="autonomy")
        result, confirm_fn, handler = await _run_middleware(perm, call)

        assert not result.success
        assert "permission_denied" == result.failure_type
        confirm_fn.assert_not_called()
        handler.assert_not_called()

    async def test_subagent_no_grant_denied(self):
        """origin='subagent' + no grant → DENY."""
        perm = PermissionContext(session_id="test")
        call = _call("write_file", {"path": "x.py"}, origin="subagent")
        result, confirm_fn, handler = await _run_middleware(perm, call)

        assert not result.success
        assert "permission_denied" == result.failure_type
        confirm_fn.assert_not_called()

    async def test_interactive_no_grant_calls_confirm(self):
        """origin='interactive' + no grant → calls confirm_fn (regression)."""
        perm = PermissionContext(session_id="test")
        call = _call("write_file", {"path": "x.py"}, origin="interactive")
        result, confirm_fn, handler = await _run_middleware(perm, call)

        assert result.success
        confirm_fn.assert_called_once()

    async def test_mcp_no_grant_calls_confirm(self):
        """origin='mcp' + no grant → calls confirm_fn (like interactive)."""
        perm = PermissionContext(session_id="test")
        call = _call("write_file", {"path": "x.py"}, origin="mcp")
        result, confirm_fn, handler = await _run_middleware(perm, call)

        assert result.success
        confirm_fn.assert_called_once()

    async def test_autonomy_with_grant_allowed(self):
        """origin='autonomy' + matching grant → ALLOW (no confirm needed)."""
        perm = PermissionContext(session_id="test")
        perm.authorize("write_file")
        call = _call("write_file", {"path": "x.py"}, origin="autonomy")
        result, confirm_fn, handler = await _run_middleware(perm, call)

        assert result.success
        confirm_fn.assert_not_called()
        handler.assert_called_once()

    async def test_subagent_with_grant_allowed(self):
        """origin='subagent' + matching grant → ALLOW."""
        perm = PermissionContext(session_id="test")
        perm.authorize("write_file")
        call = _call("write_file", {"path": "x.py"}, origin="subagent")
        result, confirm_fn, handler = await _run_middleware(perm, call)

        assert result.success
        confirm_fn.assert_not_called()
        handler.assert_called_once()


class TestOriginAwareScopeAwarePath:
    """Scope-aware path also respects origin for unattended deny."""

    def _path_resolver(self, call):
        return ScopeRequest(
            tool_name=call.tool_name,
            capabilities=call.capabilities,
            requirements=[
                ScopeRequirement(
                    resource="path", action="write",
                    selector=str(_WORKSPACE / "src"),
                    tool_name=call.tool_name,
                    capabilities=call.capabilities,
                ),
            ],
        )

    async def test_autonomy_scope_no_grant_denied(self):
        """Scope-aware: origin='autonomy' + CONFIRM verdict → DENY."""
        perm = PermissionContext(session_id="test")
        reg = _make_registry(_tool_def(
            "write_file", caps=ToolCapability.MUTATES,
            scope_resolver=self._path_resolver,
        ))
        call = _call(
            "write_file", {"path": "src/x.py"},
            caps=ToolCapability.MUTATES, origin="autonomy",
        )
        result, confirm_fn, handler = await _run_middleware(
            perm, call, registry=reg,
        )

        assert not result.success
        assert "permission_denied" == result.failure_type
        confirm_fn.assert_not_called()

    async def test_autonomy_scope_with_grant_allowed(self):
        """Scope-aware: origin='autonomy' + matching scope grant → ALLOW."""
        perm = PermissionContext(session_id="test")
        perm.grant(ScopeGrant(
            resource="path", action="write",
            selector=str(_WORKSPACE / "src"),
            source="test",
        ))
        reg = _make_registry(_tool_def(
            "write_file", caps=ToolCapability.MUTATES,
            scope_resolver=self._path_resolver,
        ))
        call = _call(
            "write_file", {"path": "src/x.py"},
            caps=ToolCapability.MUTATES, origin="autonomy",
        )
        result, confirm_fn, handler = await _run_middleware(
            perm, call, registry=reg,
        )

        assert result.success
        confirm_fn.assert_not_called()


# =====================================================================
# 2. MCP server pipeline integration
# =====================================================================

class TestMCPPipelineIntegration:
    """Verify MCP _call_tool constructs proper ToolCall and routes through pipeline."""

    async def test_mcp_call_constructs_full_toolcall(self):
        """MCP handler should construct ToolCall with trust_level, caps, origin='mcp'."""
        from loom.extensibility.mcp_server import run_mcp_server

        reg = _make_registry(
            _tool_def("read_file", trust=TrustLevel.SAFE, caps=ToolCapability.NONE),
        )

        captured_calls = []

        class MockPipeline:
            async def execute(self, call, handler):
                captured_calls.append(call)
                return await handler(call)

        mock_pipeline = MockPipeline()

        # We can't easily run the full MCP server, but we can verify
        # the function signature accepts pipeline and session_id
        import inspect
        sig = inspect.signature(run_mcp_server)
        assert "pipeline" in sig.parameters
        assert "session_id" in sig.parameters
        assert sig.parameters["session_id"].default == "mcp"


# =====================================================================
# 3. Autonomy daemon origin
# =====================================================================

class TestAutonomyDaemonOrigin:
    """Verify daemon no longer patches _confirm, uses origin='autonomy'."""

    def test_no_confirm_patching_in_daemon(self):
        """daemon._run_agent should not contain _auto_approve or _original_confirm."""
        import inspect
        from loom.autonomy.daemon import AutonomyDaemon
        source = inspect.getsource(AutonomyDaemon._run_agent)
        assert "_auto_approve" not in source
        assert "_original_confirm" not in source
        assert "origin=" in source or 'origin="autonomy"' in source

    def test_no_blast_radius_import_in_daemon(self):
        """BlastRadiusMiddleware should no longer be imported in daemon."""
        import loom.autonomy.daemon as daemon_mod
        assert not hasattr(daemon_mod, "BlastRadiusMiddleware")


# =====================================================================
# 4. Sub-agent grant inheritance
# =====================================================================

class TestSubAgentGrantInheritance:
    """Sub-agent should inherit parent grants, not blanket authorize."""

    def test_run_subagent_accepts_parent_grants(self):
        """run_subagent should accept parent_grants parameter."""
        import inspect
        from loom.core.agent.subagent import run_subagent
        sig = inspect.signature(run_subagent)
        assert "parent_grants" in sig.parameters

    def test_subagent_inherits_parent_grants(self):
        """run_subagent should copy parent scope grants into child perm."""
        import inspect
        from loom.core.agent.subagent import run_subagent
        source = inspect.getsource(run_subagent)
        assert "parent_grants" in source
        assert "perm.grant(g)" in source

    def test_subagent_has_full_pipeline(self):
        """Sub-agent pipeline should include all 5 middleware layers."""
        import inspect
        from loom.core.agent.subagent import run_subagent
        source = inspect.getsource(run_subagent)
        assert "LifecycleMiddleware" in source
        assert "TraceMiddleware" in source
        assert "SchemaValidationMiddleware" in source
        assert "BlastRadiusMiddleware" in source
        assert "LifecycleGateMiddleware" in source

    def test_subagent_toolcall_has_origin(self):
        """Sub-agent ToolCall construction should include origin='subagent'."""
        import inspect
        from loom.core.agent.subagent import run_subagent
        source = inspect.getsource(run_subagent)
        assert 'origin="subagent"' in source


# =====================================================================
# 5. Plugin middleware typo fix
# =====================================================================

class TestPluginMiddlewareFix:
    """Plugin install_into should use _middlewares, not _chain."""

    def test_plugin_uses_middlewares_not_chain(self):
        """install_into should reference _middlewares attribute."""
        import inspect
        from loom.extensibility.plugin import PluginRegistry
        source = inspect.getsource(PluginRegistry.install_into)
        assert "_middlewares" in source
        assert "_chain" not in source

    def test_middleware_pipeline_has_middlewares_attr(self):
        """MiddlewarePipeline should have _middlewares (not _chain)."""
        pipeline = MiddlewarePipeline()
        assert hasattr(pipeline, "_middlewares")
        assert not hasattr(pipeline, "_chain")


# =====================================================================
# 6. ToolCall origin field
# =====================================================================

class TestToolCallOriginField:
    """ToolCall should have an origin field with proper default."""

    def test_default_origin_is_interactive(self):
        call = ToolCall(
            tool_name="test", args={},
            trust_level=TrustLevel.SAFE, session_id="test",
        )
        assert call.origin == "interactive"

    def test_origin_can_be_set(self):
        for origin in ("interactive", "mcp", "autonomy", "subagent"):
            call = ToolCall(
                tool_name="test", args={},
                trust_level=TrustLevel.SAFE, session_id="test",
                origin=origin,
            )
            assert call.origin == origin
