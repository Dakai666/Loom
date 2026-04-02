from dataclasses import dataclass, field
from enum import Enum


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
    def label(self) -> str:
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
