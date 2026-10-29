"""Repeated identical tool call nudge (Issue #654).

An agent can get stuck re-sending the exact call it just made — same tool,
same arguments — and receiving the exact same result, even after it has said
what it should do instead. Two live cases on ``chatgpt/gpt-6.1-terra``
(2026-10-29): ~50 identical ``recall`` calls in a CLI session, and 384
identical ``create_discord_forum_post`` failures in one morning-briefing turn
that ran 47 minutes and used up the provider quota.

Nothing in the harness noticed. This tracker counts, per user turn, how many
times each ``(tool, args, result)`` triple has been seen; from the
``THRESHOLD``-th occurrence on, the tool result the model reads carries a
one-line reminder, so the agent gets the chance to correct course itself.

The reminder alone is not trusted to stop a loop — the 384-call turn ignored
an error message that already named the cause. At ``HARD_LIMIT`` the result
says the turn is ending, ``limit_hit`` is set, and ``stream_turn`` ends the
turn once the current tool batch is complete (so every tool call keeps its
result). The call itself is never blocked.

A call whose arguments or result differ is a different triple and is never
counted: a retry that changes something is legitimate.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from loom.core.harness.middleware import ToolResult

THRESHOLD = 3
HARD_LIMIT = 10


def _digest(tool_name: str, args: dict[str, Any] | None, result: ToolResult) -> str:
    try:
        canon_args = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        canon_args = repr(args)
    metadata = result.metadata or {}
    # A JIT-spilled output is replaced by a placeholder whose scratchpad ref
    # is unique per call; compare the spilled content's digest instead.
    if metadata.get("jit_digest"):
        body = f"jit:{metadata['jit_digest']}"
    elif result.success:
        body = "" if result.output is None else str(result.output)
    else:
        body = result.error or ""
    h = hashlib.sha256()
    for part in (tool_name, canon_args, "ok" if result.success else "err", body):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


class RepeatCallTracker:
    """Turn-scoped counter of identical ``(tool, args, result)`` triples."""

    def __init__(self, threshold: int = THRESHOLD, hard_limit: int = HARD_LIMIT) -> None:
        self._threshold = threshold
        self._hard_limit = hard_limit
        self._counts: dict[str, int] = {}
        self.limit_hit: str | None = None
        """Set to a one-line description once a triple reaches the hard limit."""

    def reset(self) -> None:
        self._counts.clear()
        self.limit_hit = None

    def observe(self, tool_name: str, args: dict[str, Any] | None, result: ToolResult) -> str:
        """Record one completed call; return the suffix for its tool result.

        Empty below the threshold. Repeats past the threshold keep the
        reminder — the count in it rising is itself the signal.
        """
        key = _digest(tool_name, args, result)
        n = self._counts.get(key, 0) + 1
        self._counts[key] = n
        if n < self._threshold:
            return ""
        if n >= self._hard_limit:
            if self.limit_hit is None:
                self.limit_hit = (
                    f"{tool_name} was called {n} times this turn with identical "
                    f"arguments and returned the identical result each time"
                )
            return (
                f"\n\n[harness] This is call #{n} this turn of {tool_name} with "
                f"identical arguments and the identical result. The harness is "
                f"ending this turn now. Do not retry this call unchanged; next "
                f"turn, report what failed or take a different approach."
            )
        return (
            f"\n\n[harness] This is call #{n} this turn of {tool_name} with "
            f"identical arguments, and it returned the identical result. "
            f"Repeating it will not change the outcome — change the arguments "
            f"or the approach, or answer from what you already have."
        )
