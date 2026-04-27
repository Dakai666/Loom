"""Regression tests for prompt-caching plumbing (issue #224, PR #229).

The PR initially had three execution-level bugs that an import + smoke test
would have caught immediately:

1. ``TurnDone`` dataclass: defaulted cache fields placed before non-default
   ``elapsed_ms`` ⇒ module fails to import.
2. ``status_bar`` f-string: a mid-string ``... if cond else ""`` consumed the
   adjacent string-literal segments via implicit concatenation, producing a
   half-empty status bar in *both* branches.
3. ``Discord _run_turn``: ``cache_tag`` referenced before being defined when
   the embed (detail) summary path was taken.

These tests guard the contracts that, if violated again, would re-introduce
those failures.
"""

from __future__ import annotations

from loom.core.events import TurnDone
from loom.core.cognition.providers import LLMResponse
from loom.platform.cli.ui import status_bar


# ── TurnDone dataclass ──────────────────────────────────────────────────────


class TestTurnDoneCacheFields:
    def test_construct_with_only_required_fields(self):
        # Defaulted cache fields must come AFTER all non-default fields, or
        # this construction (and the import itself) blows up.
        t = TurnDone(
            tool_count=1,
            input_tokens=100,
            output_tokens=50,
            elapsed_ms=1234.0,
        )
        assert t.cache_read_input_tokens == 0
        assert t.cache_creation_input_tokens == 0
        assert t.stop_reason == "complete"

    def test_construct_with_cache_fields(self):
        t = TurnDone(
            tool_count=1,
            input_tokens=100,
            output_tokens=50,
            elapsed_ms=1234.0,
            cache_read_input_tokens=8000,
            cache_creation_input_tokens=200,
        )
        assert t.cache_read_input_tokens == 8000
        assert t.cache_creation_input_tokens == 200


# ── LLMResponse cache fields ────────────────────────────────────────────────


class TestLLMResponseCacheFields:
    def test_defaults_zero(self):
        r = LLMResponse(
            text="hi", tool_uses=[], stop_reason="end_turn",
        )
        assert r.cache_read_input_tokens == 0
        assert r.cache_creation_input_tokens == 0

    def test_carry_explicit_values(self):
        r = LLMResponse(
            text="hi", tool_uses=[], stop_reason="end_turn",
            cache_read_input_tokens=5000,
            cache_creation_input_tokens=100,
        )
        assert r.cache_read_input_tokens == 5000
        assert r.cache_creation_input_tokens == 100


# ── status_bar rendering ────────────────────────────────────────────────────


class TestStatusBarSegments:
    """The PR-#229 noise: a mid-f-string conditional ate adjacent literals.

    Both branches must contain ALL of: context %, in/out tokens, elapsed,
    tool count. The cache segment is the only optional piece.
    """

    @staticmethod
    def _render(cache_pct: float = 0.0) -> str:
        return status_bar(
            context_fraction=0.5,
            input_tokens=100,
            output_tokens=50,
            elapsed_ms=1234.0,
            tool_count=2,
            cache_hit_pct=cache_pct,
        ).plain

    def test_cache_zero_keeps_all_other_segments(self):
        s = self._render(cache_pct=0.0)
        assert "context 50.0%" in s
        assert "100in / 50out" in s
        assert "1.2s" in s
        assert "2 tools" in s
        # cache segment hidden
        assert "cache" not in s

    def test_cache_nonzero_keeps_all_segments_and_adds_cache(self):
        s = self._render(cache_pct=80.0)
        assert "context 50.0%" in s
        assert "cache 80%" in s
        assert "100in / 50out" in s
        assert "1.2s" in s
        assert "2 tools" in s

    def test_cache_segment_position_between_context_and_io(self):
        # Order matters for readability — cache should sit next to context %.
        s = self._render(cache_pct=42.0)
        ctx_idx = s.index("context")
        cache_idx = s.index("cache 42%")
        io_idx = s.index("100in")
        assert ctx_idx < cache_idx < io_idx

    def test_singular_tool_label(self):
        s = status_bar(0.5, 100, 50, 1234.0, 1).plain
        assert "1 tool" in s and "tools" not in s

    def test_default_cache_pct_is_zero(self):
        # cache_hit_pct has a default — calls without it must still render
        # all required segments.
        s = status_bar(0.5, 100, 50, 1234.0, 2).plain
        assert "context" in s
        assert "100in / 50out" in s
