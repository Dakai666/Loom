"""Reasoning-effort policy — the single home for effort vocabularies.

Two wire contracts live here side by side, deliberately not merged:

* **Responses API** (Codex / xAI): ``reasoning.effort`` is always sent;
  unset or invalid normalises to ``"high"``. Omitting it lets the backend
  reason unbounded (the gpt-5.5 stall root cause), so there is no "omit".
* **Anthropic-compatible** (``output_config.effort``, MiniMax-M3.1-Flash):
  unset or invalid normalises to ``None`` = omit the field and take the
  vendor default (``max``). ``none`` is not a level — that endpoint can't
  disable thinking and answers it with a 400. Adds ``max``; no ``minimal``.
"""

from __future__ import annotations

from typing import Any


RESPONSES_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
RESPONSES_REASONING_DEFAULT_EFFORT = "high"


def normalize_reasoning_effort(value: str | None) -> str:
    if not value:
        return RESPONSES_REASONING_DEFAULT_EFFORT
    candidate = str(value).strip().lower()
    if candidate in RESPONSES_REASONING_EFFORTS:
        return candidate
    return RESPONSES_REASONING_DEFAULT_EFFORT


ANTHROPIC_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


def normalize_anthropic_effort(value: Any) -> str | None:
    """Canonical ``output_config.effort`` level, or ``None`` (omit → vendor default)."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if candidate in ANTHROPIC_EFFORT_LEVELS else None
