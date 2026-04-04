from dataclasses import dataclass, field
from enum import Enum, Flag, auto


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
    """Holds runtime authorization state for a single session."""

    session_id: str
    # Tools the user has explicitly authorized for this session (GUARDED level).
    session_authorized: set[str] = field(default_factory=set)

    def authorize(self, tool_name: str) -> None:
        self.session_authorized.add(tool_name)

    def revoke(self, tool_name: str) -> None:
        self.session_authorized.discard(tool_name)

    def is_authorized(self, tool_name: str, trust_level: TrustLevel) -> bool:
        if trust_level == TrustLevel.SAFE:
            return True
        if trust_level == TrustLevel.GUARDED:
            return tool_name in self.session_authorized
        # CRITICAL always requires fresh confirmation — never pre-authorized.
        return False
