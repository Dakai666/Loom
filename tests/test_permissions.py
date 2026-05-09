"""
Tests for loom.core.harness.permissions — TrustLevel, ToolCapability, PermissionContext.

Covers Phase 1 fragility fix from issue #347.
"""

from __future__ import annotations

import time

import pytest

from loom.core.harness.permissions import (
    ToolCapability,
    TrustLevel,
    PermissionContext,
)
from loom.core.harness.scope import (
    ScopeGrant,
    ScopeRequest,
    ScopeRequirement,
    PermissionVerdict,
    DiffReason,
)


# ===========================================================================
# TrustLevel
# ===========================================================================

class TestTrustLevel:
    def test_safe_value(self):
        assert TrustLevel.SAFE.value == "safe"

    def test_guarded_value(self):
        assert TrustLevel.GUARDED.value == "guarded"

    def test_critical_value(self):
        assert TrustLevel.CRITICAL.value == "critical"

    def test_plain_uppercase(self):
        assert TrustLevel.SAFE.plain == "SAFE"
        assert TrustLevel.GUARDED.plain == "GUARDED"
        assert TrustLevel.CRITICAL.plain == "CRITICAL"

    def test_display_plain_alias(self):
        assert TrustLevel.SAFE.display_plain == "SAFE"
        assert TrustLevel.GUARDED.display_plain == "GUARDED"

    def test_label_contains_rich_markup(self):
        label = TrustLevel.SAFE.label
        assert "green" in label
        assert "SAFE" in label

    def test_display_rich_alias(self):
        assert TrustLevel.SAFE.display_rich == TrustLevel.SAFE.label

    def test_critical_label_contains_red(self):
        label = TrustLevel.CRITICAL.label
        assert "red" in label
        assert "CRITICAL" in label

    def test_all_levels_distinct(self):
        levels = {TrustLevel.SAFE, TrustLevel.GUARDED, TrustLevel.CRITICAL}
        plains = {l.plain for l in levels}
        assert len(plains) == 3


# ===========================================================================
# ToolCapability
# ===========================================================================

class TestToolCapability:
    def test_default_is_none(self):
        assert ToolCapability.NONE == ToolCapability(0)

    def test_flags_are_unique_powers_of_two(self):
        flags = [
            ToolCapability.EXEC,
            ToolCapability.NETWORK,
            ToolCapability.AGENT_SPAN,
            ToolCapability.MUTATES,
            ToolCapability.READ_PROBE,
        ]
        seen = set()
        for f in flags:
            assert f.value not in seen, f"{f} value collision"
            seen.add(f.value)

    def test_or_combines_flags(self):
        combined = ToolCapability.EXEC | ToolCapability.NETWORK
        assert combined & ToolCapability.EXEC
        assert combined & ToolCapability.NETWORK
        assert not (combined & ToolCapability.MUTATES)

    def test_and_checks_membership(self):
        assert ToolCapability.EXEC & ToolCapability.EXEC
        assert not (ToolCapability.EXEC & ToolCapability.NETWORK)

    def test_multiple_flags_combined(self):
        combo = ToolCapability.EXEC | ToolCapability.MUTATES | ToolCapability.NETWORK
        assert combo & ToolCapability.EXEC
        assert combo & ToolCapability.MUTATES
        assert combo & ToolCapability.NETWORK
        assert not (combo & ToolCapability.AGENT_SPAN)

    def test_none_and_anything_is_none(self):
        assert not (ToolCapability.NONE & ToolCapability.EXEC)
        assert (ToolCapability.NONE | ToolCapability.EXEC) == ToolCapability.EXEC


# ===========================================================================
# PermissionContext — legacy API
# ===========================================================================

class TestPermissionContextLegacy:
    def test_new_context_has_no_authorizations(self):
        ctx = PermissionContext(session_id="s1")
        assert ctx.session_authorized == set()
        assert ctx.exec_auto is False

    def test_authorize_adds_tool(self):
        ctx = PermissionContext(session_id="s1")
        ctx.authorize("write_file")
        assert "write_file" in ctx.session_authorized

    def test_revoke_removes_tool(self):
        ctx = PermissionContext(session_id="s1")
        ctx.authorize("write_file")
        ctx.revoke("write_file")
        assert "write_file" not in ctx.session_authorized

    def test_revoke_missing_is_noop(self):
        ctx = PermissionContext(session_id="s1")
        ctx.revoke("nonexistent")

    def test_is_authorized_safe_always_true(self):
        ctx = PermissionContext(session_id="s1")
        assert ctx.is_authorized("any_tool", TrustLevel.SAFE) is True

    def test_is_authorized_guarded_requires_auth(self):
        ctx = PermissionContext(session_id="s1")
        assert ctx.is_authorized("write_file", TrustLevel.GUARDED) is False
        ctx.authorize("write_file")
        assert ctx.is_authorized("write_file", TrustLevel.GUARDED) is True

    def test_is_authorized_critical_never_pre_authorized(self):
        ctx = PermissionContext(session_id="s1")
        ctx.authorize("dangerous_tool")
        assert ctx.is_authorized("dangerous_tool", TrustLevel.CRITICAL) is False

    def test_enable_exec_auto(self):
        ctx = PermissionContext(session_id="s1")
        ctx.enable_exec_auto()
        assert ctx.exec_auto is True
        assert any(g.source == "exec_auto" for g in ctx.grants)

    def test_disable_exec_auto(self):
        ctx = PermissionContext(session_id="s1")
        ctx.enable_exec_auto()
        ctx.disable_exec_auto()
        assert ctx.exec_auto is False
        assert not any(g.source == "exec_auto" for g in ctx.grants)

    def test_multiple_authorizations(self):
        ctx = PermissionContext(session_id="s1")
        ctx.authorize("a")
        ctx.authorize("b")
        ctx.authorize("c")
        assert ctx.session_authorized == {"a", "b", "c"}
        ctx.revoke("b")
        assert ctx.session_authorized == {"a", "c"}


# ===========================================================================
# PermissionContext — scope-aware API
# ===========================================================================

class TestPermissionContextScope:
    def test_grant_adds_to_list(self):
        ctx = PermissionContext(session_id="s1")
        g = ScopeGrant(resource="path", action="read", selector="/ws/")
        ctx.grant(g)
        assert len(ctx.grants) == 1
        assert ctx.grants[0].resource == "path"

    def test_grant_many_adds_all(self):
        ctx = PermissionContext(session_id="s1")
        grants = [
            ScopeGrant(resource="path", action="read", selector="/ws/"),
            ScopeGrant(resource="network", action="connect", selector="api.example.com"),
        ]
        ctx.grant_many(grants)
        assert len(ctx.grants) == 2

    def test_revoke_matching_removes_by_predicate(self):
        ctx = PermissionContext(session_id="s1")
        ctx.grant(ScopeGrant(resource="path", action="read", selector="/ws/a"))
        ctx.grant(ScopeGrant(resource="path", action="read", selector="/ws/b"))
        ctx.revoke_matching(lambda g: g.selector == "/ws/a")
        assert len(ctx.grants) == 1
        assert ctx.grants[0].selector == "/ws/b"

    def test_purge_expired_removes_expired(self):
        ctx = PermissionContext(session_id="s1")
        expired = ScopeGrant(
            resource="path", action="read", selector="/ws/",
            valid_until=time.time() - 10,
        )
        active = ScopeGrant(
            resource="path", action="read", selector="/ws/",
            valid_until=time.time() + 3600,
        )
        ctx.grant(expired)
        ctx.grant(active)
        removed = ctx.purge_expired()
        assert removed == 1
        assert len(ctx.grants) == 1

    def test_purge_expired_no_expired_returns_zero(self):
        ctx = PermissionContext(session_id="s1")
        ctx.grant(ScopeGrant(resource="path", action="read", selector="/ws/"))
        removed = ctx.purge_expired()
        assert removed == 0

    def test_evaluate_safe_always_allow(self):
        ctx = PermissionContext(session_id="s1")
        req = ScopeRequest(tool_name="read_file", capabilities=ToolCapability.NONE, requirements=[])
        verdict = ctx.evaluate(req, TrustLevel.SAFE)
        assert verdict == PermissionVerdict.ALLOW

    def test_evaluate_critical_returns_confirm_when_covered(self):
        ctx = PermissionContext(session_id="s1")
        ctx.grant(ScopeGrant(resource="path", action="write", selector="/ws/"))
        req = ScopeRequest(
            tool_name="rm_rf",
            capabilities=ToolCapability.MUTATES,
            requirements=[
                ScopeRequirement(resource="path", action="write", selector="/ws/tmp.txt"),
            ],
        )
        verdict = ctx.evaluate(req, TrustLevel.CRITICAL)
        assert verdict == PermissionVerdict.CONFIRM

    def test_evaluate_critical_first_time_returns_confirm(self):
        ctx = PermissionContext(session_id="s1")
        req = ScopeRequest(
            tool_name="rm_rf",
            capabilities=ToolCapability.MUTATES,
            requirements=[
                ScopeRequirement(resource="path", action="write", selector="/ws/tmp.txt"),
            ],
        )
        verdict = ctx.evaluate(req, TrustLevel.CRITICAL)
        assert verdict == PermissionVerdict.CONFIRM

    def test_evaluate_guarded_with_matching_grant_allow(self):
        ctx = PermissionContext(session_id="s1")
        ctx.grant(ScopeGrant(resource="path", action="write", selector="/ws/"))
        req = ScopeRequest(
            tool_name="write_file",
            capabilities=ToolCapability.MUTATES,
            requirements=[
                ScopeRequirement(resource="path", action="write", selector="/ws/output.txt"),
            ],
        )
        verdict = ctx.evaluate(req, TrustLevel.GUARDED)
        assert verdict == PermissionVerdict.ALLOW

    def test_evaluate_guarded_no_grant_confirm(self):
        ctx = PermissionContext(session_id="s1")
        req = ScopeRequest(
            tool_name="write_file",
            capabilities=ToolCapability.MUTATES,
            requirements=[
                ScopeRequirement(resource="path", action="write", selector="/ws/output.txt"),
            ],
        )
        verdict = ctx.evaluate(req, TrustLevel.GUARDED)
        assert verdict == PermissionVerdict.CONFIRM

    def test_evaluate_guarded_expand_scope(self):
        """Grant for /ws/ but request targets /etc/ → SELECTOR_EXPANSION."""
        ctx = PermissionContext(session_id="s1")
        ctx.grant(ScopeGrant(resource="path", action="write", selector="/ws/"))
        req = ScopeRequest(
            tool_name="write_file",
            capabilities=ToolCapability.MUTATES,
            requirements=[
                ScopeRequirement(resource="path", action="write", selector="/etc/config.txt"),
            ],
        )
        verdict = ctx.evaluate(req, TrustLevel.GUARDED)
        assert verdict == PermissionVerdict.EXPAND_SCOPE

    def test_diff_fully_covered(self):
        ctx = PermissionContext(session_id="s1")
        ctx.grant(ScopeGrant(resource="path", action="read", selector="/ws/"))
        req = ScopeRequest(
            tool_name="read_file",
            capabilities=ToolCapability.NONE,
            requirements=[
                ScopeRequirement(resource="path", action="read", selector="/ws/doc/report.md"),
            ],
        )
        diff = ctx.diff(req)
        assert diff.is_fully_covered
        assert diff.reason == DiffReason.FULLY_COVERED

    def test_diff_missing(self):
        ctx = PermissionContext(session_id="s1")
        req = ScopeRequest(
            tool_name="read_file",
            capabilities=ToolCapability.NONE,
            requirements=[
                ScopeRequirement(resource="path", action="read", selector="/ws/doc/report.md"),
            ],
        )
        diff = ctx.diff(req)
        assert not diff.is_fully_covered
        assert len(diff.missing) == 1

    def test_consumable_budget_consumption(self):
        ctx = PermissionContext(session_id="s1")
        ctx.grant(ScopeGrant(
            resource="agent", action="spawn", selector="*",
            constraints={"remaining_budget": 3},
        ))
        req = ScopeRequest(
            tool_name="spawn_agent",
            capabilities=ToolCapability.AGENT_SPAN,
            requirements=[
                ScopeRequirement(
                    resource="agent", action="spawn", selector="default",
                    constraints={"spawn_count": 1},
                ),
            ],
        )
        v1 = ctx.evaluate(req, TrustLevel.GUARDED)
        assert v1 == PermissionVerdict.ALLOW
        effective = ctx._effective_grants()
        assert effective[0].constraints["remaining_budget"] == 2

        v2 = ctx.evaluate(req, TrustLevel.GUARDED)
        assert v2 == PermissionVerdict.ALLOW
        effective2 = ctx._effective_grants()
        assert effective2[0].constraints["remaining_budget"] == 1

    def test_consumable_budget_exhausted(self):
        ctx = PermissionContext(session_id="s1")
        ctx.grant(ScopeGrant(
            resource="agent", action="spawn", selector="*",
            constraints={"remaining_budget": 1},
        ))
        req = ScopeRequest(
            tool_name="spawn_agent",
            capabilities=ToolCapability.AGENT_SPAN,
            requirements=[
                ScopeRequirement(
                    resource="agent", action="spawn", selector="default",
                    constraints={"spawn_count": 1},
                ),
            ],
        )
        ctx.evaluate(req, TrustLevel.GUARDED)
        v2 = ctx.evaluate(req, TrustLevel.GUARDED)
        assert v2 != PermissionVerdict.ALLOW

    def test_multiple_grants_different_resources(self):
        ctx = PermissionContext(session_id="s1")
        ctx.grant(ScopeGrant(resource="path", action="read", selector="/ws/"))
        ctx.grant(ScopeGrant(resource="network", action="connect", selector="api.github.com"))
        req = ScopeRequest(
            tool_name="fetch_url",
            capabilities=ToolCapability.NETWORK,
            requirements=[
                ScopeRequirement(resource="network", action="connect", selector="api.github.com"),
            ],
        )
        verdict = ctx.evaluate(req, TrustLevel.GUARDED)
        assert verdict == PermissionVerdict.ALLOW

    def test_recent_denies_starts_zero(self):
        ctx = PermissionContext(session_id="s1")
        assert ctx.recent_denies == 0

    def test_grant_fills_timestamp_when_zero(self):
        ctx = PermissionContext(session_id="s1")
        g = ScopeGrant(resource="path", action="read", selector="/ws/", granted_at=0.0)
        ctx.grant(g)
        assert ctx.grants[0].granted_at > 0.0

    def test_grant_preserves_existing_timestamp(self):
        ctx = PermissionContext(session_id="s1")
        ts = time.time()
        g = ScopeGrant(resource="path", action="read", selector="/ws/", granted_at=ts)
        ctx.grant(g)
        assert ctx.grants[0].granted_at == ts

    def test_empty_grants_evaluate_guarded_confirm(self):
        ctx = PermissionContext(session_id="s1")
        req = ScopeRequest(
            tool_name="fetch_url",
            capabilities=ToolCapability.NETWORK,
            requirements=[
                ScopeRequirement(resource="network", action="connect", selector="api.example.com"),
            ],
        )
        verdict = ctx.evaluate(req, TrustLevel.GUARDED)
        assert verdict == PermissionVerdict.CONFIRM
