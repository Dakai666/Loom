"""
Regression for the legacy-bypass / scope-grant interaction (issue #525,
Codex review of PR #527).

The bug: a tool pre-authorised by *name* (via the autonomy ``allowed_tools``
bridge → ``perm.authorize(tool)``) used to bypass scope evaluation on BOTH
``CONFIRM`` and ``EXPAND_SCOPE``. So a phase that declared
``allowed_tools = ["write_file"]`` *and* a bounding
``scope_grants = [{path, write, "autonomy/circadian"}]`` got a BLANKET
write_file — the grant's fence was silently void.

The fix: the name-level bypass applies only on ``CONFIRM`` (no scope_grant
declared for this resource → allowed_tools is the sole authorization). On
``EXPAND_SCOPE`` (a grant for this resource exists but the call exceeds its
selector) the scope is authoritative, so an out-of-scope call is denied even
when the tool name is pre-authorised.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from loom.core.harness.middleware import (
    BlastRadiusMiddleware,
    ToolCall,
    ToolResult,
)
from loom.core.harness.permissions import PermissionContext, ToolCapability, TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.harness.scope import ScopeGrant, ScopeRequest, ScopeRequirement


def _write_file_resolver(call: ToolCall) -> ScopeRequest:
    """Minimal write_file scope: a path/write requirement at args['path']."""
    return ScopeRequest(
        tool_name="write_file",
        capabilities=ToolCapability.MUTATES,
        requirements=[
            ScopeRequirement(
                resource="path",
                action="write",
                selector=call.args["path"],
                tool_name="write_file",
            )
        ],
    )


def _make_setup():
    handler_calls: list[str] = []

    async def handler(call: ToolCall) -> ToolResult:
        handler_calls.append(call.args.get("path", ""))
        return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True, output="ok")

    tool = ToolDefinition(
        name="write_file", description="", input_schema={},
        executor=handler, trust_level=TrustLevel.GUARDED,
        capabilities=ToolCapability.MUTATES, scope_resolver=_write_file_resolver,
    )
    reg = ToolRegistry()
    reg.register(tool)

    perm = PermissionContext(session_id="t")
    # A bounding grant: write_file may touch only autonomy/circadian …
    perm.grant(ScopeGrant(resource="path", action="write", selector="autonomy/circadian"))
    # … AND the tool is pre-authorised by name, simulating allowed_tools.
    perm.authorize("write_file")

    blast = BlastRadiusMiddleware(
        perm_ctx=perm, registry=reg, confirm_fn=AsyncMock(return_value=True),
    )
    return blast, handler, handler_calls


def _call(path: str) -> ToolCall:
    # origin="autonomy" → unattended: an out-of-scope call fails fast instead
    # of prompting (the circadian phase turn has no human to ask).
    return ToolCall(
        tool_name="write_file", args={"path": path},
        trust_level=TrustLevel.GUARDED, session_id="t", origin="autonomy",
    )


class TestScopeBoundsDespiteAllowedTools:
    async def test_in_scope_write_is_allowed(self):
        blast, _handler, calls = _make_setup()
        result = await blast.process(_call("autonomy/circadian/note.md"), _handler)
        assert result.success
        assert calls == ["autonomy/circadian/note.md"]

    async def test_out_of_scope_write_is_denied_even_when_name_authorized(self):
        # This is the regression: allowed_tools authorised write_file by name,
        # but the scope_grant fences it to autonomy/circadian — a write to
        # doc/outside.md must NOT slip through on the legacy bypass.
        blast, _handler, calls = _make_setup()
        result = await blast.process(_call("doc/outside.md"), _handler)
        assert not result.success
        assert result.failure_type == "permission_denied"
        assert calls == []  # handler never ran
