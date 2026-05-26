from __future__ import annotations

import asyncio

import pytest

from loom.core.cognition.responses_policy import (
    RESPONSES_REASONING_DEFAULT_EFFORT,
    normalize_reasoning_effort,
)
from loom.core.cognition.responses_sse import (
    ResponsesProviderError,
    ResponsesStreamState,
    classify_responses_sse_error,
    iter_sse_events,
)


def test_response_failed_event_becomes_stream_error() -> None:
    state = ResponsesStreamState(provider="Codex Responses", model="gpt-5.5")
    data = {
        "type": "response.failed",
        "response": {
            "status": "failed",
            "error": {"code": "server_error", "message": "backend failed"},
        },
    }

    error = classify_responses_sse_error(state, "response.failed", data)

    assert error is not None
    assert error.failure_type == "stream_error_event"
    assert "backend failed" in str(error)
    assert "provider=Codex Responses" in str(error)
    assert "phase=" in str(error)


def test_non_max_output_incomplete_becomes_stream_incomplete() -> None:
    state = ResponsesStreamState(provider="xAI Responses", model="grok-4.3")
    data = {
        "type": "response.incomplete",
        "response": {
            "status": "incomplete",
            "incomplete_details": {"reason": "content_filter"},
        },
    }

    error = classify_responses_sse_error(state, "response.incomplete", data)

    assert error is not None
    assert error.failure_type == "stream_incomplete"
    assert "content_filter" in str(error)


def test_max_output_incomplete_is_not_provider_error() -> None:
    state = ResponsesStreamState(provider="Codex Responses", model="gpt-5.5")
    data = {
        "type": "response.incomplete",
        "response": {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
        },
    }

    assert classify_responses_sse_error(state, "response.incomplete", data) is None


async def _never_yields():
    while True:
        await asyncio.sleep(3600)
        yield "unreachable"


@pytest.mark.asyncio
async def test_first_event_watchdog_fires_when_no_sse_line_arrives() -> None:
    state = ResponsesStreamState(provider="Codex Responses", model="gpt-5.5")
    with pytest.raises(ResponsesProviderError) as exc:
        async for _event, _data in iter_sse_events(
            _never_yields(),
            state=state,
            first_event_timeout=0.01,
            idle_event_timeout=0.0,
        ):
            pass

    assert exc.value.failure_type == "ttfb_timeout"
    assert "first_event_wait" in str(exc.value)


async def _reasoning_then_silent():
    yield "event: response.reasoning_summary_text.delta"
    yield 'data: {"type":"response.reasoning_summary_text.delta","delta":"thinking"}'
    yield ""
    await asyncio.sleep(0.03)


@pytest.mark.asyncio
async def test_first_event_watchdog_stops_after_any_event_line() -> None:
    state = ResponsesStreamState(provider="Codex Responses", model="gpt-5.5")
    seen: list[str] = []

    async for event, data in iter_sse_events(
        _reasoning_then_silent(),
        state=state,
        first_event_timeout=0.01,
        idle_event_timeout=0.0,
    ):
        seen.append(event)
        assert data["delta"] == "thinking"

    assert seen == ["response.reasoning_summary_text.delta"]


async def _malformed_frame():
    yield "event: response.output_text.delta"
    yield "data: {not-json"
    yield ""


@pytest.mark.asyncio
async def test_malformed_complete_sse_frame_is_failure() -> None:
    state = ResponsesStreamState(provider="xAI Responses", model="grok-4.3")

    with pytest.raises(ResponsesProviderError) as exc:
        async for _event, _data in iter_sse_events(
            _malformed_frame(),
            state=state,
            first_event_timeout=0.01,
            idle_event_timeout=0.0,
        ):
            pass

    assert exc.value.failure_type == "malformed_sse"
    assert "response.output_text.delta" in str(exc.value)


def test_normalize_reasoning_effort_defaults_invalid_values() -> None:
    assert RESPONSES_REASONING_DEFAULT_EFFORT == "high"
    assert normalize_reasoning_effort(None) == "high"
    assert normalize_reasoning_effort("") == "high"
    assert normalize_reasoning_effort(" HIGH ") == "high"
    assert normalize_reasoning_effort("xhigh") == "xhigh"
    assert normalize_reasoning_effort("turbo") == "high"
