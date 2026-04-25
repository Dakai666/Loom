"""
Context Budget Manager.

Tracks token usage across a session and signals when the context window
is approaching its limit so the session can trigger compression before
performance degrades.

Token estimation uses the ~4 chars/token heuristic (accurate ±15% for
English; good enough for triggering compression thresholds).
"""

import json
from dataclasses import dataclass, field
from typing import Any


def estimate_tokens(obj: Any) -> int:
    """Rough token count for any serialisable object."""
    if isinstance(obj, str):
        return max(1, len(obj) // 4)
    try:
        return max(1, len(json.dumps(obj, ensure_ascii=False)) // 4)
    except (TypeError, ValueError):
        return 1


@dataclass
class ContextBudget:
    """
    Tracks token consumption and advises on compression.

    Parameters
    ----------
    total_tokens : int
        Maximum context window of the active model.
    compression_threshold : float
        Fraction of total_tokens at which `should_compress()` returns True.
        Default 0.80 (compress when 80% full).
    """

    total_tokens: int
    compression_threshold: float = 0.80
    used_tokens: int = field(default=0, init=False)

    # --- Accounting ---

    def record_response(self, input_tokens: int, output_tokens: int) -> None:
        """
        Update usage from provider-reported token counts.

        ``input_tokens`` is the *total* context the model processed this
        call — it already includes every prior message.  We therefore
        REPLACE (not add) used_tokens so the budget always reflects the
        real current window size, not a cumulative sum that would grow
        exponentially across turns.

        ``input_tokens == 0`` means "provider didn't report" (aborted
        stream, or MiniMax responses that omit usage), NOT "context is
        empty". Treat as a no-op so a stale-but-real reading isn't
        replaced by a phantom zero — same convention as
        :meth:`ContextLayoutDimension.update_total` in telemetry.py.
        Without this guard, ``should_compress()`` silently disarms after
        any zero-usage response and the history grows unbounded.
        """
        if input_tokens <= 0:
            return
        self.used_tokens = input_tokens + output_tokens

    def record_messages(self, messages: list[dict[str, Any]]) -> None:
        """Recount usage from the current message list (after compression)."""
        self.used_tokens = sum(estimate_tokens(m) for m in messages)

    def add(self, tokens: int) -> None:
        self.used_tokens += tokens

    # --- Queries ---

    @property
    def remaining(self) -> int:
        return max(0, self.total_tokens - self.used_tokens)

    @property
    def usage_fraction(self) -> float:
        return self.used_tokens / self.total_tokens if self.total_tokens else 0.0

    def should_compress(self) -> bool:
        return self.usage_fraction >= self.compression_threshold

    def fits(self, text: str) -> bool:
        return estimate_tokens(text) <= self.remaining

    def reset(self) -> None:
        self.used_tokens = 0

    def __str__(self) -> str:
        pct = self.usage_fraction * 100
        return (
            f"ContextBudget({self.used_tokens:,}/{self.total_tokens:,} tokens, "
            f"{pct:.1f}%{'  [compress]' if self.should_compress() else ''})"
        )
