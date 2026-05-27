"""
Circadian Autonomy — the daily-life rhythm layer (milestone #14, epic #458).

This package adds Loom's missing continuous life line on top of the existing
autonomy primitives, without reinventing any of them:

    wake → live → weave → sleep → remember → wake again

It is *surgical addition*: triggers come from ``loom.autonomy.triggers``,
delivery from ``loom.autonomy.chime`` + the Discord bot, scheduling from
``AutonomyDaemon``. Circadian only supplies the daily-session bookkeeping
(``CircadianState``), the idempotent lifecycle entry point
(``ensure_today_session``), and the wiring that registers the dawn/sleep
cron triggers with deterministic handlers.

PR 1 scope (issue #459): state + lifecycle skeleton only. Phase chime, cheap
gate, weave plan, journal land in later PRs (#460–#463).
"""

from __future__ import annotations

from loom.autonomy.circadian.state import CircadianState

__all__ = ["CircadianState"]
