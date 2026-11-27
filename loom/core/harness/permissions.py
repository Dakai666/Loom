from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, Flag, auto
from typing import Any, Callable, Iterable, TYPE_CHECKING

from .mode import DEFAULT_MODE, PermissionMode

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
    - A declaration (#704 S3) never covers an EXEC tool: shell is authorized
      only by exec scope grants.
    - The confirm UI can display a more specific warning message per capability.
    - Future: capability-level rate-limiting, audit tagging, policy overrides.
    """
    NONE       = 0
    EXEC       = auto()      # runs arbitrary shell / subprocess commands
    NETWORK    = auto()      # makes outbound network calls
    AGENT_SPAN = auto()      # spawns one or more sub-agents
    MUTATES    = auto()      # modifies files, memory, or persistent state
    READ_PROBE = auto()      # counts as a "probe" for LegitimacyGuard's
                             # probe-first heuristic — gathers context without
                             # mutating it. SAFE-trust tools satisfy the
                             # heuristic implicitly (see LegitimacyGuard);
                             # set this flag explicitly on GUARDED read tools
                             # such as web_search, or on MCP tools that would
                             # otherwise be unrecognized.


class TrustLevel(Enum):
    """
    Three-tier trust hierarchy controlling tool execution behaviour.

    SAFE     — read-only, local, fully reversible → executed automatically.
    GUARDED  — writes, network, side-effects → requires session authorization
                or explicit user confirmation.
    CRITICAL — destructive, cross-system, irreversible → always requires fresh
                human confirmation and is written to the immutable audit log.

    Display methods
    ---------------
    This enum provides two rendering paths.  **Always** choose the right one
    for the output channel — mixing them will corrupt output in non-Rich
    environments.

    ``.plain`` / ``.display_plain``
        Plain uppercase ASCII string (``"SAFE"``, ``"GUARDED"``, ``"CRITICAL"``).
        **Use everywhere except Rich console output:**

        - Discord messages / embeds
        - TUI widget labels and inline widgets
        - JSON / log fields
        - ``ExecutionNodeView.trust_level``
        - Any non-Rich ``print()`` call

    ``.label`` / ``.display_rich``
        Rich markup string (e.g. ``"[green]SAFE[/green]"``).
        **Use ONLY with** ``console.print()`` in CLI/Terminal contexts.
        Passing ``.label`` to Discord or a TUI widget will render the raw
        markup as literal text (e.g. ``"[green]SAFE[/green]"``).

    Issue #148: enforcement is centralised here; platforms only call
    ``.plain`` or ``.label`` — they do not implement trust-tier logic.
    """
    SAFE = "safe"
    GUARDED = "guarded"
    CRITICAL = "critical"

    @property
    def plain(self) -> str:
        """Plain uppercase name — use when the caller controls styling.

        Suitable for Discord, TUI widgets, JSON, log files, and any
        non-Rich output channel.  Equivalent to ``display_plain``.
        """
        return self.value.upper()

    @property
    def display_plain(self) -> str:
        """Alias for ``.plain`` — plain uppercase trust tier name.

        Prefer this alias over ``.plain`` when the intent is display
        (vs. equality checks) to make the rendering path explicit.
        """
        return self.plain

    @property
    def label(self) -> str:
        """Rich markup label — for CLI ``console.print()`` **only**.

        Do **not** pass this to Discord, TUI widgets, JSON, or any other
        channel that does not interpret Rich markup.  Use ``.plain`` instead.
        Equivalent to ``display_rich``.
        """
        colours = {
            TrustLevel.SAFE: "[green]SAFE[/green]",
            TrustLevel.GUARDED: "[yellow]GUARDED[/yellow]",
            TrustLevel.CRITICAL: "[red]CRITICAL[/red]",
        }
        return colours[self]

    @property
    def display_rich(self) -> str:
        """Alias for ``.label`` — Rich markup trust tier name.

        Prefer this alias over ``.label`` when the intent is display
        (vs. equality checks) to make the rendering path explicit.
        Only valid for Rich ``console.print()`` in CLI/Terminal contexts.
        """
        return self.label


@dataclass(frozen=True)
class Boundary:
    """Tools one boundary declares (#704 S3)."""
    source: str                 # e.g. "platform:discord", "autonomy:dawn", "subagent"
    tools: frozenset[str]
    unattended_only: bool = False


@dataclass
class PermissionContext:
    """
    Holds runtime authorization state for a single session.

    Scope grants (Issue #45) authorize resources; boundaries (#704 S3)
    declare tools. Neither is a name-level pass: BlastRadius decides every
    call at the permission-mode decision point.
    """

    session_id: str

    # --- Declared tools (#704 S3) ---
    # A tool name never authorizes by itself. A boundary (the Discord
    # platform, a routine's ``allowed_tools``, a sub-agent's whitelist)
    # declares which tools it uses; BlastRadius still decides each call at
    # the permission-mode decision point, where the hard guardrails and the
    # declared scope ceilings hold.
    boundaries: dict[str, "Boundary"] = field(default_factory=dict)
    # Sources of active external chimes. Orthogonal to declarations/grants:
    # any one external source constrains the entire unattended turn.
    external_inputs: set[str] = field(default_factory=set)

    # --- Scope-aware authorization (Issue #45 Phase A) ---
    grants: list[ScopeGrant] = field(default_factory=list)
    _usage: dict[int, dict[str, int]] = field(default_factory=dict)
    """Consumable constraint tracking: grant-index → constraint-key → consumed."""

    # --- Penalty Box (Issue #47 Phase 2) ---
    recent_denies: int = 0

    # --- Permission mode (#704) ---
    # 自理 by default on every entry point. Only the operator's /mode or
    # --mode changes it (``LoomSession.set_permission_mode``); no tool does.
    mode: PermissionMode = DEFAULT_MODE

    # ── Declarations (#704 S3) ────────────────────────────────────

    def declare(self, source: str, tools: "Iterable[str]", *, unattended_only: bool = False) -> None:
        """Declare the tools a boundary uses. ``unattended_only``: a
        program's declaration covers only its own unattended calls, never
        an attended turn in the same session. Declaring again replaces."""
        self.boundaries[source] = Boundary(source, frozenset(tools), unattended_only)

    def withdraw(self, source: str) -> None:
        self.boundaries.pop(source, None)

    def declared_by(self, tool_name: str, *, attended: bool) -> str | None:
        """The first boundary that declares *tool_name* for a call that is
        (or is not) attended, or None."""
        for b in self.boundaries.values():
            if tool_name in b.tools and not (attended and b.unattended_only):
                return b.source
        return None

    def declared_tools(self) -> frozenset[str]:
        return frozenset().union(*(b.tools for b in self.boundaries.values()))

    def runs_unasked(self, tool_name: str, trust_level: TrustLevel, *, attended: bool = True) -> bool:
        """Whether a call can be expected to run without a prompt (parallel
        dispatch): a SAFE tool, or a GUARDED one a boundary declares. The
        decision point still checks each call."""
        if trust_level == TrustLevel.SAFE:
            return True
        return trust_level == TrustLevel.GUARDED and self.declared_by(
            tool_name, attended=attended) is not None

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

    def purge_expired(self) -> int:
        """Remove expired grants from the grant list. Returns count removed."""
        import time as _time
        now = _time.time()
        before = len(self.grants)
        self.revoke_matching(lambda g: g.valid_until > 0 and now > g.valid_until)
        return before - len(self.grants)

    def evaluate(
        self, request: ScopeRequest, trust_level: TrustLevel, *, attended: bool = True,
    ) -> PermissionVerdict:
        """
        Evaluate a scope request against current grants.

        Returns a PermissionVerdict:
            ALLOW        — all requirements covered by existing grants
            CONFIRM      — first-time authorization needed
            EXPAND_SCOPE — existing grants don't cover the request scope
            DENY         — CRITICAL trust or policy block

        ``attended=False`` (an unattended call) ignores grants a human gave
        in an attended turn (``ScopeGrant.attended_only``, #690 S6b).
        """
        from .scope import PermissionVerdict as PV, DiffReason

        if trust_level == TrustLevel.CRITICAL:
            diff = self.diff(request, attended=attended)
            # CRITICAL always requires fresh confirmation, but we distinguish
            # between first-time and expansion for the UI layer.
            if diff.is_fully_covered:
                return PV.CONFIRM
            return PV.EXPAND_SCOPE if diff.reason != DiffReason.FIRST_TIME else PV.CONFIRM

        if trust_level == TrustLevel.SAFE:
            return PV.ALLOW

        # GUARDED — check scope coverage
        diff = self.diff(request, attended=attended)
        if diff.is_fully_covered:
            # Consume budgets for covered requirements
            self._consume_budgets(request)
            return PV.ALLOW

        if diff.reason == DiffReason.FIRST_TIME or diff.reason == DiffReason.RESOURCE_TYPE_NEW:
            return PV.CONFIRM

        return PV.EXPAND_SCOPE

    def diff(self, request: ScopeRequest, *, attended: bool = True) -> ScopeDiff:
        """Compute the scope diff between request and current grants."""
        from .scope import compute_diff
        effective = self._effective_grants()
        if not attended:
            effective = [g for g in effective if not g.attended_only]
        return compute_diff(effective, request)

    def _effective_grants(self) -> list[ScopeGrant]:
        """Return grants with consumable budgets adjusted for usage, expired grants filtered out."""
        from .scope import ScopeGrant as SG
        import time as _time

        # Self-heal: purge expired grants so _usage stays in sync (#88 review)
        if any(g.valid_until > 0 and _time.time() > g.valid_until for g in self.grants):
            self.purge_expired()

        now = _time.time()
        effective: list[ScopeGrant] = []
        for i, g in enumerate(self.grants):
            # TTL expiry check (#88) — belt-and-suspenders after purge above
            if g.valid_until > 0 and now > g.valid_until:
                continue
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
                valid_until=g.valid_until,
                attended_only=g.attended_only,
            ))
        return effective

    def _consume_budgets(self, request: ScopeRequest) -> None:
        """
        Decrement consumable budgets for covered requirements.

        Must only be called after ``evaluate()`` has confirmed full
        coverage — this method matches against *effective* grants
        (with already-consumed budgets subtracted) so that a depleted
        grant is never double-matched.

        Consumption units per constraint key:
        - ``remaining_budget``: consumes ``req.constraints["spawn_count"]``
          (defaults to 1) — tracks agent spawn permits.
        - ``max_calls``: always consumes 1 per matched requirement —
          tracks call-count limits (network, exec, etc.).
        """
        from .scope import covers as scope_covers

        effective = self._effective_grants()

        for req in request.requirements:
            for i, eff_g in enumerate(effective):
                if not scope_covers(eff_g, req):
                    continue
                # Determine consumption amount per constraint key
                for key in ("remaining_budget", "max_calls"):
                    if key not in self.grants[i].constraints:
                        continue
                    if not isinstance(self.grants[i].constraints[key], (int, float)):
                        continue
                    if key == "remaining_budget":
                        amount = req.constraints.get("spawn_count", 1)
                    else:
                        # max_calls: each matched requirement consumes 1
                        amount = 1
                    usage = self._usage.setdefault(i, {})
                    usage[key] = usage.get(key, 0) + amount
                break  # Only consume from the first matching grant
