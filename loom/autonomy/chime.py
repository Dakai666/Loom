"""
Chime — scheduled wake into an existing session (issue #369).

A chime is the autonomy daemon's way to nudge an *already-active* session
instead of opening a fresh independent one. The nudge is delivered as a
``<system_chime>``-wrapped message so the agent can tell at a glance that
this turn was triggered by a cron schedule, not by the user speaking.

This module is intentionally tiny: just the request dataclass and the
formatting helper. Routing, dispatch, and rendering live in the platform
layer (Discord bot for v1) so the autonomy package stays platform-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ChimeRequest:
    """One scheduled wake-up for a target session."""

    schedule_name: str
    intent: str
    fired_at: datetime
    target: dict[str, Any] = field(default_factory=dict)
    """Routing hint, e.g. ``{"type": "discord_thread", "id": "12345"}``."""
    trust_level: str | None = None
    allowed_tools: tuple[str, ...] = ()
    scope_grants: tuple[dict[str, Any], ...] = ()


def format_chime_content(req: ChimeRequest) -> str:
    """Build the ``<system_chime>`` wrapper the agent sees in its history.

    The wrapper is the contract that lets the agent (and any future tooling)
    detect "this turn was triggered by a schedule" without parsing free text.
    Keep the attribute set stable: schedule + fired_at are load-bearing for
    audits and dedupe.
    """
    iso = req.fired_at.isoformat()
    intent = req.intent.strip()
    return (
        f'<system_chime schedule="{req.schedule_name}" fired_at="{iso}">\n'
        f"{intent}\n"
        f"</system_chime>"
    )
