"""
Scope-aware permission substrate — Issue #45 Phase A.

Provides the data model and matching logic that upgrades Loom's
authorization from tool-name-based to resource-scope-based.

Key abstractions:
    ScopeDescriptor  — shared base for grants and requirements
    ScopeGrant       — an authorized resource boundary
    ScopeRequirement — what a single tool call actually needs
    ScopeRequest     — aggregated requirements for one tool call
    ScopeDiff        — delta between request and existing grants
    DiffReason       — machine-readable reason for the delta
    PermissionVerdict— structured middleware authorization result
    ScopeMatcher     — protocol for resource-type-specific matching

This module does NOT define UX (that is #88) or autonomy reasoning
(that is #47).  It only provides the computable, verifiable substrate
that those layers will build on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Protocol

from .permissions import ToolCapability


# ---------------------------------------------------------------------------
# Core data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScopeDescriptor:
    """
    Shared base fields for grants and requirements.

    Frozen so instances can be used in sets / as dict keys when needed.
    Both ScopeGrant and ScopeRequirement use composition (not inheritance)
    because Python dataclasses cannot mix frozen and non-frozen inheritance.
    """
    resource: str
    """Resource type: path, network, exec, agent, mutation."""

    action: str
    """Action verb: read, write, execute, spawn, mutate, connect."""

    selector: str
    """
    Resource-specific identifier.

    Examples:
        path     → directory prefix like "/workspace/doc/"
        network  → domain like "api.openai.com"
        exec     → "workspace" or "*"
        agent    → "default"
        mutation → "memory" or "relation"
    """

    constraints: dict[str, Any] = field(default_factory=dict)
    """
    Additional restrictions.

    Examples:
        {"absolute_paths": "deny"}
        {"remaining_budget": 3}
        {"max_calls": 10}
    """


@dataclass
class ScopeGrant:
    """
    An authorized resource boundary — what has been approved.

    Same fields as ScopeDescriptor plus provenance metadata.
    """
    resource: str
    action: str
    selector: str
    constraints: dict[str, Any] = field(default_factory=dict)

    source: str = "manual_confirm"
    """Origin of this grant: manual_confirm, lease, auto, exec_auto, system."""

    granted_at: float = 0.0
    """time.time() when granted.  #88 needs this for TTL calculation."""


@dataclass(frozen=True)
class ScopeRequirement:
    """
    What a single tool call actually needs — the request side.

    Same fields as ScopeDescriptor plus tool provenance.
    """
    resource: str
    action: str
    selector: str
    constraints: dict[str, Any] = field(default_factory=dict)

    tool_name: str = ""
    """Which tool generated this requirement."""

    capabilities: ToolCapability = ToolCapability.NONE
    """Capability flags from the tool definition."""


@dataclass
class ScopeRequest:
    """Aggregated requirements for one tool call."""
    tool_name: str
    capabilities: ToolCapability
    requirements: list[ScopeRequirement] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Diff and verdict
# ---------------------------------------------------------------------------

class DiffReason(Enum):
    """Machine-readable reason for a scope diff — #47 consumes this."""
    FULLY_COVERED = "fully_covered"
    FIRST_TIME = "first_time"
    SELECTOR_EXPANSION = "selector_expansion"
    CONSTRAINT_EXPANSION = "constraint_expansion"
    RESOURCE_TYPE_NEW = "resource_type_new"


@dataclass
class ScopeDiff:
    """Delta between a request and the current grants."""
    missing: list[ScopeRequirement] = field(default_factory=list)
    covered: list[ScopeRequirement] = field(default_factory=list)
    reason: DiffReason = DiffReason.FULLY_COVERED

    @property
    def is_fully_covered(self) -> bool:
        return len(self.missing) == 0


class PermissionVerdict(Enum):
    """Structured authorization result from the middleware."""
    ALLOW = "allow"
    CONFIRM = "confirm"
    EXPAND_SCOPE = "expand_scope"
    DENY = "deny"


# ---------------------------------------------------------------------------
# ScopeMatcher — resource-type-specific matching
# ---------------------------------------------------------------------------

# All three scope types (ScopeDescriptor, ScopeGrant, ScopeRequirement)
# share resource/action/selector/constraints fields.  Matchers accept any
# object with those attributes — we use a Protocol for type safety.

class _HasScopeFields(Protocol):
    resource: str
    action: str
    selector: str
    constraints: dict[str, Any]


class ScopeMatcher(Protocol):
    """Protocol for checking whether a grant covers a requirement."""
    def covers(self, grant: _HasScopeFields, requirement: _HasScopeFields) -> bool: ...


class PathMatcher:
    """
    Path-prefix containment matcher.

    A grant with selector="/workspace/doc/" covers any requirement
    whose selector starts with that prefix (using PurePosixPath
    is_relative_to for correctness).
    """
    def covers(self, grant: _HasScopeFields, requirement: _HasScopeFields) -> bool:
        if grant.action != requirement.action:
            return False
        try:
            return PurePosixPath(requirement.selector).is_relative_to(
                PurePosixPath(grant.selector)
            )
        except (TypeError, ValueError):
            return False


class NetworkMatcher:
    """
    Exact domain match for network resources.

    Phase A: exact match only.  Wildcard (*.openai.com) deferred.
    """
    def covers(self, grant: _HasScopeFields, requirement: _HasScopeFields) -> bool:
        if grant.action != requirement.action:
            return False
        return grant.selector == requirement.selector


class ExecMatcher:
    """
    Workspace containment matcher for exec resources.

    A grant with selector="workspace" covers requirements that do not
    contain absolute paths outside the workspace.  The requirement's
    constraints["absolute_paths"] == "deny" from the grant is enforced:
    if the grant forbids absolute paths and the requirement has them,
    coverage fails.
    """
    def covers(self, grant: _HasScopeFields, requirement: _HasScopeFields) -> bool:
        if grant.action != requirement.action:
            return False
        # Grant selector "workspace" covers requirement selector "workspace"
        # or any sub-workspace selector.
        if grant.selector == "workspace":
            if requirement.selector not in ("workspace", ""):
                return False
            # Check absolute_paths constraint
            if grant.constraints.get("absolute_paths") == "deny":
                if requirement.constraints.get("has_absolute_paths"):
                    return False
            return True
        # Wildcard grant covers everything
        if grant.selector == "*":
            return True
        return grant.selector == requirement.selector


class AgentMatcher:
    """
    Budget-arithmetic matcher for agent spawn resources.

    A grant covers a requirement if the grant's remaining_budget
    is >= the requirement's spawn count (defaults to 1).
    """
    def covers(self, grant: _HasScopeFields, requirement: _HasScopeFields) -> bool:
        if grant.action != requirement.action:
            return False
        if grant.selector != "*" and grant.selector != requirement.selector:
            return False
        budget = grant.constraints.get("remaining_budget")
        needed = requirement.constraints.get("spawn_count", 1)
        if budget is not None and budget < needed:
            return False
        return True


class MutationMatcher:
    """Exact match on mutation target (memory, relation, etc.)."""
    def covers(self, grant: _HasScopeFields, requirement: _HasScopeFields) -> bool:
        if grant.action != requirement.action:
            return False
        return grant.selector == requirement.selector


# ---------------------------------------------------------------------------
# Matcher registry
# ---------------------------------------------------------------------------

_MATCHERS: dict[str, ScopeMatcher] = {
    "path": PathMatcher(),
    "network": NetworkMatcher(),
    "exec": ExecMatcher(),
    "agent": AgentMatcher(),
    "mutation": MutationMatcher(),
}


def get_matcher(resource: str) -> ScopeMatcher:
    """Return the matcher for a resource type, falling back to exact match."""
    return _MATCHERS.get(resource, NetworkMatcher())  # NetworkMatcher does exact match


def covers(grant: _HasScopeFields, requirement: _HasScopeFields) -> bool:
    """Top-level convenience: does this grant cover this requirement?"""
    if grant.resource != requirement.resource:
        return False
    matcher = get_matcher(grant.resource)
    return matcher.covers(grant, requirement)


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------

def compute_diff(
    grants: list[ScopeGrant],
    request: ScopeRequest,
) -> ScopeDiff:
    """
    Compare a ScopeRequest against existing grants and produce a ScopeDiff.

    For each requirement, check if any grant covers it.  Uncovered
    requirements go into `missing`; covered ones go into `covered`.
    The `reason` is determined by the nature of the missing requirements.
    """
    covered_reqs: list[ScopeRequirement] = []
    missing_reqs: list[ScopeRequirement] = []

    for req in request.requirements:
        if any(covers(g, req) for g in grants):
            covered_reqs.append(req)
        else:
            missing_reqs.append(req)

    if not missing_reqs:
        return ScopeDiff(
            missing=[], covered=covered_reqs, reason=DiffReason.FULLY_COVERED,
        )

    # Determine the reason for the gap
    reason = _classify_diff_reason(grants, missing_reqs)
    return ScopeDiff(missing=missing_reqs, covered=covered_reqs, reason=reason)


def _classify_diff_reason(
    grants: list[ScopeGrant],
    missing: list[ScopeRequirement],
) -> DiffReason:
    """
    Classify why requirements are missing.

    Priority order:
    1. If no grant exists for this resource type at all → RESOURCE_TYPE_NEW
    2. If grants exist for the resource but with a different selector → SELECTOR_EXPANSION
    3. If grants exist with matching selector but constraints block → CONSTRAINT_EXPANSION
    4. Otherwise → FIRST_TIME
    """
    if not grants:
        return DiffReason.FIRST_TIME

    grant_resources = {g.resource for g in grants}

    has_new_resource = False
    has_selector_expansion = False
    has_constraint_expansion = False

    for req in missing:
        if req.resource not in grant_resources:
            has_new_resource = True
        elif any(
            g.resource == req.resource and g.action == req.action
            for g in grants
        ):
            # Same resource+action exists, but selector or constraints differ
            if any(
                (g.resource, g.action, g.selector) == (req.resource, req.action, req.selector)
                for g in grants
            ):
                has_constraint_expansion = True
            else:
                has_selector_expansion = True
        # else: no grant with matching resource+action → first_time for this action

    if has_new_resource:
        return DiffReason.RESOURCE_TYPE_NEW
    if has_selector_expansion:
        return DiffReason.SELECTOR_EXPANSION
    if has_constraint_expansion:
        return DiffReason.CONSTRAINT_EXPANSION
    return DiffReason.FIRST_TIME
