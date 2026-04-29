"""
HarnessChannel — three-channel routing for harness-emitted messages.

Issue #236 PR-C. Centralises the decision *which channel* a harness
message belongs to:

- ``inline(message, level)``  →  flow stream, append-only, signed
                                  with ``⚙ harness ›`` prefix
- ``flash(message)``          →  footer ephemeral (PR-D); no-op in
                                  PR-C — green-light auth events
                                  simply don't print
- ``modal``                   →  intentionally not exposed here.
                                  Modal prompts (confirm / fatal
                                  recovery) live in
                                  ``loom.platform.cli.ui.select_prompt``

Why this abstraction
--------------------
Before PR-C the call sites scattered ``console.print(...)`` with
ad-hoc styling, and the routing decision (留底 vs 不留底) lived inside
human heads. This module makes the choice explicit at the call site,
lets us audit channel consistency by grepping ``harness_channel.``,
and gives PR-D a single seam to swap the no-op ``flash`` for a real
footer.

Levels
------
``info``     — neutral state change (sanitize, compaction summary)
``success``  — positive outcome (rare on inline; usually quiet)
``warning``  — denial / governor reject / non-fatal anomaly
``error``    — fatal / forensics-worthy
"""

from __future__ import annotations

from typing import Literal

from rich.console import Console
from rich.text import Text


Level = Literal["info", "success", "warning", "error"]

_LEVEL_TO_TOKEN: dict[Level, str] = {
    "info":    "loom.muted",
    "success": "loom.success",
    "warning": "loom.warning",
    "error":   "loom.error",
}


def render_harness_inline(message: str, level: Level = "info") -> Text:
    """Build the formatted Text for a single inline harness message.

    Public so callers that want to embed the formatted line into a
    larger Rich renderable (Panel, Table) can share the same look.
    """
    body_token = _LEVEL_TO_TOKEN[level]
    return Text.from_markup(
        f"[loom.harness.signature]⚙ harness ›[/loom.harness.signature] "
        f"[{body_token}]{message}[/{body_token}]"
    )


class HarnessChannel:
    """Routing front-end for harness-emitted messages.

    A single instance lives on the platform-cli surface (created in
    :func:`loom.platform.cli.main._chat`). Core / cognition layers do
    NOT import this directly — they emit via callbacks that the
    platform layer wires. Keeps the architecture-guard one-way rule
    intact.
    """

    def __init__(self, console: Console) -> None:
        self._console = console

    # ------------------------------------------------------------------
    # Inline — append-only,留底
    # ------------------------------------------------------------------

    def inline(self, message: str, *, level: Level = "info") -> None:
        """Print ``⚙ harness › <message>`` to the stream and leave it there."""
        self._console.print(render_harness_inline(message, level))

    # ------------------------------------------------------------------
    # Flash — ephemeral, deferred to PR-D
    # ------------------------------------------------------------------

    def flash(self, message: str) -> None:  # noqa: ARG002 — accept for API stability
        """Footer flash for green-light events.

        PR-C: **no-op**. Green-light auth (``pre-authorized`` /
        ``exec_auto`` / ``scope-allow``) intentionally produces no
        output during this PR. The footer arrives in PR-D and the
        flash will manifest there.

        Kept as a method (not omitted) so call sites can express
        intent — "this would flash if we had a footer" — and PR-D's
        wire-up is a one-method change here, not a hunt across the
        codebase.
        """
        return None
