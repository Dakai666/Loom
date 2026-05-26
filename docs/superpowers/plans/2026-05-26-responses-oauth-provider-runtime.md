# Responses OAuth Provider Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split Codex OAuth and xAI OAuth Responses runtimes into provider-specific modules, add shared Responses SSE diagnostics/watchdogs, and preserve the normalized `LLMResponse` contract.

**Architecture:** Keep Anthropic, MiniMax, and OpenAI-compatible providers in `providers.py`; only Codex/xAI Responses code moves out. Shared code is split into neutral SSE mechanics in `responses_sse.py` and shared Responses policy in `responses_policy.py`, so provider files own payload and auth decisions while session only sees normalized stream events, final responses, or structured provider drops.

**Tech Stack:** Python 3.12, async iterators, `httpx.AsyncClient.stream()`, pytest, GitNexus CLI, existing Loom provider/router/session contracts.

---

## Locked Decisions

- Codex and xAI live in separate files: `codex_responses_provider.py` and `xai_responses_provider.py`.
- `providers.py` remains the home for `AnthropicProvider`, `_OpenAICompatibleBase`, OpenAI/OpenRouter/Ollama/LMStudio, `_retry_async`, and `_to_anthropic_messages`.
- `providers.py` re-exports `CodexResponsesProvider` and `XAIResponsesProvider`; existing imports from `loom.core.cognition.providers` remain valid.
- `responses_sse.py` is provider-neutral. It may store a provider label in diagnostics, but it must not branch on provider name.
- `_normalize_reasoning_effort` and reasoning effort constants move to `responses_policy.py`, not `responses_sse.py`.
- TTFB watchdog uses first SSE event line as the marker, not first output token. Default first-event timeout is 45 seconds. Idle timeout is implemented but disabled by default (`0.0`) to avoid killing long high-effort reasoning after a `response.created` event.
- Malformed complete SSE frames become `ResponsesProviderError(failure_type="malformed_sse")`. This intentionally replaces silent `json.JSONDecodeError: continue` because silent protocol drift is the failure mode being fixed.
- Codex keeps `SUPPORTS_MAX_OUTPUT_TOKENS = False`; xAI keeps `SUPPORTS_MAX_OUTPUT_TOKENS = True` and sends `max_output_tokens` explicitly.
- `response.incomplete` with `incomplete_details.reason == "max_output_tokens"` is not a provider error. It must continue producing `LLMResponse.stop_reason == "max_tokens"` so Issue #271 reasoning continuation remains intact.
- Provider failures use `ResponsesProviderError`, and `LoomSession.stream_turn()` converts that exception into a `TurnDropped` event with a concise provider detail. `LLMResponse` shape is unchanged.

## File Map

- Create `loom/core/cognition/responses_sse.py`: neutral SSE frame parsing, phase state, HTTP/transport diagnostics, first-event watchdog, idle watchdog, `ResponsesProviderError`, `_ReasoningStreamWrapper`.
- Create `loom/core/cognition/responses_policy.py`: reasoning effort constants and `normalize_reasoning_effort()`.
- Create `loom/core/cognition/codex_responses_provider.py`: Codex OAuth credential loading, Codex payload policy, Codex stream consumption.
- Create `loom/core/cognition/xai_responses_provider.py`: xAI OAuth credential loading, xAI payload policy, xAI stream consumption.
- Modify `loom/core/cognition/providers.py`: remove Codex/xAI implementations and moved helpers; import/re-export Codex/xAI providers; keep Anthropic/OpenAI-compatible code unchanged.
- Modify `loom/core/session.py`: catch `ResponsesProviderError` around `router.stream_chat()` and yield a diagnostic `TurnDropped`.
- Modify `loom/core/events.py`: add optional `provider_error_detail: str = ""` to `TurnDropped`.
- Modify `loom/platform/discord/bot.py`: include provider detail in Discord `TurnDropped` status when available.
- Create `tests/test_responses_sse.py`: shared parser/error/watchdog tests.
- Update `tests/test_codex_responses_provider.py`: import compatibility, Codex payload, Codex SSE failure and watchdog behavior.
- Update `tests/test_xai_responses_provider.py`: import compatibility, xAI payload, xAI entitlement classification, xAI watchdog behavior.
- Update `tests/test_session.py`: provider error to `TurnDropped` contract.
- Create `tests/test_responses_oauth_live.py`: opt-in live OAuth smoke tests skipped unless `LOOM_LIVE_OAUTH_SMOKE=1`.

## Task 0: Open the Draft PR Before Runtime Edits

**Files:**
- No runtime files.

- [ ] **Step 1: Confirm the branch contains only spec/plan work**

Run:

```bash
git status --short --branch
```

Expected:

```text
## codex/xai-oauth-runtime-cleanup
```

- [ ] **Step 2: Commit this plan**

Run:

```bash
git add docs/superpowers/plans/2026-05-26-responses-oauth-provider-runtime.md
git commit -m "docs: plan responses oauth runtime cleanup"
```

Expected: one docs commit.

- [ ] **Step 3: Push the planning branch**

Run:

```bash
git push -u origin codex/xai-oauth-runtime-cleanup
```

Expected: branch pushed to `origin/codex/xai-oauth-runtime-cleanup`.

- [ ] **Step 4: Open the draft PR**

Run:

```bash
gh pr create --repo Dakai666/Loom --base master --head codex/xai-oauth-runtime-cleanup --draft --title "refactor(cognition): split Responses OAuth providers" --body $'## Summary\n- split Codex and xAI Responses OAuth runtime into provider-specific modules\n- add shared Responses SSE diagnostics and watchdog plan before implementation\n- keep Anthropic/MiniMax out of this PR except compatibility imports\n\n## Verification\n- planning only so far\n\nFollow-up planned: Anthropic/MiniMax provider contract extraction after Codex/xAI stabilize.'
```

Expected: draft PR URL is printed.

## Task 1: Run GitNexus Impact Analysis Before Runtime Edits

**Files:**
- No file edits.

- [ ] **Step 1: Impact Codex provider**

Run:

```bash
npx gitnexus impact CodexResponsesProvider --direction upstream
```

Expected: direct callers include the provider router/session path and Codex provider tests. If risk is HIGH or CRITICAL, stop and report the blast radius before editing.

- [ ] **Step 2: Impact xAI provider**

Run:

```bash
npx gitnexus impact XAIResponsesProvider --direction upstream
```

Expected: direct callers include the provider router/session path and xAI provider tests. If risk is HIGH or CRITICAL, stop and report the blast radius before editing.

- [ ] **Step 3: Impact shared Responses helpers**

Run:

```bash
npx gitnexus impact _responses_sse_error_detail --direction upstream
npx gitnexus impact _ReasoningStreamWrapper --direction upstream
npx gitnexus impact _normalize_reasoning_effort --direction upstream
```

Expected: callers are Codex/xAI provider code only. If `_retry_async` or `_to_anthropic_messages` appears in the affected set, stop because Anthropic/MiniMax is leaking into scope.

- [ ] **Step 4: Impact session drop event changes**

Run:

```bash
npx gitnexus impact TurnDropped --direction upstream
npx gitnexus impact stream_turn --direction upstream
```

Expected: affected consumers include CLI/Discord event rendering and session tests. If risk is HIGH or CRITICAL, report before editing because this touches user-visible event flow.

## Task 2: Add Shared Responses SSE Tests First

**Files:**
- Create: `tests/test_responses_sse.py`

- [ ] **Step 1: Create failing shared SSE tests**

Add `tests/test_responses_sse.py`:

```python
import asyncio

import pytest

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
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
pytest -q tests/test_responses_sse.py
```

Expected: import failure for `loom.core.cognition.responses_sse`.

## Task 3: Implement Neutral Responses SSE Module

**Files:**
- Create: `loom/core/cognition/responses_sse.py`
- Test: `tests/test_responses_sse.py`

- [ ] **Step 1: Add `responses_sse.py`**

Create `loom/core/cognition/responses_sse.py`:

```python
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


DEFAULT_FIRST_EVENT_TIMEOUT = 45.0
DEFAULT_IDLE_EVENT_TIMEOUT = 0.0


@dataclass
class ResponsesStreamState:
    provider: str
    model: str
    phase: str = "request_built"
    started_at: float = 0.0
    first_event_at: float | None = None
    last_event_at: float | None = None
    last_event_type: str = ""
    text_delta_count: int = 0
    tool_delta_count: int = 0

    def __post_init__(self) -> None:
        if self.started_at <= 0.0:
            self.started_at = time.monotonic()

    def mark_phase(self, phase: str) -> None:
        self.phase = phase

    def mark_event_line(self, event_type: str) -> None:
        now = time.monotonic()
        if self.first_event_at is None:
            self.first_event_at = now
        self.last_event_at = now
        self.last_event_type = event_type

    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)


class ResponsesProviderError(RuntimeError):
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        failure_type: str,
        phase: str,
        detail: str,
        last_event_type: str = "",
        elapsed_seconds: float = 0.0,
    ) -> None:
        self.provider = provider
        self.model = model
        self.failure_type = failure_type
        self.phase = phase
        self.detail = detail
        self.last_event_type = last_event_type
        self.elapsed_seconds = elapsed_seconds
        parts = [
            f"provider={provider}",
            f"model={model}",
            f"failure_type={failure_type}",
            f"phase={phase}",
        ]
        if last_event_type:
            parts.append(f"last_event={last_event_type}")
        parts.append(f"elapsed={elapsed_seconds:.1f}s")
        if detail:
            parts.append(f"detail={detail[:1000]}")
        super().__init__("; ".join(parts))

    @classmethod
    def from_state(
        cls,
        state: ResponsesStreamState,
        *,
        failure_type: str,
        detail: str,
    ) -> "ResponsesProviderError":
        return cls(
            provider=state.provider,
            model=state.model,
            failure_type=failure_type,
            phase=state.phase,
            detail=detail,
            last_event_type=state.last_event_type,
            elapsed_seconds=state.elapsed_seconds(),
        )


class _ReasoningStreamWrapper:
    __slots__ = ("_open",)

    def __init__(self) -> None:
        self._open = False

    def open_chunk(self, delta: str) -> str:
        if not self._open:
            self._open = True
            return f"<think>{delta}"
        return delta

    def close(self) -> str:
        if self._open:
            self._open = False
            return "</think>"
        return ""

    @property
    def is_open(self) -> bool:
        return self._open


def stream_interrupt_detail(provider: str, exc: BaseException) -> str:
    detail = str(exc).strip() or type(exc).__name__
    return f"{provider} stream interrupted: {detail}"


async def http_status_detail(provider: str, exc: Any) -> str:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", "unknown")
    body = ""
    if response is not None:
        aread = getattr(response, "aread", None)
        if callable(aread):
            try:
                await aread()
            except Exception:
                pass
        try:
            body = str(getattr(response, "text", "") or "").strip()
        except Exception:
            body = ""
    detail = f"{provider} request failed with HTTP {status_code}"
    if body:
        detail += f": {body[:1000]}"
    return detail


def _error_detail(data: dict[str, Any], event_type: str, status: str) -> str:
    response = data.get("response")
    response = response if isinstance(response, dict) else {}
    error = data.get("error") or response.get("error")
    incomplete = response.get("incomplete_details")

    if isinstance(error, dict):
        message = error.get("message") or error.get("detail") or error.get("reason")
        code = error.get("code") or error.get("type")
        if code and message:
            return f"{code}: {message}"
        if message:
            return str(message)
        if code:
            return str(code)
        return json.dumps(error, ensure_ascii=False)
    if error:
        return str(error)
    if isinstance(incomplete, dict):
        return json.dumps(incomplete, ensure_ascii=False)
    if data.get("message"):
        return str(data.get("message"))
    if status:
        return f"{event_type} status={status}"
    return event_type or "response error"


def classify_responses_sse_error(
    state: ResponsesStreamState,
    event_type: str,
    data: dict[str, Any],
) -> ResponsesProviderError | None:
    response = data.get("response")
    response = response if isinstance(response, dict) else {}
    status = str(response.get("status") or "").lower()
    error = data.get("error") or response.get("error")
    incomplete = response.get("incomplete_details")

    if status == "incomplete" and isinstance(incomplete, dict):
        reason = str(incomplete.get("reason") or "").strip()
        if reason == "max_output_tokens":
            return None
        if reason:
            return ResponsesProviderError.from_state(
                state,
                failure_type="stream_incomplete",
                detail=_error_detail(data, event_type, status),
            )

    failed_event = (
        event_type in {"error", "response.failed"}
        or event_type.endswith(".failed")
        or status in {"failed", "cancelled"}
        or error is not None
    )
    if not failed_event:
        return None

    return ResponsesProviderError.from_state(
        state,
        failure_type="stream_error_event",
        detail=_error_detail(data, event_type, status),
    )


def classify_xai_http_failure(body: str) -> str:
    haystack = body.lower()
    markers = ("quota", "entitlement", "subscription", "rate limit", "billing")
    if any(marker in haystack for marker in markers):
        return "entitlement_or_quota"
    return "http_error"


async def _next_line_with_timeout(
    iterator: AsyncIterator[str],
    *,
    timeout: float,
) -> str:
    return await asyncio.wait_for(iterator.__anext__(), timeout=timeout)


async def iter_sse_events(
    lines: AsyncIterator[str],
    *,
    state: ResponsesStreamState,
    first_event_timeout: float = DEFAULT_FIRST_EVENT_TIMEOUT,
    idle_event_timeout: float = DEFAULT_IDLE_EVENT_TIMEOUT,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    state.mark_phase("first_event_wait")
    iterator = lines.__aiter__()
    event: str | None = None
    data_lines: list[str] = []

    while True:
        timeout = 0.0
        if state.first_event_at is None:
            timeout = first_event_timeout
        elif idle_event_timeout > 0.0:
            timeout = idle_event_timeout

        try:
            if timeout > 0.0:
                line = await _next_line_with_timeout(iterator, timeout=timeout)
            else:
                line = await iterator.__anext__()
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError as exc:
            failure_type = "ttfb_timeout" if state.first_event_at is None else "idle_timeout"
            raise ResponsesProviderError.from_state(
                state,
                failure_type=failure_type,
                detail=f"no SSE event within {timeout:.1f}s",
            ) from exc

        if line.startswith("event: "):
            event = line[7:]
            state.mark_event_line(event)
            if event.endswith("reasoning_summary_text.delta"):
                state.mark_phase("reasoning_stream")
            elif event.endswith("output_text.delta"):
                state.mark_phase("output_stream")
            elif event.endswith("function_call_arguments.delta"):
                state.mark_phase("tool_call_stream")
                state.tool_delta_count += 1
        elif line.startswith("data: "):
            data_lines.append(line[6:])
        elif line == "" and event:
            raw = "\n".join(data_lines)
            current_event = event
            event = None
            data_lines = []
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ResponsesProviderError.from_state(
                    state,
                    failure_type="malformed_sse",
                    detail=f"{current_event}: {raw[:1000]}",
                ) from exc
            event_type = str(data.get("type") or current_event)
            if error := classify_responses_sse_error(state, event_type, data):
                raise error
            if event_type.endswith("output_text.delta"):
                state.text_delta_count += 1
            yield event_type, data
```

- [ ] **Step 2: Run shared SSE tests**

Run:

```bash
pytest -q tests/test_responses_sse.py
```

Expected: `6 passed`.

- [ ] **Step 3: Commit shared SSE module and tests**

Run:

```bash
git add loom/core/cognition/responses_sse.py tests/test_responses_sse.py
git commit -m "feat(cognition): add responses sse diagnostics"
```

Expected: commit succeeds.

## Task 4: Add Shared Responses Policy Module

**Files:**
- Create: `loom/core/cognition/responses_policy.py`
- Test: `tests/test_responses_sse.py`

- [ ] **Step 1: Add policy tests**

Append to `tests/test_responses_sse.py`:

```python
from loom.core.cognition.responses_policy import (
    RESPONSES_REASONING_DEFAULT_EFFORT,
    normalize_reasoning_effort,
)


def test_normalize_reasoning_effort_defaults_invalid_values() -> None:
    assert RESPONSES_REASONING_DEFAULT_EFFORT == "high"
    assert normalize_reasoning_effort(None) == "high"
    assert normalize_reasoning_effort("") == "high"
    assert normalize_reasoning_effort(" HIGH ") == "high"
    assert normalize_reasoning_effort("xhigh") == "xhigh"
    assert normalize_reasoning_effort("turbo") == "high"
```

- [ ] **Step 2: Run policy test and verify it fails**

Run:

```bash
pytest -q tests/test_responses_sse.py::test_normalize_reasoning_effort_defaults_invalid_values
```

Expected: import failure for `loom.core.cognition.responses_policy`.

- [ ] **Step 3: Add `responses_policy.py`**

Create `loom/core/cognition/responses_policy.py`:

```python
from __future__ import annotations


RESPONSES_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
RESPONSES_REASONING_DEFAULT_EFFORT = "high"


def normalize_reasoning_effort(value: str | None) -> str:
    if not value:
        return RESPONSES_REASONING_DEFAULT_EFFORT
    candidate = str(value).strip().lower()
    if candidate in RESPONSES_REASONING_EFFORTS:
        return candidate
    return RESPONSES_REASONING_DEFAULT_EFFORT
```

- [ ] **Step 4: Run policy tests**

Run:

```bash
pytest -q tests/test_responses_sse.py
```

Expected: `7 passed`.

- [ ] **Step 5: Commit policy module**

Run:

```bash
git add loom/core/cognition/responses_policy.py tests/test_responses_sse.py
git commit -m "feat(cognition): add responses reasoning policy"
```

Expected: commit succeeds.

## Task 5: Extract Codex Responses Provider

**Files:**
- Create: `loom/core/cognition/codex_responses_provider.py`
- Modify: `loom/core/cognition/providers.py`
- Test: `tests/test_codex_responses_provider.py`

- [ ] **Step 1: Preserve Codex import compatibility in tests**

Confirm `tests/test_codex_responses_provider.py` keeps this import:

```python
from loom.core.cognition.providers import CodexResponsesProvider
```

If the file imports directly from `codex_responses_provider.py`, change it back to the compatibility path above.

- [ ] **Step 2: Add Codex watchdog test**

Add this test to `tests/test_codex_responses_provider.py` using the file's existing fake client style:

```python
@pytest.mark.asyncio
async def test_codex_stream_chat_raises_ttfb_timeout(monkeypatch):
    provider = CodexResponsesProvider()
    monkeypatch.setattr(provider, "_load_bearer_token", lambda: "token")
    provider._first_event_timeout = 0.01

    class HangingResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            while True:
                await asyncio.sleep(3600)
                yield "unreachable"

    class StreamContext:
        async def __aenter__(self):
            return HangingResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, *args, **kwargs):
            return StreamContext()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    with pytest.raises(RuntimeError) as exc:
        async for _chunk, _final in provider.stream_chat([{"role": "user", "content": "hi"}]):
            pass

    assert "failure_type=ttfb_timeout" in str(exc.value)
    assert "provider=Codex Responses" in str(exc.value)
```

- [ ] **Step 3: Run Codex tests and verify they fail before extraction**

Run:

```bash
pytest -q tests/test_codex_responses_provider.py::test_codex_stream_chat_raises_ttfb_timeout
```

Expected: failure because current provider has no first-event watchdog.

- [ ] **Step 4: Create `codex_responses_provider.py`**

Move the full `CodexResponsesProvider` class from `providers.py` into `loom/core/cognition/codex_responses_provider.py`. The new file must import only the shared provider types and shared Responses helpers:

```python
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from .providers import LLMProvider, LLMResponse, ToolUse
from .responses_policy import normalize_reasoning_effort
from .responses_sse import (
    DEFAULT_FIRST_EVENT_TIMEOUT,
    DEFAULT_IDLE_EVENT_TIMEOUT,
    ResponsesProviderError,
    ResponsesStreamState,
    _ReasoningStreamWrapper,
    http_status_detail,
    iter_sse_events,
    stream_interrupt_detail,
)
```

In `CodexResponsesProvider.__init__()`, add these attributes:

```python
self._first_event_timeout = DEFAULT_FIRST_EVENT_TIMEOUT
self._idle_event_timeout = DEFAULT_IDLE_EVENT_TIMEOUT
```

Replace the manual `event/data_lines/json.loads` loop in `stream_chat()` with:

```python
state = ResponsesStreamState(provider="Codex Responses", model=self._api_model())
state.mark_phase("request_sent")
async with httpx.AsyncClient(timeout=self._timeout) as client:
    async with client.stream(
        "POST",
        self._base_url,
        headers={
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        json=payload,
    ) as resp:
        state.mark_phase("headers_received")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ResponsesProviderError.from_state(
                state,
                failure_type="http_error",
                detail=await http_status_detail("Codex Responses", exc),
            ) from exc
        try:
            async for event_type, data in iter_sse_events(
                resp.aiter_lines(),
                state=state,
                first_event_timeout=self._first_event_timeout,
                idle_event_timeout=self._idle_event_timeout,
            ):
                if abort_signal is not None and abort_signal.is_set():
                    break
                delta = data.get("delta")
                if isinstance(delta, str) and event_type.endswith("reasoning_summary_text.delta"):
                    yield (reasoning_wrapper.open_chunk(delta), None)
                elif (
                    event_type.endswith("reasoning_summary_text.done")
                    or event_type.endswith("reasoning_summary_part.done")
                ):
                    closing = reasoning_wrapper.close()
                    if closing:
                        yield (closing, None)
                elif isinstance(delta, str) and event_type.endswith("output_text.delta"):
                    closing = reasoning_wrapper.close()
                    if closing:
                        yield (closing, None)
                    full_content += delta
                    yield (delta, None)
                item = data.get("item")
                if isinstance(item, dict) and event_type.endswith("output_item.done"):
                    output_items.append(item)
                response = data.get("response")
                if isinstance(response, dict):
                    response_status = str(response.get("status") or response_status)
                    usage = response.get("usage") or {}
                    if isinstance(usage, dict):
                        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or input_tokens)
                        output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or output_tokens)
        except httpx.HTTPError as exc:
            raise ResponsesProviderError.from_state(
                state,
                failure_type="provider_protocol_error",
                detail=stream_interrupt_detail("Codex Responses", exc),
            ) from exc
```

Keep the existing final `LLMResponse` construction, including:

```python
stop_reason = "tool_use" if tool_uses else "end_turn"
if response_status == "incomplete":
    stop_reason = "max_tokens"
```

- [ ] **Step 5: Re-export Codex from `providers.py`**

Remove the old `CodexResponsesProvider` class body from `providers.py`. Add this import after `LLMProvider` and `LLMResponse` are defined and before code that needs provider registration:

```python
from .codex_responses_provider import CodexResponsesProvider
```

If the import creates a circular import at module import time, move the import to the bottom of `providers.py` after `LMStudioProvider` and before `__all__` or module end. Keep the public name `CodexResponsesProvider`.

- [ ] **Step 6: Run Codex tests**

Run:

```bash
pytest -q tests/test_codex_responses_provider.py
```

Expected: all Codex provider tests pass, including import from `loom.core.cognition.providers`.

- [ ] **Step 7: Commit Codex extraction**

Run:

```bash
git add loom/core/cognition/codex_responses_provider.py loom/core/cognition/providers.py tests/test_codex_responses_provider.py
git commit -m "refactor(cognition): extract codex responses provider"
```

Expected: commit succeeds.

## Task 6: Extract xAI Responses Provider

**Files:**
- Create: `loom/core/cognition/xai_responses_provider.py`
- Modify: `loom/core/cognition/providers.py`
- Test: `tests/test_xai_responses_provider.py`

- [ ] **Step 1: Preserve xAI import compatibility in tests**

Confirm `tests/test_xai_responses_provider.py` keeps this import:

```python
from loom.core.cognition.providers import CodexResponsesProvider, XAIResponsesProvider
```

- [ ] **Step 2: Add xAI entitlement and watchdog tests**

Add this HTTP classification test to `tests/test_xai_responses_provider.py`:

```python
from loom.core.cognition.responses_sse import classify_xai_http_failure


def test_xai_http_body_classifies_entitlement_or_quota() -> None:
    body = '{"error":{"message":"subscription does not include this model quota"}}'

    assert classify_xai_http_failure(body) == "entitlement_or_quota"
```

Add this watchdog test using the same fake client pattern as Codex:

```python
@pytest.mark.asyncio
async def test_xai_stream_chat_raises_ttfb_timeout(monkeypatch):
    provider = XAIResponsesProvider()
    monkeypatch.setattr(provider, "_load_bearer_token", lambda: "token")
    provider._first_event_timeout = 0.01

    class HangingResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            while True:
                await asyncio.sleep(3600)
                yield "unreachable"

    class StreamContext:
        async def __aenter__(self):
            return HangingResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, *args, **kwargs):
            return StreamContext()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    with pytest.raises(RuntimeError) as exc:
        async for _chunk, _final in provider.stream_chat([{"role": "user", "content": "hi"}]):
            pass

    assert "failure_type=ttfb_timeout" in str(exc.value)
    assert "provider=xAI Responses" in str(exc.value)
```

- [ ] **Step 3: Run xAI tests and verify watchdog fails before extraction**

Run:

```bash
pytest -q tests/test_xai_responses_provider.py::test_xai_stream_chat_raises_ttfb_timeout
```

Expected: failure because current provider has no first-event watchdog.

- [ ] **Step 4: Create `xai_responses_provider.py`**

Move the full `XAIResponsesProvider` class from `providers.py` into `loom/core/cognition/xai_responses_provider.py`. The new file uses the same imports as Codex plus `classify_xai_http_failure`:

```python
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from .providers import LLMProvider, LLMResponse, ToolUse
from .responses_policy import normalize_reasoning_effort
from .responses_sse import (
    DEFAULT_FIRST_EVENT_TIMEOUT,
    DEFAULT_IDLE_EVENT_TIMEOUT,
    ResponsesProviderError,
    ResponsesStreamState,
    _ReasoningStreamWrapper,
    classify_xai_http_failure,
    http_status_detail,
    iter_sse_events,
    stream_interrupt_detail,
)
```

Add explicit token-cap policy:

```python
SUPPORTS_MAX_OUTPUT_TOKENS = True
```

Build payload with:

```python
if self.SUPPORTS_MAX_OUTPUT_TOKENS:
    payload["max_output_tokens"] = max_tokens
payload["reasoning"] = {"effort": self._reasoning_effort}
```

Use the same `ResponsesStreamState` and `iter_sse_events()` pattern from Codex. In the HTTP error block, set the failure type from the body:

```python
detail = await http_status_detail("xAI Responses", exc)
raise ResponsesProviderError.from_state(
    state,
    failure_type=classify_xai_http_failure(detail),
    detail=detail,
) from exc
```

Keep final `LLMResponse.stop_reason == "max_tokens"` when `response_status == "incomplete"`.

- [ ] **Step 5: Re-export xAI from `providers.py`**

Remove the old `XAIResponsesProvider` class body from `providers.py`. Add:

```python
from .xai_responses_provider import XAIResponsesProvider
```

Place the import beside the Codex re-export location.

- [ ] **Step 6: Run xAI tests**

Run:

```bash
pytest -q tests/test_xai_responses_provider.py
```

Expected: all xAI provider tests pass, including import from `loom.core.cognition.providers`.

- [ ] **Step 7: Commit xAI extraction**

Run:

```bash
git add loom/core/cognition/xai_responses_provider.py loom/core/cognition/providers.py tests/test_xai_responses_provider.py
git commit -m "refactor(cognition): extract xai responses provider"
```

Expected: commit succeeds.

## Task 7: Surface Provider Diagnostics Through Session Drops

**Files:**
- Modify: `loom/core/events.py`
- Modify: `loom/core/session.py`
- Modify: `loom/platform/discord/bot.py`
- Test: `tests/test_session.py`

- [ ] **Step 1: Add session contract test**

Add to `tests/test_session.py` near existing `stream_turn` drop tests:

```python
@pytest.mark.asyncio
async def test_stream_turn_surfaces_responses_provider_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from loom.core import session as session_module
    from loom.core.cognition.responses_sse import ResponsesProviderError
    from loom.core.events import TurnDropped
    from loom.core.session import LoomSession

    async def failing_stream_chat(**kwargs):
        raise ResponsesProviderError(
            provider="Codex Responses",
            model="gpt-5.5",
            failure_type="ttfb_timeout",
            phase="first_event_wait",
            detail="no SSE event within 45.0s",
            elapsed_seconds=45.0,
        )
        yield "", None

    router = SimpleNamespace(
        stream_chat=failing_stream_chat,
        native_max_tokens=lambda model: None,
    )
    monkeypatch.setattr(session_module, "build_router", lambda *args, **kwargs: router)
    monkeypatch.setattr(session_module, "_load_loom_config", lambda: {})

    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = LoomSession(
        model="codex/gpt-5.5",
        db_path=str(tmp_path / "loom.db"),
        workspace=workspace,
    )

    class FakeEpisodic:
        async def write(self, entry):
            return None

    session._memory = SimpleNamespace(episodic=FakeEpisodic())

    events = []
    async for event in session.stream_turn("hello"):
        events.append(event)
        if isinstance(event, TurnDropped):
            break

    dropped = next(event for event in events if isinstance(event, TurnDropped))
    assert dropped.stop_reason == "provider_ttfb_timeout"
    assert dropped.exhausted is True
    assert "provider=Codex Responses" in dropped.provider_error_detail
    assert "failure_type=ttfb_timeout" in dropped.provider_error_detail
```

- [ ] **Step 2: Run session test and verify it fails**

Run:

```bash
pytest -q tests/test_session.py::test_stream_turn_surfaces_responses_provider_error
```

Expected: failure because `TurnDropped` has no `provider_error_detail` and `stream_turn` does not catch `ResponsesProviderError`.

- [ ] **Step 3: Add detail field to `TurnDropped`**

In `loom/core/events.py`, change `TurnDropped` to include:

```python
provider_error_detail: str = ""
```

Keep the field defaulted after `exhausted: bool = False` so existing constructors remain valid.

- [ ] **Step 4: Catch provider errors in `stream_turn`**

In `loom/core/session.py`, import:

```python
from loom.core.cognition.responses_sse import ResponsesProviderError
```

Wrap only the existing `async for chunk, final in self.router.stream_chat(...)` block:

```python
try:
    async for chunk, final in self.router.stream_chat(
        model=_active_model,
        messages=self.messages,
        tools=tools,
        max_tokens=_resolve_output_max_tokens(
            self._loom_config, _active_model, router=self.router,
        ),
        abort_signal=abort_signal,
    ):
        ...
except ResponsesProviderError as exc:
    logger.error("stream_turn: provider error: %s", exc)
    yield TurnDropped(
        stop_reason=f"provider_{exc.failure_type}",
        retry_count=_stream_retry,
        tool_count=tool_count,
        exhausted=True,
        provider_error_detail=str(exc),
    )
    break
```

Do not catch generic `RuntimeError`; only catch `ResponsesProviderError` so unrelated exceptions still fail loudly.

- [ ] **Step 5: Render provider detail in Discord drops**

In `loom/platform/discord/bot.py`, inside the main `TurnDropped` branch, after building `drop_msg`, append:

```python
if event.provider_error_detail:
    drop_msg += f"\n-# `{event.provider_error_detail[:900]}`"
```

Do the same in the chime `TurnDropped` branch:

```python
detail = f" — {event.provider_error_detail[:900]}" if event.provider_error_detail else ""
await _safe_send(channel, f"-# ⚠️ Chime turn dropped: {event.stop_reason}{detail}")
```

- [ ] **Step 6: Run session and Discord-adjacent tests**

Run:

```bash
pytest -q tests/test_session.py::test_stream_turn_surfaces_responses_provider_error tests/test_reasoning_continuation.py
```

Expected: tests pass and reasoning continuation remains unchanged.

- [ ] **Step 7: Commit diagnostics surfacing**

Run:

```bash
git add loom/core/events.py loom/core/session.py loom/platform/discord/bot.py tests/test_session.py
git commit -m "feat(session): surface responses provider diagnostics"
```

Expected: commit succeeds.

## Task 8: Add Opt-In Live OAuth Smoke Tests

**Files:**
- Create: `tests/test_responses_oauth_live.py`

- [ ] **Step 1: Add live smoke tests skipped by default**

Create `tests/test_responses_oauth_live.py`:

```python
import os

import pytest

from loom.core.cognition.providers import CodexResponsesProvider, XAIResponsesProvider


pytestmark = pytest.mark.skipif(
    os.environ.get("LOOM_LIVE_OAUTH_SMOKE") != "1",
    reason="set LOOM_LIVE_OAUTH_SMOKE=1 to run real OAuth provider smoke tests",
)


@pytest.mark.asyncio
async def test_live_codex_oauth_small_prompt() -> None:
    provider = CodexResponsesProvider()
    response = await provider.chat(
        [{"role": "user", "content": "Reply with exactly: loom-codex-smoke"}],
        max_tokens=128,
    )

    assert response.text
    assert "loom-codex-smoke" in response.text.lower()


@pytest.mark.asyncio
async def test_live_xai_oauth_small_prompt() -> None:
    provider = XAIResponsesProvider()
    response = await provider.chat(
        [{"role": "user", "content": "Reply with exactly: loom-xai-smoke"}],
        max_tokens=128,
    )

    assert response.text
    assert "loom-xai-smoke" in response.text.lower()


@pytest.mark.asyncio
async def test_live_codex_oauth_large_context_does_not_silently_stall() -> None:
    provider = CodexResponsesProvider()
    long_context = "\n".join(f"line {idx}: implementation context" for idx in range(600))
    response = await provider.chat(
        [
            {"role": "system", "content": "Answer concisely."},
            {"role": "user", "content": f"{long_context}\n\nReturn the final word: codex-large-ok"},
        ],
        max_tokens=256,
    )

    assert response.text
    assert "codex-large-ok" in response.text.lower()


@pytest.mark.asyncio
async def test_live_xai_oauth_large_context_does_not_silently_stall() -> None:
    provider = XAIResponsesProvider()
    long_context = "\n".join(f"line {idx}: implementation context" for idx in range(600))
    response = await provider.chat(
        [
            {"role": "system", "content": "Answer concisely."},
            {"role": "user", "content": f"{long_context}\n\nReturn the final word: xai-large-ok"},
        ],
        max_tokens=256,
    )

    assert response.text
    assert "xai-large-ok" in response.text.lower()
```

- [ ] **Step 2: Verify live tests skip by default**

Run:

```bash
pytest -q tests/test_responses_oauth_live.py
```

Expected: `4 skipped`.

- [ ] **Step 3: Commit live smoke test scaffold**

Run:

```bash
git add tests/test_responses_oauth_live.py
git commit -m "test(cognition): add live responses oauth smoke tests"
```

Expected: commit succeeds.

## Task 9: Focused Verification and Live Smoke

**Files:**
- No planned edits unless tests expose a defect.

- [ ] **Step 1: Run focused unit tests**

Run:

```bash
pytest -q tests/test_responses_sse.py tests/test_codex_responses_provider.py tests/test_xai_responses_provider.py tests/test_session.py tests/test_reasoning_continuation.py
```

Expected: all selected tests pass.

- [ ] **Step 2: Compile changed Python modules**

Run:

```bash
python -m py_compile loom/core/cognition/responses_sse.py loom/core/cognition/responses_policy.py loom/core/cognition/codex_responses_provider.py loom/core/cognition/xai_responses_provider.py loom/core/cognition/providers.py loom/core/session.py loom/core/events.py loom/platform/discord/bot.py
```

Expected: no output and exit code 0.

- [ ] **Step 3: Run live OAuth smoke tests**

Run:

```bash
LOOM_LIVE_OAUTH_SMOKE=1 pytest -q tests/test_responses_oauth_live.py -s
```

Expected: Codex and xAI either return the requested marker text, or fail with a visible provider diagnostic such as `credential_missing`, `credential_expired`, `entitlement_or_quota`, `http_error`, or `ttfb_timeout`. A hang or empty turn is a failing result.

- [ ] **Step 4: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 5: Run GitNexus change detection**

Run:

```bash
npx gitnexus detect-changes --scope staged
```

Expected: affected symbols are limited to Responses OAuth providers, Responses SSE/policy helpers, `TurnDropped`, and `LoomSession.stream_turn`. If Anthropic/MiniMax provider flows are listed, inspect before committing.

## Task 10: Doc Drift Awareness and Follow-Up Issue

**Files:**
- No runtime edits.

- [ ] **Step 1: Grep docs for changed surface names**

Run:

```bash
grep -rln "CodexResponsesProvider\\|XAIResponsesProvider\\|ResponsesProviderError\\|TurnDropped\\|providers.py\\|xai_oauth\\|codex/" doc/
```

Expected: list any matching docs as "these docs may need updating" in the PR notes. Do not update docs in this PR unless the hit quotes a changed signature or changed user-visible behavior.

- [ ] **Step 2: Open follow-up issue for Anthropic/MiniMax extraction**

Run:

```bash
gh issue create --repo Dakai666/Loom --title "Follow-up: extract Anthropic and MiniMax provider runtime contract" --body $'After Codex/xAI Responses OAuth runtime stabilizes, extract AnthropicProvider and MiniMax usage into a cleaner provider-specific contract.\n\nScope notes:\n- MiniMax/Anthropic is stable and intentionally out of scope for the Codex/xAI cleanup PR.\n- Preserve current AsyncAnthropic SDK behavior.\n- Do not mix this with Responses SSE diagnostics.\n- Start from the same import-compatibility pattern used for Codex/xAI providers.'
```

Expected: issue URL is printed.

- [ ] **Step 3: Update draft PR body with verification**

Run:

```bash
gh pr edit --repo Dakai666/Loom --body $'## Summary\n- split Codex and xAI Responses OAuth runtime into provider-specific modules\n- add neutral Responses SSE parser, phase diagnostics, first-event watchdog, and structured provider errors\n- surface provider failures through `TurnDropped` without changing `LLMResponse`\n- keep Anthropic/MiniMax and OpenAI-compatible providers in `providers.py`\n\n## Verification\n- `pytest -q tests/test_responses_sse.py tests/test_codex_responses_provider.py tests/test_xai_responses_provider.py tests/test_session.py tests/test_reasoning_continuation.py`\n- `python -m py_compile loom/core/cognition/responses_sse.py loom/core/cognition/responses_policy.py loom/core/cognition/codex_responses_provider.py loom/core/cognition/xai_responses_provider.py loom/core/cognition/providers.py loom/core/session.py loom/core/events.py loom/platform/discord/bot.py`\n- `LOOM_LIVE_OAUTH_SMOKE=1 pytest -q tests/test_responses_oauth_live.py -s`\n- `git diff --check`\n- `npx gitnexus detect-changes --scope staged`\n\n## Docs\n- Doc drift grep run for `CodexResponsesProvider`, `XAIResponsesProvider`, `ResponsesProviderError`, `TurnDropped`, `providers.py`, `xai_oauth`, and `codex/`.'
```

Expected: PR body updated.

## Final Verification Before Marking Ready

- [ ] Run the focused verification commands from Task 9 again after the last commit.
- [ ] Run `npx gitnexus detect-changes --scope staged` before the final commit if any files changed after Task 9.
- [ ] Confirm `git status --short` is clean after all commits.
- [ ] Convert draft PR to ready only after live smoke results are included in the PR body.

Run:

```bash
gh pr ready --repo Dakai666/Loom
```

Expected: PR is ready for review.
