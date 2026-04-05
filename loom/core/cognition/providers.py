"""
LLM Provider abstractions.

All providers expose a single async `chat()` method that returns a
normalized `LLMResponse`.  The rest of the framework never imports
a provider SDK directly — it always goes through this interface.

Supported providers
-------------------
MiniMaxProvider   — minimax.io (OpenAI-compatible, model MiniMax-M2.7)
AnthropicProvider — api.anthropic.com

Internal message format (OpenAI-style, used as canonical across the framework)
----
User:      {"role": "user", "content": "..."}
Assistant: {"role": "assistant", "content": "...", "tool_calls": [...]}  # tool_calls optional
Tool:      {"role": "tool", "tool_call_id": "...", "content": "..."}
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


async def _retry_async(
    coro_fn,
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """
    Call ``coro_fn()`` up to ``max_retries`` times with exponential backoff.

    Retryable conditions:
    - HTTP status in {429, 500, 502, 503, 504}
    - asyncio.TimeoutError
    - Any exception whose class name contains "timeout" or "connection"
    """
    last_exc: BaseException = RuntimeError("no attempts made")
    for attempt in range(max_retries):
        try:
            return await coro_fn()
        except BaseException as exc:
            last_exc = exc
            status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            exc_name = type(exc).__name__.lower()
            retryable = (
                isinstance(exc, asyncio.TimeoutError)
                or (isinstance(status, int) and status in _RETRYABLE_STATUSES)
                or "timeout" in exc_name
                or "connection" in exc_name
            )
            if not retryable or attempt == max_retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc  # unreachable


# ---------------------------------------------------------------------------
# Normalized data types
# ---------------------------------------------------------------------------

@dataclass
class ToolUse:
    """A tool call requested by the model, normalized across providers."""
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class LLMResponse:
    """
    Normalized response from any LLM provider.

    `raw_message` is the provider-specific assistant message dict that
    must be appended back into the message history for multi-turn correctness.
    """
    text: str | None
    tool_uses: list[ToolUse]
    stop_reason: str          # "end_turn" | "tool_use" | "max_tokens"
    input_tokens: int = 0
    output_tokens: int = 0
    raw_message: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Provider base
# ---------------------------------------------------------------------------

class LLMProvider(ABC):
    """Abstract base for all LLM providers."""
    name: str

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 8096,
    ) -> LLMResponse:
        """Send a chat request and return a normalized response."""
        ...

    @abstractmethod
    def format_tool_result(
        self,
        tool_use_id: str,
        content: str,
        success: bool = True,
    ) -> dict[str, Any]:
        """Build the provider-correct tool-result message to append to history."""
        ...

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 8096,
        *,
        abort_signal: Any = None,
    ) -> AsyncIterator[tuple[str, LLMResponse | None]]:
        """
        Stream a chat response.

        Yields ``(chunk, None)`` for each text fragment and
        ``("", LLMResponse)`` once as the final item.

        Parameters
        ----------
        abort_signal:
            An ``asyncio.Event`` from an ``AbortController``. When the event
            is set (abort requested), the HTTP request is cancelled and the
            stream exits cleanly.  Pass ``None`` for no abort support.
        """
        response = await self.chat(messages=messages, tools=tools, max_tokens=max_tokens)
        if response.text:
            yield (response.text, None)
        yield ("", response)

    def format_tools(
        self, tools: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Convert Loom-internal tool definitions to provider wire format.
        Default: OpenAI format.  Anthropic overrides this.
        """
        return [
            {"type": "function", "function": t}
            for t in tools
        ]


# ---------------------------------------------------------------------------
# MiniMax provider  (OpenAI-compatible endpoint)
# ---------------------------------------------------------------------------

_XML_TOOL_CALL_RE = re.compile(
    r'<minimax:tool_call>.*?<invoke\s+name="([^"]+)">(.*?)</invoke>.*?</minimax:tool_call>',
    re.DOTALL,
)
_XML_PARAM_RE = re.compile(
    r'<parameter\s+name="([^"]+)">(.*?)</parameter>',
    re.DOTALL,
)


def _parse_xml_tool_calls(content: str) -> list[ToolUse]:
    """
    Fallback parser for MiniMax XML-format tool calls.

    MiniMax M2.x models may emit raw XML instead of OpenAI-style
    tool_calls when the reasoning chain overflows into the content field.
    This parser handles that gracefully.
    """
    uses = []
    for m in _XML_TOOL_CALL_RE.finditer(content):
        name = m.group(1)
        args: dict[str, Any] = {}
        for pm in _XML_PARAM_RE.finditer(m.group(2)):
            raw_val = pm.group(2).strip()
            try:
                args[pm.group(1)] = json.loads(raw_val)
            except (json.JSONDecodeError, ValueError):
                args[pm.group(1)] = raw_val
        uses.append(ToolUse(id=str(uuid.uuid4()), name=name, args=args))
    return uses


class MiniMaxProvider(LLMProvider):
    """
    MiniMax international API (minimax.io) via the OpenAI-compatible endpoint.

    Tool calling: tries standard OpenAI tool_calls first; falls back to
    XML parsing if the model emits XML in the content field.
    """

    name = "minimax"
    BASE_URL = "https://api.minimax.io/v1"

    def __init__(
        self,
        api_key: str,
        model: str = "MiniMax-M2.7",
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        from openai import OpenAI, AsyncOpenAI
        self._api_key = api_key
        self._client = OpenAI(api_key=api_key, base_url=self.BASE_URL, timeout=timeout)
        self._async_client = AsyncOpenAI(
            api_key=api_key, base_url=self.BASE_URL, timeout=timeout,
        )
        self.model = model
        self._timeout = timeout
        self._max_retries = max_retries

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 8096,
    ) -> LLMResponse:
        loop = asyncio.get_event_loop()

        async def _call() -> LLMResponse:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    None, self._sync_chat, messages, tools, max_tokens,
                ),
                timeout=self._timeout,
            )

        return await _retry_async(_call, max_retries=self._max_retries)

    def _sync_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        max_tokens: int,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = self.format_tools(tools)

        response = self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = choice.message
        content_text: str = msg.content or ""

        tool_uses: list[ToolUse] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, ValueError):
                    args = {"_raw": tc.function.arguments}
                tool_uses.append(ToolUse(
                    id=tc.id,
                    name=tc.function.name,
                    args=args,
                ))
        elif content_text and "<minimax:tool_call>" in content_text:
            tool_uses = _parse_xml_tool_calls(content_text)

        finish = choice.finish_reason or "stop"
        if finish in ("tool_calls", "function_call"):
            stop_reason = "tool_use"
        elif finish == "length":
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"
        if tool_uses and stop_reason != "tool_use":
            stop_reason = "tool_use"

        raw_message: dict[str, Any] = {"role": "assistant", "content": content_text}
        if msg.tool_calls:
            raw_message["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]

        usage = response.usage
        return LLMResponse(
            text=content_text or None,
            tool_uses=tool_uses,
            stop_reason=stop_reason,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            raw_message=raw_message,
        )

    async def stream_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 8096,
        *,
        abort_signal: Any = None,
    ) -> AsyncIterator[tuple[str, LLMResponse | None]]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = self.format_tools(tools)

        full_content = ""
        tc_accum: dict[int, dict] = {}
        finish_reason: str | None = None
        input_tokens = 0
        output_tokens = 0

        stream = await self._async_client.chat.completions.create(**kwargs)
        try:
            async for chunk in stream:
                if abort_signal is not None and abort_signal.is_set():
                    break
                if not chunk.choices:
                    if chunk.usage:
                        input_tokens = chunk.usage.prompt_tokens or 0
                        output_tokens = chunk.usage.completion_tokens or 0
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if delta.content:
                    full_content += delta.content
                    yield (delta.content, None)
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tc_accum:
                            tc_accum[idx] = {"id": "", "name": "", "arguments": ""}
                        if tc_delta.id:
                            tc_accum[idx]["id"] = tc_delta.id
                        if tc_delta.function and tc_delta.function.name:
                            tc_accum[idx]["name"] = tc_delta.function.name
                        if tc_delta.function and tc_delta.function.arguments:
                            tc_accum[idx]["arguments"] += tc_delta.function.arguments
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
        finally:
            await stream.close()

        tool_uses: list[ToolUse] = []
        for idx in sorted(tc_accum):
            tc = tc_accum[idx]
            try:
                args = json.loads(tc["arguments"])
            except (json.JSONDecodeError, ValueError):
                args = {"_raw": tc["arguments"]}
            tool_uses.append(ToolUse(
                id=tc["id"] or str(uuid.uuid4()),
                name=tc["name"],
                args=args,
            ))

        if not tool_uses and full_content and "<minimax:tool_call>" in full_content:
            tool_uses = _parse_xml_tool_calls(full_content)

        if finish_reason in ("tool_calls", "function_call"):
            stop_reason = "tool_use"
        elif finish_reason == "length":
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"
        if tool_uses and stop_reason != "tool_use":
            stop_reason = "tool_use"

        raw_message: dict[str, Any] = {"role": "assistant", "content": full_content}
        if tc_accum:
            tool_calls_out = []
            for i in sorted(tc_accum):
                raw_args = tc_accum[i]["arguments"]
                try:
                    json.loads(raw_args)
                    valid_args = raw_args
                except (json.JSONDecodeError, ValueError):
                    parsed_args = {}
                    for tu in tool_uses:
                        if tu.id == tc_accum[i]["id"] or tu.name == tc_accum[i]["name"]:
                            parsed_args = tu.args
                            break
                    valid_args = json.dumps(parsed_args, ensure_ascii=False)
                tool_calls_out.append({
                    "id": tc_accum[i]["id"],
                    "type": "function",
                    "function": {
                        "name": tc_accum[i]["name"],
                        "arguments": valid_args,
                    },
                })
            raw_message["tool_calls"] = tool_calls_out

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


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------

class AnthropicProvider(LLMProvider):
    """
    Anthropic Claude via the anthropic SDK.

    Messages are converted to/from Anthropic format internally.
    The canonical external format is always OpenAI-style.
    """

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-6",
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        import anthropic as _anthropic
        self._api_key = api_key
        self._client = _anthropic.Anthropic(api_key=api_key, timeout=timeout)
        self._async_client = _anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout)
        self.model = model
        self._timeout = timeout
        self._max_retries = max_retries

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 8096,
    ) -> LLMResponse:
        loop = asyncio.get_event_loop()

        async def _call() -> LLMResponse:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    None, self._sync_chat, messages, tools, max_tokens,
                ),
                timeout=self._timeout,
            )

        return await _retry_async(_call, max_retries=self._max_retries)

    def _sync_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        max_tokens: int,
    ) -> LLMResponse:
        anthropic_msgs = _to_anthropic_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": anthropic_msgs,
        }
        if tools:
            kwargs["tools"] = [
                {"name": t["name"], "description": t.get("description", ""),
                 "input_schema": t.get("parameters", t.get("input_schema", {}))}
                for t in tools
            ]

        response = self._client.messages.create(**kwargs)

        text: str | None = None
        tool_uses: list[ToolUse] = []

        for block in response.content:
            if hasattr(block, "text"):
                text = block.text
            elif block.type == "tool_use":
                tool_uses.append(ToolUse(
                    id=block.id, name=block.name, args=dict(block.input)
                ))

        stop_reason_map = {
            "end_turn": "end_turn",
            "tool_use": "tool_use",
            "max_tokens": "max_tokens",
        }
        stop_reason = stop_reason_map.get(response.stop_reason, "end_turn")

        raw_message: dict[str, Any] = {"role": "assistant", "content": text or ""}
        if tool_uses:
            raw_message["tool_calls"] = [
                {
                    "id": tu.id,
                    "type": "function",
                    "function": {
                        "name": tu.name,
                        "arguments": json.dumps(tu.args),
                    },
                }
                for tu in tool_uses
            ]

        return LLMResponse(
            text=text,
            tool_uses=tool_uses,
            stop_reason=stop_reason,
            input_tokens=response.usage.input_tokens if response.usage else 0,
            output_tokens=response.usage.output_tokens if response.usage else 0,
            raw_message=raw_message,
        )

    async def stream_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 8096,
        *,
        abort_signal: Any = None,
    ) -> AsyncIterator[tuple[str, LLMResponse | None]]:
        anthropic_msgs = _to_anthropic_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": anthropic_msgs,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters", t.get("input_schema", {})),
                }
                for t in tools
            ]

        full_text = ""
        tool_uses: list[ToolUse] = []
        input_tokens = 0
        output_tokens = 0
        stop_reason = "end_turn"

        aborted = False
        async with self._async_client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                if abort_signal is not None and abort_signal.is_set():
                    aborted = True
                    break
                full_text += text
                yield (text, None)
            final = None if aborted else await stream.get_final_message()

        if final is not None:
            for block in final.content:
                if block.type == "tool_use":
                    tool_uses.append(ToolUse(
                        id=block.id,
                        name=block.name,
                        args=dict(block.input),
                    ))
            stop_reason_map = {
                "end_turn": "end_turn",
                "tool_use": "tool_use",
                "max_tokens": "max_tokens",
            }
            stop_reason = stop_reason_map.get(final.stop_reason, "end_turn")
            if final.usage:
                input_tokens = final.usage.input_tokens
                output_tokens = final.usage.output_tokens

        raw_message: dict[str, Any] = {"role": "assistant", "content": full_text or ""}
        if tool_uses:
            raw_message["tool_calls"] = [
                {
                    "id": tu.id,
                    "type": "function",
                    "function": {
                        "name": tu.name,
                        "arguments": json.dumps(tu.args),
                    },
                }
                for tu in tool_uses
            ]

        yield ("", LLMResponse(
            text=full_text or None,
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

    def format_tools(self, tools: list[dict]) -> list[dict]:
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("parameters", t.get("input_schema", {})),
            }
            for t in tools
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_anthropic_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Convert OpenAI-style canonical messages to Anthropic wire format.
    """
    result: list[dict[str, Any]] = []
    i = 0

    while i < len(messages):
        msg = messages[i]
        role = msg["role"]

        if role in ("user", "system"):
            result.append({"role": role, "content": msg["content"]})
            i += 1

        elif role == "assistant":
            content_blocks: list[dict] = []
            if msg.get("content"):
                content_blocks.append({"type": "text", "text": msg["content"]})
            for tc in msg.get("tool_calls", []):
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": json.loads(tc["function"]["arguments"]),
                })
            result.append({
                "role": "assistant",
                "content": content_blocks or msg.get("content", ""),
            })
            i += 1

        elif role == "tool":
            tool_results: list[dict] = []
            while i < len(messages) and messages[i]["role"] == "tool":
                tr = messages[i]
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tr["tool_call_id"],
                    "content": tr["content"],
                })
                i += 1
            result.append({"role": "user", "content": tool_results})

        else:
            i += 1

    return result
