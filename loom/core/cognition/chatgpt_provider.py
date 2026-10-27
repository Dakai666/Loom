"""ChatGPT plan usage through the documented public Responses API.

The legacy codex/ provider stays separate. Never substitute an API key,
another account, or Codex backend when plan admission or quota fails.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .chatgpt_auth import ChatGPTAuth, ChatGPTAuthError, RESOURCE
from .codex_responses_provider import CodexResponsesProvider, _build_responses_content
from .providers import LLMResponse, ToolUse, _reduce_tool_result_to_text
from .responses_policy import RESPONSES_REASONING_DEFAULT_EFFORT
from .responses_sse import ResponsesProviderError, ResponsesStreamState, iter_sse_events


logger = logging.getLogger(__name__)


def _content(content: Any, role: str) -> list[dict[str, Any]]:
    # A vanished local image must not take down an otherwise readable turn.
    if not isinstance(content, list):
        return _build_responses_content(content, role)
    blocks = []
    for block in content:
        try:
            blocks.extend(_build_responses_content([block], role))
        except OSError:
            blocks.append({"type": "input_text" if role == "user" else "output_text",
                           "text": "[Image unavailable: local file cannot be read.]"})
    return blocks


class ChatGPTResponsesProvider(CodexResponsesProvider):
    name = "chatgpt"
    ROUTING_PREFIX = "chatgpt/"
    DEFAULT_MODEL = "gpt-6.1-sol"
    DEFAULT_BASE_URL = f"{RESOURCE}/responses"
    TOOL_NAMESPACE = "loom"
    # Explicit route contract, independent of the legacy Codex backend.
    SUPPORTS_MAX_OUTPUT_TOKENS = False

    def __init__(self, model: str = "", *, auth: ChatGPTAuth | None = None,
                 account: str | None = None, reasoning_effort: str | None = None,
                 timeout: float = 600, first_event_timeout: float | None = None,
                 idle_event_timeout: float | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(model=model or f"chatgpt/{self.DEFAULT_MODEL}",
                         base_url=self.DEFAULT_BASE_URL, timeout=timeout,
                         reasoning_effort=reasoning_effort,
                         first_event_timeout=first_event_timeout,
                         idle_event_timeout=idle_event_timeout)
        self.auth = auth or ChatGPTAuth()
        self.account = account
        self.transport = transport
        self._warned_context_models: set[str] = set()
        self._explicit_effort = reasoning_effort
        if reasoning_effort is not None:
            if reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
                raise ValueError("Invalid ChatGPT reasoning_effort.")
            self._reasoning_effort = reasoning_effort

    def _catalog_entry(self, model: str) -> dict[str, Any]:
        try:
            models = self.auth.catalog(self.account)
        except ChatGPTAuthError:
            models = []
        slug = model.removeprefix(self.ROUTING_PREFIX)
        return next((entry for entry in models if entry.get("slug") == slug), {})

    def native_context_window(self, model: str) -> int | None:
        value = self._catalog_entry(model).get("context_window")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        if model not in self._warned_context_models:
            logger.warning(
                "ChatGPT model %s has no cached context_window; using 32768 tokens. "
                "Refresh with `loom auth chatgpt models` or configure a context window override.", model)
            self._warned_context_models.add(model)
        return 32768

    def _model_reasoning_effort(self) -> str | None:
        entry = self._catalog_entry(self._api_model())
        levels = entry.get("supported_reasoning_levels")
        supported = [level.get("effort") if isinstance(level, dict) else level
                     for level in levels] if isinstance(levels, list) else None
        if self._explicit_effort is not None:
            if supported is not None and self._explicit_effort not in supported:
                raise ValueError(f"ChatGPT model {self._api_model()} does not support reasoning_effort="
                                 f"{self._explicit_effort}.")
            return self._explicit_effort
        if supported == []:
            return None  # Only omit when the catalog explicitly reports no levels.
        default = entry.get("default_reasoning_level")
        if isinstance(default, str) and default and (supported is None or default in supported):
            return default
        if supported:
            return (RESPONSES_REASONING_DEFAULT_EFFORT if RESPONSES_REASONING_DEFAULT_EFFORT in supported
                    else supported[0])
        # Missing metadata must not silently delegate effort to the backend.
        return RESPONSES_REASONING_DEFAULT_EFFORT

    def format_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        functions = super().format_tools(tools)
        return [{"type": "namespace", "name": self.TOOL_NAMESPACE,
                 "description": "Tools executed by Loom on the user's behalf.", "tools": functions}]

    def _build_payload(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                       max_tokens: int) -> dict[str, Any]:
        instructions, items = [], []
        for msg in messages:
            role, content = msg.get("role"), msg.get("content", "")
            if role in ("system", "developer"):
                if content:
                    instructions.append(str(content))
            elif role == "tool":
                items.append({"type": "function_call_output", "call_id": msg["tool_call_id"],
                              "output": _reduce_tool_result_to_text(content)})
            else:
                if content:
                    items.append({"role": role, "content": _content(content, role)})
                if role == "assistant":
                    for call in msg.get("tool_calls", []) or []:
                        function = call["function"]
                        items.append({"type": "function_call", "call_id": call["id"],
                                      "namespace": self.TOOL_NAMESPACE, "name": function["name"],
                                      "arguments": function["arguments"]})
        payload = {"model": self._api_model(), "instructions": "\n\n".join(instructions),
                   "input": items, "store": False, "stream": True}
        effort = self._model_reasoning_effort()
        if effort is not None:
            payload["reasoning"] = {"effort": effort}
        if tools:
            payload["tools"] = self.format_tools(tools)
        return payload

    async def stream_chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                          max_tokens: int = 8096, *, abort_signal: Any = None
                          ) -> AsyncIterator[tuple[str, LLMResponse | None]]:
        state = ResponsesStreamState(provider="ChatGPT", model=self._api_model())
        try:
            token = await asyncio.to_thread(self.auth.access_token, self.account)
        except ChatGPTAuthError as exc:
            raise ResponsesProviderError.from_state(
                state, failure_type="credential_missing", detail=str(exc)) from exc
        payload = self._build_payload(messages, tools, max_tokens)
        text, calls, usage = "", {}, {}
        completed = False
        async with httpx.AsyncClient(timeout=self._timeout, transport=self.transport,
                                     follow_redirects=False) as client:
            state.mark_phase("request_sent")
            try:
                async with client.stream("POST", self.DEFAULT_BASE_URL,
                                         headers={"Authorization": f"Bearer {token}",
                                                  "Accept": "text/event-stream"}, json=payload) as response:
                    state.mark_phase("headers_received")
                    if response.status_code != 200:
                        body = (await response.aread()).decode(errors="replace").replace(token, "[redacted]")
                        # Keep admission's actual response shape, status, and request ID.
                        request_id = response.headers.get("x-request-id", "unknown")
                        raise ResponsesProviderError.from_state(
                            state, failure_type="http_error",
                            detail=f"HTTP {response.status_code}; request_id={request_id}; {body[:4000]}")
                    async for event, data in iter_sse_events(
                        response.aiter_lines(), state=state,
                        first_event_timeout=self._first_event_timeout,
                        idle_event_timeout=self._idle_event_timeout,
                    ):
                        if abort_signal is not None and abort_signal.is_set():
                            raise asyncio.CancelledError()
                        if event == "response.output_text.delta":
                            delta = data.get("delta", "")
                            text += delta
                            yield delta, None
                        if event == "response.output_item.done":
                            item = data.get("item", {})
                            if item.get("type") == "function_call":
                                calls[item["call_id"]] = item
                        if event in ("response.completed", "response.incomplete"):
                            result = data.get("response", {})
                            if event == "response.incomplete" or result.get("status") not in (None, "completed"):
                                raise ResponsesProviderError.from_state(
                                    state, failure_type="stream_incomplete", detail="ChatGPT response was incomplete.")
                            completed = True
                            usage = result.get("usage") or {}
                            for item in result.get("output", []):
                                if item.get("type") == "function_call":
                                    calls[item["call_id"]] = item
                            break
            except httpx.HTTPError as exc:
                raise ResponsesProviderError.from_state(
                    state, failure_type="provider_protocol_error", detail=type(exc).__name__) from exc
        if not completed:
            raise ResponsesProviderError.from_state(
                state, failure_type="stream_incomplete", detail="Stream ended without response.completed.")
        tool_uses, raw_calls = [], []
        for call_id, call in calls.items():
            namespace = call.get("namespace")
            if namespace not in (None, self.TOOL_NAMESPACE):
                raise ResponsesProviderError.from_state(
                    state, failure_type="provider_protocol_error", detail="Unknown tool namespace.")
            name = call["name"]
            # Some servers return qualified names instead of a namespace field.
            name = name.removeprefix(self.TOOL_NAMESPACE + ".")
            try:
                args = json.loads(call.get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError("tool arguments must be an object")
            except (ValueError, TypeError) as exc:
                raise ResponsesProviderError.from_state(
                    state, failure_type="provider_protocol_error", detail="Invalid function arguments.") from exc
            tool_uses.append(ToolUse(id=call_id, name=name, args=args))
            raw_calls.append({"id": call_id, "type": "function", "function": {
                "name": name, "arguments": call.get("arguments") or "{}"}})
        raw = {"role": "assistant", "content": text}
        if raw_calls:
            raw["tool_calls"] = raw_calls
        yield "", LLMResponse(
            text=text or None, tool_uses=tool_uses,
            stop_reason="tool_use" if tool_uses else "end_turn",
            input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0),
            cache_read_input_tokens=(usage.get("input_tokens_details") or {}).get("cached_tokens", 0),
            raw_message=raw,
        )
