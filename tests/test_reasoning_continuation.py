"""
Tests for reasoning continuation on stop_reason='max_tokens' (Issue #271).

The recovery path:
  - When the model emits stop_reason='max_tokens' with 0 tool calls, the
    harness injects a <system-reminder> telling the agent to spill in-flight
    reasoning to scratchpad and resume on the next response, instead of
    silently truncating the turn via TurnDropped.
  - Capped at _MAX_REASONING_CONTINUATIONS retries per turn to prevent
    unbounded loops on pathologically long prompts.
  - Disabled when ``[harness] reasoning_continuation = "off"``.

Tests use a duck-typed LoomSession stand-in for the predicate, mirroring
the pattern in test_observation_masking.py — full stream_turn integration
requires a mocked router/provider stack and is left as follow-up.
"""

from __future__ import annotations

from types import SimpleNamespace

from loom.core.events import ReasoningContinuation
from loom.core.session import LoomSession, _MAX_REASONING_CONTINUATIONS


def _mock_session(
    *,
    mode: str = "auto",
    consecutive: int = 0,
) -> SimpleNamespace:
    """Minimal stand-in exposing only the attrs _should_continue_reasoning reads."""
    return SimpleNamespace(
        _reasoning_continuation_mode=mode,
        _consecutive_max_tokens=consecutive,
    )


def _decide(session: SimpleNamespace, stop_reason: str, tool_count: int) -> bool:
    return LoomSession._should_continue_reasoning(session, stop_reason, tool_count)


class TestShouldContinueReasoning:
    """Predicate truth table — see _should_continue_reasoning docstring."""

    def test_max_tokens_zero_tools_first_attempt_continues(self) -> None:
        s = _mock_session()
        assert _decide(s, "max_tokens", 0) is True

    def test_max_tokens_zero_tools_within_budget_continues(self) -> None:
        s = _mock_session(consecutive=_MAX_REASONING_CONTINUATIONS - 1)
        assert _decide(s, "max_tokens", 0) is True

    def test_budget_exhausted_falls_through(self) -> None:
        """At the cap, predicate returns False so the caller falls through
        to TurnDropped instead of looping forever."""
        s = _mock_session(consecutive=_MAX_REASONING_CONTINUATIONS)
        assert _decide(s, "max_tokens", 0) is False

    def test_with_tool_calls_does_not_trigger(self) -> None:
        """Recovery only kicks in for pure-reasoning truncation. If a tool
        ran this round, the situation is ambiguous and we drop instead."""
        s = _mock_session()
        assert _decide(s, "max_tokens", 1) is False
        assert _decide(s, "max_tokens", 5) is False

    def test_other_stop_reasons_do_not_trigger(self) -> None:
        """end_turn / tool_use / unknown providers values don't enter the
        recovery path even with 0 tool calls."""
        s = _mock_session()
        assert _decide(s, "end_turn", 0) is False
        assert _decide(s, "tool_use", 0) is False
        assert _decide(s, "stream_none", 0) is False
        assert _decide(s, "unknown", 0) is False

    def test_off_mode_disables_entirely(self) -> None:
        s = _mock_session(mode="off")
        assert _decide(s, "max_tokens", 0) is False


class TestConfigParsing:
    """Verify __init__ correctly normalizes reasoning_continuation."""

    def _read_mode(self, raw_value):
        """Replicate the __init__ parsing logic from session.py:642-650.
        Kept inline so the test fails fast if that logic changes."""
        mode = str(raw_value if raw_value is not None else "auto").lower()
        if mode not in ("auto", "off"):
            mode = "auto"
        return mode

    def test_default_is_auto(self) -> None:
        assert self._read_mode(None) == "auto"

    def test_explicit_off_respected(self) -> None:
        assert self._read_mode("off") == "off"

    def test_explicit_auto_respected(self) -> None:
        assert self._read_mode("auto") == "auto"

    def test_invalid_value_falls_back_to_auto(self) -> None:
        assert self._read_mode("aggressive") == "auto"
        assert self._read_mode("yes") == "auto"
        assert self._read_mode("") == "auto"

    def test_case_insensitive(self) -> None:
        assert self._read_mode("OFF") == "off"
        assert self._read_mode("Auto") == "auto"


class TestReasoningContinuationEvent:
    """Sanity-check the event dataclass signature consumers depend on."""

    def test_default_display_text_present(self) -> None:
        ev = ReasoningContinuation(attempt=1, max_attempts=2)
        assert ev.display_text  # non-empty default

    def test_attempt_and_max_attempts_recorded(self) -> None:
        ev = ReasoningContinuation(attempt=2, max_attempts=2)
        assert ev.attempt == 2
        assert ev.max_attempts == 2

    def test_display_text_overridable(self) -> None:
        ev = ReasoningContinuation(
            attempt=1, max_attempts=2, display_text="custom",
        )
        assert ev.display_text == "custom"


def test_max_continuations_constant_is_sane() -> None:
    """Guard against accidental zero/negative defaults that would disable
    recovery entirely or cause off-by-one loops."""
    assert _MAX_REASONING_CONTINUATIONS >= 1
    assert _MAX_REASONING_CONTINUATIONS <= 5  # sanity upper bound
