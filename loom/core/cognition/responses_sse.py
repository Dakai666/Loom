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
            context = " ".join(part for part in (event_type, f"status={status}" if status else "") if part)
            return ResponsesProviderError.from_state(
                state,
                failure_type="stream_incomplete",
                detail=f"{state.provider} response error ({context}): {_error_detail(data, event_type, status)}",
            )

    failed_event = (
        event_type in {"error", "response.failed"}
        or event_type.endswith(".failed")
        or status in {"failed", "cancelled"}
        or error is not None
    )
    if not failed_event:
        return None

    context = " ".join(part for part in (event_type, f"status={status}" if status else "") if part)
    detail = f"{state.provider} response error"
    if context:
        detail += f" ({context})"
    extra = _error_detail(data, event_type, status)
    if extra:
        detail += f": {extra}"
    return ResponsesProviderError.from_state(
        state,
        failure_type="stream_error_event",
        detail=detail,
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
