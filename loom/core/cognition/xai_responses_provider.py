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


class XAIResponsesProvider(LLMProvider):
    """
    xAI Grok OAuth chat via xAI's Responses API.

    Routing prefix: ``xai/``. This provider intentionally has no
    ``XAI_API_KEY`` fallback; selecting ``xai/...`` means OAuth only.
    """

    name = "xai"
    ROUTING_PREFIX = "xai/"
    DEFAULT_MODEL = "grok-4.3"
    DEFAULT_BASE_URL = "https://api.x.ai/v1/responses"
    DEFAULT_TIMEOUT = 600.0
    SUPPORTS_MAX_OUTPUT_TOKENS = True

    def __init__(
        self,
        model: str = "",
        base_url: str = "",
        timeout: float = 0.0,
        reasoning_effort: str | None = None,
    ) -> None:
        self.model = model or (self.ROUTING_PREFIX + self.DEFAULT_MODEL)
        self._base_url = base_url or self.DEFAULT_BASE_URL
        self._timeout = timeout or self.DEFAULT_TIMEOUT
        self._reasoning_effort = normalize_reasoning_effort(reasoning_effort)
        self._first_event_timeout = DEFAULT_FIRST_EVENT_TIMEOUT
        self._idle_event_timeout = DEFAULT_IDLE_EVENT_TIMEOUT

    def _api_model(self) -> str:
        return self.model.removeprefix(self.ROUTING_PREFIX)

    def _provider_state(self) -> ResponsesStreamState:
        return ResponsesStreamState(provider="xAI Responses", model=self._api_model())

    def _load_bearer_token(self) -> str:
        from loom.core.cognition.xai_auth import load_xai_oauth_credential

        credential = load_xai_oauth_credential()
        if credential is None:
            raise RuntimeError(
                "No unexpired xAI OAuth token found. Run `loom auth xai` "
                "before using an OAuth model such as `xai/grok-4.3`."
            )
        return credential.token

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
    ) -> dict[str, Any]:
        instructions: list[str] = []
        input_items: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "system":
                if content:
                    instructions.append(str(content))
                continue
            if role == "tool":
                call_id = msg.get("tool_call_id")
                if call_id:
                    input_items.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": str(content),
                    })
                continue
            if role == "assistant":
                if content:
                    input_items.append({
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": str(content)}],
                    })
                for tc in msg.get("tool_calls", []) or []:
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    input_items.append({
                        "type": "function_call",
                        "call_id": tc.get("id") or str(uuid.uuid4()),
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "{}"),
                    })
                continue
            input_items.append({
                "role": "user" if role != "assistant" else "assistant",
                "content": [{"type": "input_text", "text": str(content)}],
            })

        payload: dict[str, Any] = {
            "model": self._api_model(),
            "instructions": "\n\n".join(instructions) or "You are a helpful assistant.",
            "input": input_items,
            "stream": True,
            "store": False,
        }
        if self.SUPPORTS_MAX_OUTPUT_TOKENS:
            payload["max_output_tokens"] = max_tokens
        payload["reasoning"] = {"effort": self._reasoning_effort}
        if tools:
            payload["tools"] = self.format_tools(tools)
        return payload

    def format_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        formatted: list[dict[str, Any]] = []
        for tool in tools:
            formatted.append({
                "type": "function",
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters") or tool.get("input_schema") or {},
            })
        return formatted

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 8096,
    ) -> LLMResponse:
        text_parts: list[str] = []
        final: LLMResponse | None = None
        async for chunk, resp in self.stream_chat(
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
        ):
            if resp is not None:
                final = resp
            elif chunk:
                text_parts.append(chunk)
        return final or LLMResponse(
            text="".join(text_parts) or None,
            tool_uses=[],
            stop_reason="end_turn",
        )

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 8096,
        *,
        abort_signal: Any = None,
    ) -> AsyncIterator[tuple[str, LLMResponse | None]]:
        import httpx

        state = self._provider_state()
        try:
            bearer = self._load_bearer_token()
        except RuntimeError as exc:
            raise ResponsesProviderError.from_state(
                state,
                failure_type="credential_missing",
                detail=str(exc),
            ) from exc
        payload = self._build_payload(messages, tools, max_tokens)
        full_content = ""
        output_items: list[dict[str, Any]] = []
        input_tokens = 0
        output_tokens = 0
        response_status = "in_progress"
        reasoning_wrapper = _ReasoningStreamWrapper()

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            state.mark_phase("request_sent")
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
                    detail = await http_status_detail("xAI Responses", exc)
                    raise ResponsesProviderError.from_state(
                        state,
                        failure_type=classify_xai_http_failure(detail),
                        detail=detail,
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
                                input_tokens = int(
                                    usage.get("input_tokens") or usage.get("prompt_tokens") or input_tokens
                                )
                                output_tokens = int(
                                    usage.get("output_tokens") or usage.get("completion_tokens") or output_tokens
                                )
                except httpx.HTTPError as exc:
                    raise ResponsesProviderError.from_state(
                        state,
                        failure_type="provider_protocol_error",
                        detail=stream_interrupt_detail("xAI Responses", exc),
                    ) from exc

        closing = reasoning_wrapper.close()
        if closing:
            yield (closing, None)

        tool_uses: list[ToolUse] = []
        raw_tool_calls: list[dict[str, Any]] = []
        for item in output_items:
            if item.get("type") != "function_call":
                continue
            args_raw = item.get("arguments") or "{}"
            try:
                args = json.loads(args_raw)
            except (json.JSONDecodeError, ValueError):
                args = {"_raw": args_raw}
            call_id = item.get("call_id") or item.get("id") or str(uuid.uuid4())
            name = item.get("name") or ""
            tool_uses.append(ToolUse(id=call_id, name=name, args=args))
            raw_tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": args_raw},
            })

        stop_reason = "tool_use" if tool_uses else "end_turn"
        if response_status == "incomplete":
            stop_reason = "max_tokens"

        raw_message: dict[str, Any] = {"role": "assistant", "content": full_content}
        if raw_tool_calls:
            raw_message["tool_calls"] = raw_tool_calls
        yield ("", LLMResponse(
            text=full_content or None,
            tool_uses=tool_uses,
            stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            raw_message=raw_message,
        ))

    def format_tool_result(
        self, tool_use_id: str, content: str, success: bool = True
    ) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": tool_use_id,
            "content": content if success else f"Error: {content}",
        }
