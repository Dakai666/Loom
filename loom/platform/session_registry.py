"""
SessionRegistry — in-process index of active LoomSessions.

Lets the autonomy daemon find Discord thread sessions (or future CLI sessions)
so cron-triggered ``chime`` events can wake an existing conversation instead
of spawning an independent session.

Scope (v1):
- Single process only. The Discord bot and AutonomyDaemon must share an event
  loop (i.e. ``loom discord start --autonomy``).
- Sessions are registered under one or more string labels (e.g.
  ``discord_thread=<id>``). Lookup is exact match on a label pair.

Not in v1:
- Cross-process discovery, persistence, or RPC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loom.core.session import LoomSession


@dataclass(frozen=True)
class SessionInfo:
    """Read-only snapshot of one registered session."""

    session_id: str
    labels: dict[str, str]


class SessionRegistry:
    """In-process registry of live LoomSession instances."""

    def __init__(self) -> None:
        self._sessions: dict[str, "LoomSession"] = {}
        self._labels: dict[str, dict[str, str]] = {}

    def register(
        self,
        session_id: str,
        session: "LoomSession",
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Add or replace a session entry. Existing labels are overwritten."""
        self._sessions[session_id] = session
        self._labels[session_id] = dict(labels or {})

    def unregister(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._labels.pop(session_id, None)

    def get(self, session_id: str) -> "LoomSession | None":
        return self._sessions.get(session_id)

    def find_by_label(self, key: str, value: str) -> list["LoomSession"]:
        """Return every session whose label ``key`` equals ``value``."""
        out: list["LoomSession"] = []
        for sid, labels in self._labels.items():
            if labels.get(key) == value:
                sess = self._sessions.get(sid)
                if sess is not None:
                    out.append(sess)
        return out

    def list_active(self) -> list[SessionInfo]:
        return [
            SessionInfo(session_id=sid, labels=dict(self._labels.get(sid, {})))
            for sid in self._sessions
        ]


_registry: SessionRegistry | None = None


def get_registry() -> SessionRegistry:
    """Module-level singleton."""
    global _registry
    if _registry is None:
        _registry = SessionRegistry()
    return _registry


def reset_registry() -> None:
    """Test helper — drops all registered sessions."""
    global _registry
    _registry = None
