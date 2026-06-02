from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from .providers import LLMProvider, LLMResponse, ToolUse
from . import vision as _vision
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



# ---------------------------------------------------------------------------
# Vision: convert canonical list content to Responses-API blocks
# ---------------------------------------------------------------------------

def _build_responses_content(
    content: Any, role: str
) -> list[dict[str, Any]]:
    """
    Convert canonical user/assistant ``content`` to Responses API blocks.

    * ``str`` → ``[input_text]`` or ``[output_text]`` depending on role
    * list of blocks → mixed ``input_text`` / ``input_image`` blocks

    Image blocks use the canonical ``{"type": "image", "source": {...}}``
    shape defined in :mod:`loom.core.cognition.vision`. We delegate the
    actual MIME/size/digest validation to the shared module and just
    translate the wire shape here.
    """
    text_type = "output_text" if role == "assistant" else "input_text"
    if isinstance(content, str):
        return [{"type": text_type, "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": text_type, "text": str(content)}]
    blocks: list[dict[str, Any]] = []
    for block in content:
        btype = block.get("type")
        if btype == "text":
            txt = block.get("text", "")
            if txt:
                blocks.append({"type": text_type, "text": txt})
            continue
        if btype == "image":
            src = block.get("source", {}) or {}
            if src.get("kind") == "url":
                blocks.append({
                    "type": "input_image",
                    "image_url": src.get("ref"),
                })
                continue
            if src.get("kind") == "raw":
                raw_bytes = src.get("ref")
                vi = _vision.VisionInput(
                    source=_vision.VisionSource(kind="raw", ref=raw_bytes),
                    media_type=src.get("media_type", ""),
                    size_bytes=len(raw_bytes) if raw_bytes else 0,
                    digest=src.get("digest", ""),
                )
            else:
                vi = _vision.load_vision_input(src.get("ref"))
            blocks.append(_vision.to_responses_image_block(vi))
            continue
        blocks.append(block)
    return blocks



class CodexResponsesProvider(LLMProvider):
    """
    OpenAI Codex OAuth chat via ChatGPT's Codex Responses backend.

    Routing prefix: ``codex/``. This provider intentionally does not fall back
    to ``OPENAI_API_KEY`` so users do not accidentally burn API quota when they
    selected Codex OAuth mode.
    """

    name = "codex"
    ROUTING_PREFIX = "codex/"
    DEFAULT_MODEL = "gpt-5.5"
    DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex/responses"
    DEFAULT_TIMEOUT = 600.0
    SUPPORTS_MAX_OUTPUT_TOKENS = False

    def __init__(
        self,
        model: str = "",
        base_url: str = "",
        timeout: float = 0.0,
        reasoning_effort: str | None = None,
        first_event_timeout: float | None = None,
        idle_event_timeout: float | None = None,
    ) -> None:
        self.model = model or (self.ROUTING_PREFIX + self.DEFAULT_MODEL)
        self._base_url = base_url or self.DEFAULT_BASE_URL
        self._timeout = timeout or self.DEFAULT_TIMEOUT
        self._reasoning_effort = normalize_reasoning_effort(reasoning_effort)
        self._first_event_timeout = (
            DEFAULT_FIRST_EVENT_TIMEOUT
            if first_event_timeout is None or first_event_timeout < 0
            else first_event_timeout
        )
        self._idle_event_timeout = (
            DEFAULT_IDLE_EVENT_TIMEOUT
            if idle_event_timeout is None or idle_event_timeout < 0
            else idle_event_timeout
        )

    def _api_model(self) -> str:
        return self.model.removeprefix(self.ROUTING_PREFIX)

    def _provider_state(self) -> ResponsesStreamState:
        return ResponsesStreamState(provider="Codex Responses", model=self._api_model())

    def _load_bearer_token(self) -> str:
        from loom.core.cognition.openai_auth import load_codex_oauth_credential

        credential = load_codex_oauth_credential()
        if credential is None:
            raise RuntimeError(
                "No unexpired Codex OAuth token found. Run `codex login` "
                "or switch to an API-key OpenAI model such as `gpt-5.5`."
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
                    # A vision tool result carries canonical list content
                    # ([text, image]). The Responses function_call_output
                    # can't hold an image, so reduce to the text parts
                    # rather than emitting a Python repr of the blocks.
                    if isinstance(content, list):
                        out = "\n".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    else:
                        out = str(content)
                    input_items.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": out,
                    })
                continue
            if role == "assistant":
                if content:
                    input_items.append({
                        "role": "assistant",
                        "content": _build_responses_content(content, "assistant"),
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
                "content": _build_responses_content(content, role),
            })

        payload: dict[str, Any] = {
            "model": self._api_model(),
            "instructions": "\n\n".join(instructions) or "You are a helpful assistant.",
            "input": input_items,
            "stream": True,
            "store": False,
            "reasoning": {"effort": self._reasoning_effort},
        }
        if self.SUPPORTS_MAX_OUTPUT_TOKENS:
            payload["max_output_tokens"] = max_tokens
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
                        detail=stream_interrupt_detail("Codex Responses", exc),
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
