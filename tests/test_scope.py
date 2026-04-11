"""
Tests for Issue #45 Phase A — scope-aware permission substrate.

Coverage:
  1. ScopeMatcher.covers() per resource type
  2. PermissionContext.evaluate() — four verdict paths
  3. PermissionContext.diff() — DiffReason correctness
  4. Path-prefix coverage and expansion
  5. Exec workspace-only / absolute-path / scope-unknown
  6. Agent budget arithmetic (consume + exhaust)
  7. Consumable constraint tracking
  8. compute_diff() standalone
  9. ToolDefinition scope_resolver/scope_descriptions fields
"""

import time

import pytest

from loom.core.harness.permissions import PermissionContext, ToolCapability, TrustLevel
from loom.core.harness.scope import (
    AgentMatcher,
    DiffReason,
    ExecMatcher,
    MutationMatcher,
    NetworkMatcher,
    PathMatcher,
    PermissionVerdict,
    ScopeDiff,
    ScopeGrant,
    ScopeRequirement,
    ScopeRequest,
    compute_diff,
    covers,
    get_matcher,
)
from loom.core.harness.registry import ToolDefinition


# ── Helpers ─────────────────────────────────────────────────────────

def _grant(resource, action, selector, **constraints):
    return ScopeGrant(
        resource=resource, action=action, selector=selector,
        constraints=constraints,
    )


def _req(resource, action, selector, tool_name="test_tool", **constraints):
    return ScopeRequirement(
        resource=resource, action=action, selector=selector,
        constraints=constraints, tool_name=tool_name,
    )


def _scope_request(requirements, tool_name="test_tool"):
    return ScopeRequest(
        tool_name=tool_name,
        capabilities=ToolCapability.NONE,
        requirements=requirements,
    )


def _ctx_with_grants(*grants):
    ctx = PermissionContext(session_id="test")
    for g in grants:
        ctx.grant(g)
    return ctx


# =====================================================================
# 1. PathMatcher
# =====================================================================

class TestPathMatcher:
    matcher = PathMatcher()

    def test_exact_prefix_match(self):
        g = _grant("path", "write", "/workspace/doc/")
        r = _req("path", "write", "/workspace/doc/test.md")
        assert self.matcher.covers(g, r)

    def test_exact_same_path(self):
        g = _grant("path", "write", "/workspace/doc/")
        r = _req("path", "write", "/workspace/doc/")
        assert self.matcher.covers(g, r)

    def test_subdirectory_covered(self):
        g = _grant("path", "write", "/workspace/")
        r = _req("path", "write", "/workspace/doc/sub/deep.md")
        assert self.matcher.covers(g, r)

    def test_outside_prefix_not_covered(self):
        g = _grant("path", "write", "/workspace/doc/")
        r = _req("path", "write", "/workspace/loom/core.py")
        assert not self.matcher.covers(g, r)

    def test_action_mismatch(self):
        g = _grant("path", "read", "/workspace/doc/")
        r = _req("path", "write", "/workspace/doc/test.md")
        assert not self.matcher.covers(g, r)

    def test_sibling_not_covered(self):
        g = _grant("path", "write", "/workspace/doc/")
        r = _req("path", "write", "/workspace/docs/other.md")
        assert not self.matcher.covers(g, r)


# =====================================================================
# 2. NetworkMatcher
# =====================================================================

class TestNetworkMatcher:
    matcher = NetworkMatcher()

    def test_exact_domain_match(self):
        g = _grant("network", "connect", "api.openai.com")
        r = _req("network", "connect", "api.openai.com")
        assert self.matcher.covers(g, r)

    def test_different_domain(self):
        g = _grant("network", "connect", "api.openai.com")
        r = _req("network", "connect", "evil.com")
        assert not self.matcher.covers(g, r)

    def test_subdomain_not_covered(self):
        g = _grant("network", "connect", "openai.com")
        r = _req("network", "connect", "api.openai.com")
        assert not self.matcher.covers(g, r)

    def test_action_mismatch(self):
        g = _grant("network", "connect", "api.openai.com")
        r = _req("network", "read", "api.openai.com")
        assert not self.matcher.covers(g, r)


# =====================================================================
# 3. ExecMatcher
# =====================================================================

class TestExecMatcher:
    matcher = ExecMatcher()

    def test_workspace_covers_workspace(self):
        g = _grant("exec", "execute", "workspace")
        r = _req("exec", "execute", "workspace")
        assert self.matcher.covers(g, r)

    def test_workspace_with_absolute_paths_deny(self):
        g = _grant("exec", "execute", "workspace", absolute_paths="deny")
        r = _req("exec", "execute", "workspace", has_absolute_paths=True)
        assert not self.matcher.covers(g, r)

    def test_workspace_without_absolute_paths(self):
        g = _grant("exec", "execute", "workspace", absolute_paths="deny")
        r = _req("exec", "execute", "workspace")
        assert self.matcher.covers(g, r)

    def test_wildcard_covers_anything(self):
        g = _grant("exec", "execute", "*")
        r = _req("exec", "execute", "workspace")
        assert self.matcher.covers(g, r)

    def test_workspace_does_not_cover_outside(self):
        g = _grant("exec", "execute", "workspace")
        r = _req("exec", "execute", "/etc")
        assert not self.matcher.covers(g, r)

    def test_action_mismatch(self):
        g = _grant("exec", "execute", "workspace")
        r = _req("exec", "read", "workspace")
        assert not self.matcher.covers(g, r)


# =====================================================================
# 4. AgentMatcher
# =====================================================================

class TestAgentMatcher:
    matcher = AgentMatcher()

    def test_budget_sufficient(self):
        g = _grant("agent", "spawn", "default", remaining_budget=3)
        r = _req("agent", "spawn", "default", spawn_count=1)
        assert self.matcher.covers(g, r)

    def test_budget_exact(self):
        g = _grant("agent", "spawn", "default", remaining_budget=1)
        r = _req("agent", "spawn", "default", spawn_count=1)
        assert self.matcher.covers(g, r)

    def test_budget_insufficient(self):
        g = _grant("agent", "spawn", "default", remaining_budget=0)
        r = _req("agent", "spawn", "default", spawn_count=1)
        assert not self.matcher.covers(g, r)

    def test_no_budget_constraint_always_covers(self):
        g = _grant("agent", "spawn", "default")
        r = _req("agent", "spawn", "default", spawn_count=5)
        assert self.matcher.covers(g, r)

    def test_wildcard_selector(self):
        g = _grant("agent", "spawn", "*", remaining_budget=2)
        r = _req("agent", "spawn", "special", spawn_count=1)
        assert self.matcher.covers(g, r)

    def test_selector_mismatch(self):
        g = _grant("agent", "spawn", "default", remaining_budget=5)
        r = _req("agent", "spawn", "special", spawn_count=1)
        assert not self.matcher.covers(g, r)


# =====================================================================
# 5. MutationMatcher
# =====================================================================

class TestMutationMatcher:
    matcher = MutationMatcher()

    def test_exact_match(self):
        g = _grant("mutation", "mutate", "memory")
        r = _req("mutation", "mutate", "memory")
        assert self.matcher.covers(g, r)

    def test_different_target(self):
        g = _grant("mutation", "mutate", "memory")
        r = _req("mutation", "mutate", "relation")
        assert not self.matcher.covers(g, r)


# =====================================================================
# 6. Top-level covers() + get_matcher()
# =====================================================================

class TestTopLevelCovers:
    def test_cross_resource_never_covers(self):
        g = _grant("path", "write", "/workspace/doc/")
        r = _req("network", "connect", "api.openai.com")
        assert not covers(g, r)

    def test_unknown_resource_uses_exact_match(self):
        g = _grant("custom", "do", "foo")
        r = _req("custom", "do", "foo")
        assert covers(g, r)

    def test_get_matcher_returns_correct_type(self):
        assert isinstance(get_matcher("path"), PathMatcher)
        assert isinstance(get_matcher("network"), NetworkMatcher)
        assert isinstance(get_matcher("exec"), ExecMatcher)
        assert isinstance(get_matcher("agent"), AgentMatcher)
        assert isinstance(get_matcher("mutation"), MutationMatcher)


# =====================================================================
# 7. compute_diff()
# =====================================================================

class TestComputeDiff:
    def test_fully_covered(self):
        grants = [_grant("path", "write", "/workspace/doc/")]
        req = _scope_request([_req("path", "write", "/workspace/doc/x.md")])
        diff = compute_diff(grants, req)
        assert diff.is_fully_covered
        assert diff.reason == DiffReason.FULLY_COVERED
        assert len(diff.covered) == 1
        assert len(diff.missing) == 0

    def test_first_time_no_grants(self):
        diff = compute_diff([], _scope_request([
            _req("path", "write", "/workspace/doc/x.md"),
        ]))
        assert not diff.is_fully_covered
        assert diff.reason == DiffReason.FIRST_TIME

    def test_resource_type_new(self):
        grants = [_grant("path", "write", "/workspace/doc/")]
        req = _scope_request([_req("network", "connect", "api.openai.com")])
        diff = compute_diff(grants, req)
        assert diff.reason == DiffReason.RESOURCE_TYPE_NEW

    def test_selector_expansion(self):
        grants = [_grant("path", "write", "/workspace/doc/")]
        req = _scope_request([_req("path", "write", "/workspace/loom/core.py")])
        diff = compute_diff(grants, req)
        assert diff.reason == DiffReason.SELECTOR_EXPANSION

    def test_constraint_expansion(self):
        grants = [_grant("exec", "execute", "workspace", absolute_paths="deny")]
        req = _scope_request([
            _req("exec", "execute", "workspace", has_absolute_paths=True),
        ])
        diff = compute_diff(grants, req)
        assert diff.reason == DiffReason.CONSTRAINT_EXPANSION

    def test_mixed_covered_and_missing(self):
        grants = [_grant("path", "write", "/workspace/doc/")]
        req = _scope_request([
            _req("path", "write", "/workspace/doc/a.md"),
            _req("path", "write", "/workspace/loom/b.py"),
        ])
        diff = compute_diff(grants, req)
        assert len(diff.covered) == 1
        assert len(diff.missing) == 1

    def test_multiple_grants_cover_different_requirements(self):
        grants = [
            _grant("path", "write", "/workspace/doc/"),
            _grant("path", "write", "/workspace/tests/"),
        ]
        req = _scope_request([
            _req("path", "write", "/workspace/doc/a.md"),
            _req("path", "write", "/workspace/tests/b.py"),
        ])
        diff = compute_diff(grants, req)
        assert diff.is_fully_covered


# =====================================================================
# 8. PermissionContext.evaluate() — four verdict paths
# =====================================================================

class TestPermissionContextEvaluate:
    def test_safe_always_allow(self):
        ctx = PermissionContext(session_id="test")
        req = _scope_request([_req("path", "read", "/workspace/doc/x.md")])
        assert ctx.evaluate(req, TrustLevel.SAFE) == PermissionVerdict.ALLOW

    def test_guarded_allow_when_covered(self):
        ctx = _ctx_with_grants(_grant("path", "write", "/workspace/doc/"))
        req = _scope_request([_req("path", "write", "/workspace/doc/x.md")])
        assert ctx.evaluate(req, TrustLevel.GUARDED) == PermissionVerdict.ALLOW

    def test_guarded_confirm_first_time(self):
        ctx = PermissionContext(session_id="test")
        req = _scope_request([_req("path", "write", "/workspace/doc/x.md")])
        assert ctx.evaluate(req, TrustLevel.GUARDED) == PermissionVerdict.CONFIRM

    def test_guarded_expand_scope(self):
        ctx = _ctx_with_grants(_grant("path", "write", "/workspace/doc/"))
        req = _scope_request([_req("path", "write", "/workspace/loom/x.py")])
        assert ctx.evaluate(req, TrustLevel.GUARDED) == PermissionVerdict.EXPAND_SCOPE

    def test_guarded_confirm_new_resource_type(self):
        ctx = _ctx_with_grants(_grant("path", "write", "/workspace/doc/"))
        req = _scope_request([_req("network", "connect", "api.openai.com")])
        assert ctx.evaluate(req, TrustLevel.GUARDED) == PermissionVerdict.CONFIRM

    def test_critical_always_confirm(self):
        ctx = _ctx_with_grants(_grant("path", "write", "/workspace/doc/"))
        req = _scope_request([_req("path", "write", "/workspace/doc/x.md")])
        assert ctx.evaluate(req, TrustLevel.CRITICAL) == PermissionVerdict.CONFIRM

    def test_critical_expand_scope(self):
        ctx = _ctx_with_grants(_grant("path", "write", "/workspace/doc/"))
        req = _scope_request([_req("path", "write", "/workspace/loom/x.py")])
        assert ctx.evaluate(req, TrustLevel.CRITICAL) == PermissionVerdict.EXPAND_SCOPE


# =====================================================================
# 9. PermissionContext.diff()
# =====================================================================

class TestPermissionContextDiff:
    def test_diff_fully_covered(self):
        ctx = _ctx_with_grants(_grant("path", "write", "/workspace/doc/"))
        req = _scope_request([_req("path", "write", "/workspace/doc/x.md")])
        diff = ctx.diff(req)
        assert diff.is_fully_covered

    def test_diff_missing(self):
        ctx = PermissionContext(session_id="test")
        req = _scope_request([_req("path", "write", "/workspace/doc/x.md")])
        diff = ctx.diff(req)
        assert not diff.is_fully_covered
        assert len(diff.missing) == 1


# =====================================================================
# 10. Consumable constraints — budget tracking
# =====================================================================

class TestConsumableBudgets:
    def test_agent_budget_decrements(self):
        ctx = _ctx_with_grants(
            _grant("agent", "spawn", "default", remaining_budget=3),
        )
        req = _scope_request([
            _req("agent", "spawn", "default", spawn_count=1),
        ])

        # First call — budget 3, need 1 → ALLOW
        assert ctx.evaluate(req, TrustLevel.GUARDED) == PermissionVerdict.ALLOW
        # Second call
        assert ctx.evaluate(req, TrustLevel.GUARDED) == PermissionVerdict.ALLOW
        # Third call
        assert ctx.evaluate(req, TrustLevel.GUARDED) == PermissionVerdict.ALLOW
        # Fourth call — budget exhausted
        assert ctx.evaluate(req, TrustLevel.GUARDED) == PermissionVerdict.EXPAND_SCOPE

    def test_budget_exact_exhaustion(self):
        ctx = _ctx_with_grants(
            _grant("agent", "spawn", "default", remaining_budget=1),
        )
        req = _scope_request([
            _req("agent", "spawn", "default", spawn_count=1),
        ])
        assert ctx.evaluate(req, TrustLevel.GUARDED) == PermissionVerdict.ALLOW
        assert ctx.evaluate(req, TrustLevel.GUARDED) == PermissionVerdict.EXPAND_SCOPE


# =====================================================================
# 11. Grant management
# =====================================================================

class TestGrantManagement:
    def test_grant_sets_timestamp(self):
        ctx = PermissionContext(session_id="test")
        g = ScopeGrant(resource="path", action="write", selector="/workspace/doc/")
        before = time.time()
        ctx.grant(g)
        after = time.time()
        assert before <= ctx.grants[0].granted_at <= after

    def test_grant_many(self):
        ctx = PermissionContext(session_id="test")
        ctx.grant_many([
            _grant("path", "write", "/workspace/doc/"),
            _grant("network", "connect", "api.openai.com"),
        ])
        assert len(ctx.grants) == 2

    def test_revoke_matching(self):
        ctx = _ctx_with_grants(
            _grant("path", "write", "/workspace/doc/"),
            _grant("path", "write", "/workspace/loom/"),
            _grant("network", "connect", "api.openai.com"),
        )
        ctx.revoke_matching(lambda g: g.resource == "path")
        assert len(ctx.grants) == 1
        assert ctx.grants[0].resource == "network"

    def test_revoke_preserves_usage_tracking(self):
        ctx = _ctx_with_grants(
            _grant("path", "write", "/workspace/doc/"),
            _grant("agent", "spawn", "default", remaining_budget=3),
        )
        # Consume one agent spawn
        req = _scope_request([_req("agent", "spawn", "default", spawn_count=1)])
        ctx.evaluate(req, TrustLevel.GUARDED)

        # Revoke the path grant (index 0)
        ctx.revoke_matching(lambda g: g.resource == "path")

        # Agent grant should still have 2 remaining (consumed 1)
        assert len(ctx.grants) == 1
        req2 = _scope_request([_req("agent", "spawn", "default", spawn_count=1)])
        assert ctx.evaluate(req2, TrustLevel.GUARDED) == PermissionVerdict.ALLOW
        assert ctx.evaluate(req2, TrustLevel.GUARDED) == PermissionVerdict.ALLOW
        assert ctx.evaluate(req2, TrustLevel.GUARDED) == PermissionVerdict.EXPAND_SCOPE


# =====================================================================
# 12. Legacy API backward compat
# =====================================================================

class TestLegacyCompat:
    def test_legacy_authorize_still_works(self):
        ctx = PermissionContext(session_id="test")
        ctx.authorize("write_file")
        assert ctx.is_authorized("write_file", TrustLevel.GUARDED)
        assert not ctx.is_authorized("run_bash", TrustLevel.GUARDED)

    def test_legacy_exec_auto(self):
        ctx = PermissionContext(session_id="test")
        assert not ctx.exec_auto
        ctx.enable_exec_auto()
        assert ctx.exec_auto
        ctx.disable_exec_auto()
        assert not ctx.exec_auto

    def test_safe_always_authorized(self):
        ctx = PermissionContext(session_id="test")
        assert ctx.is_authorized("any_tool", TrustLevel.SAFE)

    def test_critical_never_preauthorized(self):
        ctx = PermissionContext(session_id="test")
        ctx.authorize("dangerous_tool")
        assert not ctx.is_authorized("dangerous_tool", TrustLevel.CRITICAL)


# =====================================================================
# 13. ToolDefinition new fields
# =====================================================================

class TestToolDefinitionScopeFields:
    def test_default_values(self):
        td = ToolDefinition(
            name="test",
            description="test tool",
            trust_level=TrustLevel.GUARDED,
            input_schema={},
            executor=None,  # type: ignore
        )
        assert td.scope_descriptions == []
        assert td.scope_resolver is None

    def test_scope_resolver_callable(self):
        def my_resolver(call):
            return ScopeRequest(
                tool_name=call.tool_name,
                capabilities=ToolCapability.MUTATES,
                requirements=[],
            )

        td = ToolDefinition(
            name="test",
            description="test tool",
            trust_level=TrustLevel.GUARDED,
            input_schema={},
            executor=None,  # type: ignore
            scope_descriptions=["writes under workspace path"],
            scope_resolver=my_resolver,
        )
        assert td.scope_descriptions == ["writes under workspace path"]
        assert td.scope_resolver is my_resolver
