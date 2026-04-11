from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, Flag, auto
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .scope import (
        ScopeDiff, ScopeGrant, ScopeRequest, PermissionVerdict,
    )


class ToolCapability(Flag):
    """
    Bit-flag capabilities for a tool — additive to TrustLevel.

    These flags give the harness and UI layer finer-grained information about
    *what* a GUARDED tool actually does, beyond the single tier label.

    Use-cases:
    - EXEC and AGENT_SPAN tools are never session-pre-authorized — they always
      re-confirm (like CRITICAL) even when their trust level is GUARDED.
    - The confirm UI can display a more specific warning message per capability.
    - Future: capability-level rate-limiting, audit tagging, policy overrides.
    """
    NONE       = 0
    EXEC       = auto()      # runs arbitrary shell / subprocess commands
    NETWORK    = auto()      # makes outbound network calls
    AGENT_SPAN = auto()      # spawns one or more sub-agents
    MUTATES    = auto()      # modifies files, memory, or persistent state


class TrustLevel(Enum):
    """
    Three-tier trust hierarchy controlling tool execution behaviour.

    SAFE     — read-only, local, fully reversible → executed automatically.
    GUARDED  — writes, network, side-effects → requires session authorization
                or explicit user confirmation.
    CRITICAL — destructive, cross-system, irreversible → always requires fresh
                human confirmation and is written to the immutable audit log.
    """
    SAFE = "safe"
    GUARDED = "guarded"
    CRITICAL = "critical"

    @property
    def plain(self) -> str:
        """Plain uppercase name — use when the caller controls styling."""
        return self.value.upper()

    @property
    def label(self) -> str:
        """Rich markup label — for CLI console output only."""
        colours = {
            TrustLevel.SAFE: "[green]SAFE[/green]",
            TrustLevel.GUARDED: "[yellow]GUARDED[/yellow]",
            TrustLevel.CRITICAL: "[red]CRITICAL[/red]",
        }
        return colours[self]


@dataclass
class PermissionContext:
    """
    Holds runtime authorization state for a single session.

    Phase A (Issue #45) adds scope-aware grants alongside the legacy
    tool-name authorization.  Tools with a scope_resolver use the
    scope-aware path; tools without one fall back to legacy behavior.
    """

    session_id: str

    # --- Legacy (pre-#45) authorization ---
    session_authorized: set[str] = field(default_factory=set)
    exec_auto: bool = False

    # --- Scope-aware authorization (Issue #45 Phase A) ---
    grants: list[ScopeGrant] = field(default_factory=list)
    _usage: dict[int, dict[str, int]] = field(default_factory=dict)
    """Consumable constraint tracking: grant-index → constraint-key → consumed."""

    # ── Legacy API (unchanged) ────────────────────────────────────

    def authorize(self, tool_name: str) -> None:
        self.session_authorized.add(tool_name)

    def revoke(self, tool_name: str) -> None:
        self.session_authorized.discard(tool_name)

    def enable_exec_auto(self) -> None:
        self.exec_auto = True

    def disable_exec_auto(self) -> None:
        self.exec_auto = False

    def is_authorized(self, tool_name: str, trust_level: TrustLevel) -> bool:
        if trust_level == TrustLevel.SAFE:
            return True
        if trust_level == TrustLevel.GUARDED:
            return tool_name in self.session_authorized
        # CRITICAL always requires fresh confirmation — never pre-authorized.
        return False

    # ── Scope-aware API (Issue #45 Phase A) ───────────────────────

    def grant(self, scope: ScopeGrant) -> None:
        """Add a scope grant to the session."""
        if scope.granted_at == 0.0:
            scope.granted_at = time.time()
        self.grants.append(scope)

    def grant_many(self, scopes: list[ScopeGrant]) -> None:
        for s in scopes:
            self.grant(s)

    def revoke_matching(self, predicate: Callable[[ScopeGrant], bool]) -> None:
        """Remove all grants matching the predicate."""
        kept: list[ScopeGrant] = []
        old_indices: dict[int, int] = {}  # old index → new index
        for i, g in enumerate(self.grants):
            if not predicate(g):
                old_indices[i] = len(kept)
                kept.append(g)
        # Remap usage tracking
        new_usage: dict[int, dict[str, int]] = {}
        for old_idx, usage in self._usage.items():
            if old_idx in old_indices:
                new_usage[old_indices[old_idx]] = usage
        self.grants = kept
        self._usage = new_usage

    def evaluate(
        self, request: ScopeRequest, trust_level: TrustLevel,
    ) -> PermissionVerdict:
        """
        Evaluate a scope request against current grants.

        Returns a PermissionVerdict:
            ALLOW        — all requirements covered by existing grants
            CONFIRM      — first-time authorization needed
            EXPAND_SCOPE — existing grants don't cover the request scope
            DENY         — CRITICAL trust or policy block
        """
        from .scope import PermissionVerdict as PV, DiffReason

        if trust_level == TrustLevel.CRITICAL:
            diff = self.diff(request)
            # CRITICAL always requires fresh confirmation, but we distinguish
            # between first-time and expansion for the UI layer.
            if diff.is_fully_covered:
                return PV.CONFIRM
            return PV.EXPAND_SCOPE if diff.reason != DiffReason.FIRST_TIME else PV.CONFIRM

        if trust_level == TrustLevel.SAFE:
            return PV.ALLOW

        # GUARDED — check scope coverage
        diff = self.diff(request)
        if diff.is_fully_covered:
            # Consume budgets for covered requirements
            self._consume_budgets(request)
            return PV.ALLOW

        if diff.reason == DiffReason.FIRST_TIME or diff.reason == DiffReason.RESOURCE_TYPE_NEW:
            return PV.CONFIRM

        return PV.EXPAND_SCOPE

    def diff(self, request: ScopeRequest) -> ScopeDiff:
        """Compute the scope diff between request and current grants."""
        from .scope import compute_diff
        effective = self._effective_grants()
        return compute_diff(effective, request)

    def _effective_grants(self) -> list[ScopeGrant]:
        """Return grants with consumable budgets adjusted for usage."""
        from .scope import ScopeGrant as SG

        effective: list[ScopeGrant] = []
        for i, g in enumerate(self.grants):
            usage = self._usage.get(i, {})
            if not usage:
                effective.append(g)
                continue
            # Adjust consumable constraints
            adjusted_constraints = dict(g.constraints)
            for key, consumed in usage.items():
                original = g.constraints.get(key)
                if original is not None and isinstance(original, (int, float)):
                    remaining = max(0, original - consumed)
                    adjusted_constraints[key] = remaining
            effective.append(SG(
                resource=g.resource,
                action=g.action,
                selector=g.selector,
                constraints=adjusted_constraints,
                source=g.source,
                granted_at=g.granted_at,
            ))
        return effective

    def _consume_budgets(self, request: ScopeRequest) -> None:
        """Decrement consumable budgets for covered requirements."""
        from .scope import covers as scope_covers

        for req in request.requirements:
            for i, g in enumerate(self.grants):
                if not scope_covers(g, req):
                    continue
                # Find consumable constraints to decrement
                for key in ("remaining_budget", "max_calls"):
                    if key in g.constraints and isinstance(g.constraints[key], (int, float)):
                        usage = self._usage.setdefault(i, {})
                        usage[key] = usage.get(key, 0) + req.constraints.get("spawn_count", 1)
                break  # Only consume from the first matching grant
