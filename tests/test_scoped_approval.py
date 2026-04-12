"""
Tests for Issue #88 — Scoped Approval Leases (Phase A–C).

Phase A: Grant TTL + expiry filtering
Phase B: ConfirmDecision enum + middleware routing
Phase C: /scope command (structure tests only — no Rich console)

Coverage:
  1. ScopeGrant.valid_until field and default
  2. _effective_grants() filters expired grants
  3. purge_expired() removes expired from actual list
  4. ConfirmDecision enum values
  5. _normalize_decision() converts bool → ConfirmDecision
  6. Scope-aware path: ONCE/SCOPE/AUTO grant routing
  7. Legacy path: ConfirmDecision backward compat
  8. /scope command handler (list, revoke, clear)
"""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from loom.core.harness.middleware import (
    BlastRadiusMiddleware, ToolCall, ToolResult,
)
from loom.core.harness.permissions import (
    PermissionContext, ToolCapability, TrustLevel,
)
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.harness.scope import (
    ConfirmDecision, PermissionVerdict, ScopeGrant,
    ScopeRequirement, ScopeRequest,
)


_WORKSPACE = "/tmp/test_workspace"


# ── Helpers ─────────────────────────────────────────────────────────

def _call(tool_name, args=None, trust=TrustLevel.GUARDED,
          caps=ToolCapability.NONE, origin="interactive"):
    return ToolCall(
        tool_name=tool_name, args=args or {},
        trust_level=trust, session_id="test",
        capabilities=caps, origin=origin,
    )


def _ok_result(call):
    return ToolResult(
        call_id=call.id, tool_name=call.tool_name, success=True, output="ok",
    )


def _tool_def(name, trust=TrustLevel.GUARDED, caps=ToolCapability.NONE,
              scope_resolver=None):
    return ToolDefinition(
        name=name, description=f"test {name}",
        trust_level=trust, input_schema={},
        executor=AsyncMock(return_value=ToolResult(
            call_id="x", tool_name=name, success=True, output="ok",
        )),
        capabilities=caps, scope_resolver=scope_resolver,
    )


def _make_registry(*tool_defs):
    reg = ToolRegistry()
    for td in tool_defs:
        reg.register(td)
    return reg


async def _run_middleware(perm, call, confirm_result=True, registry=None):
    """Run BlastRadiusMiddleware. confirm_result can be bool or ConfirmDecision."""
    confirm_fn = AsyncMock(return_value=confirm_result)
    handler = AsyncMock(return_value=_ok_result(call))
    mw = BlastRadiusMiddleware(
        perm_ctx=perm, confirm_fn=confirm_fn, registry=registry,
    )
    result = await mw.process(call, handler)
    return result, confirm_fn, handler


# =====================================================================
# 1. Phase A — Grant TTL
# =====================================================================

class TestGrantTTL:
    def test_default_valid_until_is_zero(self):
        g = ScopeGrant(resource="path", action="write", selector="/doc")
        assert g.valid_until == 0.0

    def test_valid_until_can_be_set(self):
        future = time.time() + 3600
        g = ScopeGrant(resource="path", action="write", selector="/doc",
                       valid_until=future)
        assert g.valid_until == future


class TestEffectiveGrantsExpiry:
    def test_expired_grant_filtered(self):
        perm = PermissionContext(session_id="test")
        perm.grant(ScopeGrant(
            resource="path", action="write", selector="/doc",
            valid_until=time.time() - 10,  # expired 10s ago
        ))
        perm.grant(ScopeGrant(
            resource="path", action="read", selector="/src",
        ))  # no TTL, never expires
        effective = perm._effective_grants()
        assert len(effective) == 1
        assert effective[0].selector == "/src"

    def test_future_grant_retained(self):
        perm = PermissionContext(session_id="test")
        perm.grant(ScopeGrant(
            resource="exec", action="execute", selector="workspace",
            valid_until=time.time() + 3600,
        ))
        effective = perm._effective_grants()
        assert len(effective) == 1

    def test_zero_ttl_never_expires(self):
        perm = PermissionContext(session_id="test")
        perm.grant(ScopeGrant(
            resource="path", action="write", selector="/doc",
            valid_until=0.0,
        ))
        effective = perm._effective_grants()
        assert len(effective) == 1


class TestPurgeExpired:
    def test_purge_removes_expired(self):
        perm = PermissionContext(session_id="test")
        perm.grant(ScopeGrant(
            resource="path", action="write", selector="/doc",
            valid_until=time.time() - 10,
        ))
        perm.grant(ScopeGrant(
            resource="path", action="read", selector="/src",
        ))
        removed = perm.purge_expired()
        assert removed == 1
        assert len(perm.grants) == 1
        assert perm.grants[0].selector == "/src"

    def test_purge_nothing_expired(self):
        perm = PermissionContext(session_id="test")
        perm.grant(ScopeGrant(
            resource="path", action="write", selector="/doc",
        ))
        removed = perm.purge_expired()
        assert removed == 0
        assert len(perm.grants) == 1


class TestEffectiveGrantsSelfHeal:
    """_effective_grants() auto-purges expired grants to prevent _usage leak."""

    def test_effective_grants_purges_expired_and_usage(self):
        perm = PermissionContext(session_id="test")
        perm.grant(ScopeGrant(
            resource="path", action="write", selector="/doc",
            valid_until=time.time() - 10,
        ))
        perm.grant(ScopeGrant(
            resource="path", action="read", selector="/src",
        ))
        # Simulate usage tracking for expired grant
        perm._usage[0] = {"max_calls": 5}
        perm._usage[1] = {"max_calls": 2}

        # _effective_grants should auto-purge expired and remap _usage
        effective = perm._effective_grants()
        assert len(effective) == 1
        assert effective[0].selector == "/src"
        # _usage should have been remapped: old index 1 → new index 0
        assert 0 in perm._usage
        assert perm._usage[0] == {"max_calls": 2}


# =====================================================================
# 2. Phase B — ConfirmDecision
# =====================================================================

class TestConfirmDecisionEnum:
    def test_all_values_exist(self):
        assert ConfirmDecision.DENY.value == "deny"
        assert ConfirmDecision.ONCE.value == "once"
        assert ConfirmDecision.SCOPE.value == "scope"
        assert ConfirmDecision.AUTO.value == "auto"


class TestNormalizeDecision:
    def test_bool_true_becomes_once(self):
        d = BlastRadiusMiddleware._normalize_decision(True)
        assert d == ConfirmDecision.ONCE

    def test_bool_false_becomes_deny(self):
        d = BlastRadiusMiddleware._normalize_decision(False)
        assert d == ConfirmDecision.DENY

    def test_confirm_decision_passes_through(self):
        for cd in ConfirmDecision:
            assert BlastRadiusMiddleware._normalize_decision(cd) == cd


class TestScopeAwareDecisionRouting:
    """Scope-aware path grant behavior based on ConfirmDecision."""

    def _path_resolver(self, call):
        return ScopeRequest(
            tool_name=call.tool_name,
            capabilities=call.capabilities,
            requirements=[
                ScopeRequirement(
                    resource="path", action="write", selector="doc",
                    tool_name=call.tool_name, capabilities=call.capabilities,
                ),
            ],
        )

    async def test_once_creates_grant_no_ttl(self):
        perm = PermissionContext(session_id="test")
        reg = _make_registry(_tool_def(
            "write_file", caps=ToolCapability.MUTATES,
            scope_resolver=self._path_resolver,
        ))
        call = _call("write_file", {"path": "doc/x.md"}, caps=ToolCapability.MUTATES)
        result, _, _ = await _run_middleware(
            perm, call, confirm_result=ConfirmDecision.ONCE, registry=reg,
        )
        assert result.success
        assert len(perm.grants) >= 1
        g = perm.grants[-1]
        assert g.source == "manual_confirm"
        assert g.valid_until == 0.0  # no TTL

    async def test_scope_creates_lease_with_ttl(self):
        perm = PermissionContext(session_id="test")
        reg = _make_registry(_tool_def(
            "write_file", caps=ToolCapability.MUTATES,
            scope_resolver=self._path_resolver,
        ))
        call = _call("write_file", {"path": "doc/x.md"}, caps=ToolCapability.MUTATES)
        result, _, _ = await _run_middleware(
            perm, call, confirm_result=ConfirmDecision.SCOPE, registry=reg,
        )
        assert result.success
        assert len(perm.grants) >= 1
        g = perm.grants[-1]
        assert g.source == "lease"
        assert g.valid_until > 0  # has TTL
        assert g.valid_until > time.time()  # in the future

    async def test_auto_creates_grant_no_ttl_auto_source(self):
        perm = PermissionContext(session_id="test")
        reg = _make_registry(_tool_def(
            "write_file", caps=ToolCapability.MUTATES,
            scope_resolver=self._path_resolver,
        ))
        call = _call("write_file", {"path": "doc/x.md"}, caps=ToolCapability.MUTATES)
        result, _, _ = await _run_middleware(
            perm, call, confirm_result=ConfirmDecision.AUTO, registry=reg,
        )
        assert result.success
        assert len(perm.grants) >= 1
        g = perm.grants[-1]
        assert g.source == "auto_approve"
        assert g.valid_until == 0.0  # no TTL for auto

    async def test_deny_blocks_execution(self):
        perm = PermissionContext(session_id="test")
        reg = _make_registry(_tool_def(
            "write_file", caps=ToolCapability.MUTATES,
            scope_resolver=self._path_resolver,
        ))
        call = _call("write_file", {"path": "doc/x.md"}, caps=ToolCapability.MUTATES)
        result, _, handler = await _run_middleware(
            perm, call, confirm_result=ConfirmDecision.DENY, registry=reg,
        )
        assert not result.success
        assert result.failure_type == "permission_denied"
        handler.assert_not_called()

    async def test_confirm_decision_recorded_in_metadata(self):
        perm = PermissionContext(session_id="test")
        reg = _make_registry(_tool_def(
            "write_file", caps=ToolCapability.MUTATES,
            scope_resolver=self._path_resolver,
        ))
        call = _call("write_file", {"path": "doc/x.md"}, caps=ToolCapability.MUTATES)
        await _run_middleware(
            perm, call, confirm_result=ConfirmDecision.SCOPE, registry=reg,
        )
        assert call.metadata.get("confirm_decision") == "scope"


class TestLegacyPathDecision:
    """Legacy path (no scope_resolver) still works with bool and ConfirmDecision."""

    async def test_bool_true_still_works(self):
        perm = PermissionContext(session_id="test")
        call = _call("write_file", {"path": "x.py"})
        result, confirm_fn, handler = await _run_middleware(perm, call, confirm_result=True)
        assert result.success
        handler.assert_called_once()

    async def test_bool_false_still_denies(self):
        perm = PermissionContext(session_id="test")
        call = _call("write_file", {"path": "x.py"})
        result, _, handler = await _run_middleware(perm, call, confirm_result=False)
        assert not result.success
        handler.assert_not_called()

    async def test_confirm_decision_deny_works(self):
        perm = PermissionContext(session_id="test")
        call = _call("write_file", {"path": "x.py"})
        result, _, handler = await _run_middleware(
            perm, call, confirm_result=ConfirmDecision.DENY,
        )
        assert not result.success
        handler.assert_not_called()

    async def test_scope_decision_on_legacy_sets_fallback_metadata(self):
        """SCOPE/AUTO on legacy path records legacy_decision_fallback in metadata."""
        perm = PermissionContext(session_id="test")
        call = _call("write_file", {"path": "x.py"})
        result, _, _ = await _run_middleware(
            perm, call, confirm_result=ConfirmDecision.SCOPE,
        )
        assert result.success
        assert call.metadata.get("legacy_decision_fallback") is True

    async def test_auto_decision_on_legacy_sets_fallback_metadata(self):
        perm = PermissionContext(session_id="test")
        call = _call("write_file", {"path": "x.py"})
        result, _, _ = await _run_middleware(
            perm, call, confirm_result=ConfirmDecision.AUTO,
        )
        assert result.success
        assert call.metadata.get("legacy_decision_fallback") is True

    async def test_once_decision_on_legacy_no_fallback_metadata(self):
        perm = PermissionContext(session_id="test")
        call = _call("write_file", {"path": "x.py"})
        result, _, _ = await _run_middleware(
            perm, call, confirm_result=ConfirmDecision.ONCE,
        )
        assert result.success
        assert "legacy_decision_fallback" not in call.metadata


# =====================================================================
# 3. Phase C — /scope command structure
# =====================================================================

class TestScopeCommandHandler:
    """Test _handle_scope_command dispatching."""

    def _make_session(self, grants=None):
        session = MagicMock()
        session.perm = PermissionContext(session_id="test")
        for g in (grants or []):
            session.perm.grant(g)
        return session

    def test_list_empty(self):
        from loom.platform.cli.main import _handle_scope_command
        session = self._make_session()
        console = MagicMock()
        _handle_scope_command(session, "", console)
        console.print.assert_called()
        # Should mention "No active scope grants"
        printed = str(console.print.call_args_list)
        assert "No active" in printed

    def test_list_with_grants(self):
        from loom.platform.cli.main import _handle_scope_command
        session = self._make_session(grants=[
            ScopeGrant(resource="path", action="write", selector="/doc", source="lease"),
            ScopeGrant(resource="exec", action="execute", selector="workspace", source="exec_auto"),
        ])
        console = MagicMock()
        _handle_scope_command(session, "", console)
        # Should print a Rich Table (which calls console.print)
        console.print.assert_called()

    def test_revoke_specific(self):
        from loom.platform.cli.main import _handle_scope_command
        session = self._make_session(grants=[
            ScopeGrant(resource="path", action="write", selector="/doc"),
            ScopeGrant(resource="path", action="read", selector="/src"),
        ])
        console = MagicMock()
        _handle_scope_command(session, "revoke 0", console)
        assert len(session.perm.grants) == 1
        assert session.perm.grants[0].selector == "/src"

    def test_revoke_invalid_index(self):
        from loom.platform.cli.main import _handle_scope_command
        session = self._make_session(grants=[
            ScopeGrant(resource="path", action="write", selector="/doc"),
        ])
        console = MagicMock()
        _handle_scope_command(session, "revoke 5", console)
        # Grant should still be there
        assert len(session.perm.grants) == 1

    def test_clear_removes_non_system(self):
        from loom.platform.cli.main import _handle_scope_command
        session = self._make_session(grants=[
            ScopeGrant(resource="path", action="write", selector="/doc", source="lease"),
            ScopeGrant(resource="exec", action="execute", selector="workspace", source="system"),
        ])
        console = MagicMock()
        _handle_scope_command(session, "clear", console)
        assert len(session.perm.grants) == 1
        assert session.perm.grants[0].source == "system"

    def test_clear_preserves_exec_auto(self):
        from loom.platform.cli.main import _handle_scope_command
        session = self._make_session(grants=[
            ScopeGrant(resource="path", action="write", selector="/doc", source="lease"),
            ScopeGrant(resource="exec", action="execute", selector="workspace", source="exec_auto"),
            ScopeGrant(resource="exec", action="execute", selector="workspace", source="system"),
        ])
        console = MagicMock()
        _handle_scope_command(session, "clear", console)
        assert len(session.perm.grants) == 2
        sources = {g.source for g in session.perm.grants}
        assert sources == {"system", "exec_auto"}

    def test_purge_expired_on_list(self):
        from loom.platform.cli.main import _handle_scope_command
        session = self._make_session(grants=[
            ScopeGrant(resource="path", action="write", selector="/doc",
                       valid_until=time.time() - 10),
            ScopeGrant(resource="path", action="read", selector="/src"),
        ])
        console = MagicMock()
        _handle_scope_command(session, "", console)
        # Expired grant should have been purged
        assert len(session.perm.grants) == 1
