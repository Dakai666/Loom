"""
Context Budget Manager.

Tracks token + content-block usage across a session and signals when the
context window is approaching its limit so the session can trigger
compression before performance degrades.

Two pressure dimensions are tracked independently:

  • token usage — fraction of the model's context window consumed
  • content-block count — number of Anthropic-wire content blocks the
    next request would carry (Anthropic-compat endpoints — notably
    MiniMax — enforce a hard cap around 2013 blocks regardless of token
    budget). A heavy parallel-tool session can hit the block ceiling
    while the token meter still reads ~40%.

Token estimation is char-based with separate weights for CJK (≈1.5
chars/token) and everything else (≈4 chars/token). Mixed text is
weighted per-character. Good enough for triggering thresholds; the
authoritative count comes back from the provider via record_response().
"""

import json
from dataclasses import dataclass, field
from typing import Any

# MiniMax's Anthropic-compat endpoint rejects requests with more than
# ~2013 content blocks. Default to a conservative ceiling with headroom
# so compaction triggers before the wire actually overflows.
DEFAULT_MAX_BLOCKS = 2000


def _is_cjk(ch: str) -> bool:
    """Approximate CJK detection covering the common ranges.

    Covers CJK Unified Ideographs (4E00-9FFF), Hiragana/Katakana
    (3040-30FF), Hangul Syllables (AC00-D7AF), and CJK extension A
    (3400-4DBF). Skips rarer extensions — they're an order of magnitude
    less common in practice and not worth the branch cost.
    """
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3040 <= cp <= 0x30FF
        or 0xAC00 <= cp <= 0xD7AF
        or 0x3400 <= cp <= 0x4DBF
    )


def estimate_tokens(obj: Any) -> int:
    """Rough token count for any serialisable object.

    ASCII / Latin runs at ~4 chars/token; CJK at ~1.5 chars/token.
    Mixing both with a flat 4-char heuristic underestimates CJK-heavy
    payloads by ~60% and was a primary cause of usage_fraction drift
    after compaction in Chinese-language sessions.
    """
    if not isinstance(obj, str):
        try:
            obj = json.dumps(obj, ensure_ascii=False)
        except (TypeError, ValueError):
            return 1

    if not obj:
        return 1

    cjk_chars = 0
    other_chars = 0
    for ch in obj:
        if _is_cjk(ch):
            cjk_chars += 1
        else:
            other_chars += 1
    # int division keeps the heuristic feel but the CJK weight (3/2) is
    # tight enough that the previous test contract (400 ASCII chars →
    # 100 tokens) still holds.
    tokens = (other_chars // 4) + (cjk_chars * 2 // 3)
    return max(1, tokens)


def estimate_block_count(messages: list[dict[str, Any]]) -> int:
    """Approximate Anthropic-wire content blocks from OpenAI-canonical
    messages.

    Mapping rules (matches ``LLMProvider._to_anthropic_messages``):

      • system            → 1 text block (when content present)
      • user (str)        → 1 text block
      • user (list)       → len(content) blocks (already in block form)
      • assistant         → one ``thinking`` block per ``_thinking_blocks``
                            entry (Anthropic / MiniMax preserve these on
                            the wire to keep the reasoning chain across
                            turns); + 1 text block iff text is present;
                            + one ``tool_use`` block per entry in
                            tool_calls
      • tool              → 1 ``tool_result`` block

    Consecutive ``tool`` messages become a single wire user message on
    the way out but each still occupies one content block, so the count
    is what matters here — not message count.

    Used to gate compaction and to display the binding constraint in
    the UI.
    """
    n = 0
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "assistant":
            # Reasoning models (Anthropic thinking, MiniMax M2.x) emit
            # ``_thinking_blocks`` that ``_to_anthropic_messages`` puts
            # back on the wire ahead of the text/tool_use blocks. Each
            # thinking block is its own content block at the API
            # boundary, so it counts against the 2013 cap too.
            tbs = m.get("_thinking_blocks") or []
            n += len(tbs)
            if content:
                n += 1
            tcs = m.get("tool_calls") or []
            n += len(tcs)
        elif role == "tool":
            n += 1
        elif role == "system":
            if content:
                n += 1
        else:  # user (or anything unexpected)
            if isinstance(content, list):
                n += max(1, len(content))
            elif content:
                n += 1
            else:
                n += 1
    return n


@dataclass
class ContextBudget:
    """
    Tracks token + content-block consumption and advises on compression.

    Parameters
    ----------
    total_tokens : int
        Maximum context window of the active model.
    compression_threshold : float
        Fraction of total_tokens at which the token side of
        ``should_compress()`` fires. Default 0.80.
    max_blocks : int
        Hard ceiling on wire content blocks the next request can carry.
        Default ``DEFAULT_MAX_BLOCKS`` (sized for MiniMax's 2013 cap).
    block_threshold : float
        Fraction of max_blocks at which the block side of
        ``should_compress()`` fires. Default 0.85 — leaves ~300 blocks
        of headroom for the in-flight turn's growth.
    """

    total_tokens: int
    compression_threshold: float = 0.80
    max_blocks: int = DEFAULT_MAX_BLOCKS
    block_threshold: float = 0.85
    used_tokens: int = field(default=0, init=False)
    block_count: int = field(default=0, init=False)

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
        """Recount token + block usage from the current message list.

        Called after compaction and after any non-provider mutation of
        the message list (sanitize, observation masking, system-prompt
        refresh). Keeps both dimensions in sync so the UI doesn't show
        a stale reading until the next API call lands.
        """
        self.used_tokens = sum(estimate_tokens(m) for m in messages)
        self.block_count = estimate_block_count(messages)

    def update_block_count(self, messages: list[dict[str, Any]]) -> None:
        """Refresh only the block-count side from the current messages.

        Use when message structure changes (sanitize, masking) but a
        full token recount isn't desired — e.g. between turns when
        record_response is the authoritative token source.
        """
        self.block_count = estimate_block_count(messages)

    def add(self, tokens: int) -> None:
        self.used_tokens += tokens

    # --- Queries ---

    @property
    def remaining(self) -> int:
        return max(0, self.total_tokens - self.used_tokens)

    @property
    def usage_fraction(self) -> float:
        return self.used_tokens / self.total_tokens if self.total_tokens else 0.0

    @property
    def block_fraction(self) -> float:
        return self.block_count / self.max_blocks if self.max_blocks else 0.0

    @property
    def pressure(self) -> float:
        """The binding constraint — whichever dimension is fuller.

        This is what the UI footer should show: a 42% token reading
        while the block side sits at 87% is "really" an 87% session
        from the provider's perspective.
        """
        return max(self.usage_fraction, self.block_fraction)

    @property
    def block_bound(self) -> bool:
        """True iff block pressure exceeds token pressure — the session
        is about to hit the wire cap before the token cap."""
        return self.block_fraction > self.usage_fraction

    def should_compress(self) -> bool:
        return (
            self.usage_fraction >= self.compression_threshold
            or self.block_fraction >= self.block_threshold
        )

    def fits(self, text: str) -> bool:
        return estimate_tokens(text) <= self.remaining

    def reset(self) -> None:
        self.used_tokens = 0
        self.block_count = 0

    def format_pressure(self) -> str:
        """Compact footer string showing the binding constraint.

        Examples:
            ``"42.1%"``                     — token-bound, normal
            ``"42.1% · blocks 87.3%"``      — block-bound (wire near cap)

        UI surfaces (Discord embed footer, CLI footer) call this so the
        displayed % always tracks the side that's actually about to
        clip — not whichever dimension happens to be in the heuristic.
        """
        pct = self.pressure * 100
        if self.block_bound and self.block_count > 0:
            blk = self.block_fraction * 100
            return f"{pct:.1f}% · blocks {blk:.1f}%"
        return f"{pct:.1f}%"

    def __str__(self) -> str:
        pct = self.usage_fraction * 100
        blk = self.block_fraction * 100
        tag = "  [compress]" if self.should_compress() else ""
        return (
            f"ContextBudget({self.used_tokens:,}/{self.total_tokens:,} tokens "
            f"{pct:.1f}%, blocks {self.block_count}/{self.max_blocks} "
            f"{blk:.1f}%{tag})"
        )
