"""
LLM Provider abstractions.

All providers expose a single async `chat()` method that returns a
normalized `LLMResponse`.  The rest of the framework never imports
a provider SDK directly — it always goes through this interface.

Supported providers
-------------------
AnthropicProvider  — api.anthropic.com  (also MiniMax via base_url="https://api.minimax.io/anthropic")
OpenRouterProvider — openrouter.ai/api/v1 (OpenAI-compatible aggregator)
DeepSeek           — official api.deepseek.com via Anthropic-compatible endpoint
                     (registered as AnthropicProvider with name="deepseek",
                      base_url="https://api.deepseek.com/anthropic")
OllamaProvider     — local Ollama server (default http://localhost:11434/v1)
LMStudioProvider   — local LM Studio server (default http://localhost:1234/v1)

Internal message format (OpenAI-style, used as canonical across the framework)
----
User:      {"role": "user", "content": "..."}
Assistant: {"role": "assistant", "content": "...", "tool_calls": [...]}  # tool_calls optional
           May also carry "_thinking_blocks": [{"type": "thinking", "thinking": "..."}]
Tool:      {"role": "tool", "tool_call_id": "...", "content": "..."}
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .forensics import get_forensics


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504, 529}


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
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
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
# Anthropic provider
# ---------------------------------------------------------------------------

class AnthropicProvider(LLMProvider):
    """
    Anthropic Claude via the anthropic SDK.

    Also handles MiniMax models via the Anthropic-compatible endpoint:
        AnthropicProvider(
            api_key=minimax_key,
            base_url="https://api.minimax.io/anthropic",
            name="minimax",
            model="MiniMax-M2.7",
        )

    Messages are converted to/from Anthropic format internally.
    The canonical external format is always OpenAI-style.
    Thinking blocks (type="thinking") are preserved in raw_message["_thinking_blocks"]
    so that multi-turn tool use works correctly with reasoning models.
    """

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-6",
        timeout: float = 60.0,
        max_retries: int = 3,
        base_url: str | None = None,
        name: str | None = None,
    ) -> None:
        import anthropic as _anthropic
        if name is not None:
            self.name = name
        self._api_key = api_key
        self._client = _anthropic.Anthropic(
            api_key=api_key, base_url=base_url, timeout=timeout
        )
        self._async_client = _anthropic.AsyncAnthropic(
            api_key=api_key, base_url=base_url, timeout=timeout
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
        system_text, anthropic_msgs = _to_anthropic_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": anthropic_msgs,
        }
        if system_text:
            kwargs["system"] = [
                {"type": "text", "text": system_text,
                 "cache_control": {"type": "ephemeral"}}
            ]
        if tools:
            kwargs["tools"] = [
                {"name": t["name"], "description": t.get("description", ""),
                 "input_schema": t.get("parameters", t.get("input_schema", {}))}
                for t in tools
            ]

        forensics = get_forensics()
        forensics.record(
            provider="anthropic",
            model=self.model,
            canonical_messages=messages,
            wire_messages=anthropic_msgs,
            tools_count=len(tools or []),
        )
        try:
            response = self._client.messages.create(**kwargs)
        except BaseException as _exc:
            forensics.dump_on_failure(
                provider="anthropic",
                model=self.model,
                canonical_messages=messages,
                wire_messages=anthropic_msgs,
                error=_exc,
            )
            raise

        text: str | None = None
        tool_uses: list[ToolUse] = []
        thinking_blocks: list[dict] = []

        for block in response.content:
            if block.type == "thinking":
                thinking_blocks.append({"type": "thinking", "thinking": block.thinking})
            elif hasattr(block, "text"):
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
        if thinking_blocks:
            raw_message["_thinking_blocks"] = thinking_blocks
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

        cache_read = (
            getattr(response.usage, "cache_read_input_tokens", 0)  # Anthropic
            or getattr(response.usage, "prompt_cache_hit_tokens", 0)  # DeepSeek
            or 0
        )
        cache_creation = (
            getattr(response.usage, "cache_creation_input_tokens", 0)  # Anthropic
            or getattr(response.usage, "prompt_cache_miss_tokens", 0)  # DeepSeek
            or 0
        )
        return LLMResponse(
            text=text,
            tool_uses=tool_uses,
            stop_reason=stop_reason,
            input_tokens=response.usage.input_tokens if response.usage else 0,
            output_tokens=response.usage.output_tokens if response.usage else 0,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
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
        system_text, anthropic_msgs = _to_anthropic_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": anthropic_msgs,
        }
        if system_text:
            kwargs["system"] = system_text
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

        forensics = get_forensics()
        forensics.record(
            provider="anthropic",
            model=self.model,
            canonical_messages=messages,
            wire_messages=anthropic_msgs,
            tools_count=len(tools or []),
        )

        aborted = False
        _base_delay = 1.0
        _forensics_dumped = False
        _retry_started = time.monotonic()
        for _attempt in range(self._max_retries):
            _yielded_any = False
            try:
                async with self._async_client.messages.stream(**kwargs) as stream:
                    async for text in stream.text_stream:
                        if abort_signal is not None and abort_signal.is_set():
                            aborted = True
                            break
                        _yielded_any = True
                        full_text += text
                        yield (text, None)
                    final = None if aborted else await stream.get_final_message()
                break  # success
            except BaseException as _exc:
                # Dump forensics on the first failure only — retries on the
                # same payload would just produce identical dumps.
                if not _forensics_dumped:
                    forensics.dump_on_failure(
                        provider="anthropic",
                        model=self.model,
                        canonical_messages=messages,
                        wire_messages=anthropic_msgs,
                        error=_exc,
                    )
                    _forensics_dumped = True
                if _yielded_any:
                    raise  # mid-stream failure — can't retry safely
                exc_name = type(_exc).__name__.lower()
                status = getattr(_exc, "status_code", None) or getattr(_exc, "status", None)
                retryable = (
                    isinstance(_exc, asyncio.TimeoutError)
                    or (isinstance(status, int) and status in _RETRYABLE_STATUSES)
                    or "timeout" in exc_name
                    or "connection" in exc_name
                )
                if not retryable or _attempt == self._max_retries - 1:
                    # Annotate the exception with retry context so the
                    # user-visible error is not just an empty "ReadTimeout: ".
                    elapsed = time.monotonic() - _retry_started
                    attempts = _attempt + 1
                    orig = str(_exc) or type(_exc).__name__
                    note = f" (after {attempts} attempt(s) in {elapsed:.1f}s)"
                    try:
                        _exc.args = (orig + note, *_exc.args[1:])
                    except Exception:
                        pass
                    raise
                await asyncio.sleep(_base_delay * (2 ** _attempt))

        thinking_blocks: list[dict] = []
        if final is not None:
            for block in final.content:
                if block.type == "thinking":
                    thinking_blocks.append({"type": "thinking", "thinking": block.thinking})
                elif block.type == "tool_use":
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
        if thinking_blocks:
            raw_message["_thinking_blocks"] = thinking_blocks
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
# Local OpenAI-compatible providers (Ollama, LM Studio)
# ---------------------------------------------------------------------------

class _OpenAICompatibleBase(LLMProvider):
    """
    Shared base for any provider that speaks the OpenAI REST API.

    Subclasses set:
      name            — provider name used in the registry
      ROUTING_PREFIX  — e.g. "ollama/" stripped before the API call
      DEFAULT_BASE_URL
      DEFAULT_MODEL   — bare model name (no prefix)
      DEFAULT_TIMEOUT — local models may need more time
    """

    name: str = ""
    ROUTING_PREFIX: str = ""
    DEFAULT_BASE_URL: str = ""
    DEFAULT_MODEL: str = ""
    DEFAULT_TIMEOUT: float = 120.0

    def __init__(
        self,
        base_url: str = "",
        model: str = "",
        api_key: str = "local",
        timeout: float = 0.0,
        max_retries: int = 2,
    ) -> None:
        from openai import AsyncOpenAI
        self._base_url = base_url or self.DEFAULT_BASE_URL
        # Accept bare names ("llama3.2") or prefixed ("ollama/llama3.2") — both work
        self.model = model or (self.ROUTING_PREFIX + self.DEFAULT_MODEL)
        self._timeout = timeout or self.DEFAULT_TIMEOUT
        self._max_retries = max_retries
        self._async_client = AsyncOpenAI(
            api_key=api_key,
            base_url=self._base_url,
            timeout=self._timeout,
        )

    def _api_model(self) -> str:
        """Strip routing prefix before sending to the API."""
        return self.model.removeprefix(self.ROUTING_PREFIX)

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 8096,
    ) -> LLMResponse:
        # Collect the full stream into a single response
        text_parts: list[str] = []
        final: LLMResponse | None = None
        async for chunk, resp in self.stream_chat(
            messages=messages, tools=tools, max_tokens=max_tokens
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
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 8096,
        *,
        abort_signal: Any = None,
    ) -> AsyncIterator[tuple[str, LLMResponse | None]]:
        kwargs: dict[str, Any] = {
            "model": self._api_model(),
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = self.format_tools(tools)

        full_content = ""
        full_reasoning = ""
        tc_accum: dict[int, dict] = {}
        finish_reason: str | None = None
        input_tokens = 0
        output_tokens = 0

        stream = await self._async_client.chat.completions.create(**kwargs)
        try:
            async for chunk in stream:
                if abort_signal is not None and abort_signal.is_set():
                    break
                # Some providers (DeepSeek) report usage on the final chunk
                # alongside choices instead of in a usage-only chunk.
                if getattr(chunk, "usage", None):
                    input_tokens = chunk.usage.prompt_tokens or input_tokens
                    output_tokens = chunk.usage.completion_tokens or output_tokens
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                # DeepSeek thinking-mode emits reasoning_content separately
                # from content. The API requires it to be echoed back in
                # subsequent turns or it returns 400, so we have to capture
                # and preserve it on raw_message.
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    full_reasoning += reasoning
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

        if finish_reason in ("tool_calls", "function_call"):
            stop_reason = "tool_use"
        elif finish_reason == "length":
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"
        if tool_uses and stop_reason != "tool_use":
            stop_reason = "tool_use"

        raw_message: dict[str, Any] = {"role": "assistant", "content": full_content}
        if full_reasoning:
            raw_message["reasoning_content"] = full_reasoning
        if tc_accum:
            raw_message["tool_calls"] = [
                {
                    "id": tc_accum[i]["id"],
                    "type": "function",
                    "function": {
                        "name": tc_accum[i]["name"],
                        "arguments": tc_accum[i]["arguments"],
                    },
                }
                for i in sorted(tc_accum)
            ]

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


class OllamaProvider(_OpenAICompatibleBase):
    """
    Ollama local model server via its OpenAI-compatible endpoint.

    Routing prefix: ``ollama/``
    Default base URL: ``http://localhost:11434/v1``

    Usage::

        /model ollama/llama3.2
        /model ollama/qwen2.5-coder:7b

    Configure in loom.toml::

        [providers.ollama]
        enabled = true
        base_url = "http://localhost:11434/v1"   # optional override
        default_model = "llama3.2"
    """

    name = "ollama"
    ROUTING_PREFIX = "ollama/"
    DEFAULT_BASE_URL = "http://localhost:11434/v1"
    DEFAULT_MODEL = "llama3.2"
    DEFAULT_TIMEOUT = 180.0


class OpenRouterProvider(_OpenAICompatibleBase):
    """
    OpenRouter — OpenAI-compatible aggregator routing many models behind one API.

    Routing prefix: ``openrouter/``
    Default base URL: ``https://openrouter.ai/api/v1``

    Model identifiers carry their own provider/model-name segments
    (e.g. ``deepseek/deepseek-v4-pro``), so the full Loom model name is
    ``openrouter/<vendor>/<model>``. Only the leading ``openrouter/`` prefix
    is stripped before the API call — the rest is forwarded verbatim.

    Usage::

        /model openrouter/deepseek/deepseek-v4-pro
        /model openrouter/anthropic/claude-sonnet-4

    Configure in ``.env``::

        OPENROUTER_API_KEY=sk-or-v1-...
    """

    name = "openrouter"
    ROUTING_PREFIX = "openrouter/"
    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
    DEFAULT_MODEL = "deepseek/deepseek-v4-pro"
    DEFAULT_TIMEOUT = 180.0


class LMStudioProvider(_OpenAICompatibleBase):
    """
    LM Studio local inference server via its OpenAI-compatible endpoint.

    Routing prefix: ``lmstudio/``
    Default base URL: ``http://localhost:1234/v1``

    Usage::

        /model lmstudio/phi-4
        /model lmstudio/mistral-7b-instruct

    Configure in loom.toml::

        [providers.lmstudio]
        enabled = true
        base_url = "http://localhost:1234/v1"    # optional override
        default_model = "phi-4"
    """

    name = "lmstudio"
    ROUTING_PREFIX = "lmstudio/"
    DEFAULT_BASE_URL = "http://localhost:1234/v1"
    DEFAULT_MODEL = "phi-4"
    DEFAULT_TIMEOUT = 180.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_anthropic_messages(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """
    Convert OpenAI-style canonical messages to Anthropic wire format.

    Returns ``(system_text, messages)``. Anthropic's API requires the system
    prompt as a top-level ``system`` parameter — not inside the messages
    array. Claude/MiniMax silently accept the wrong shape, but stricter
    Anthropic-compatible endpoints (e.g. DeepSeek) reject it with 400.
    Multiple system messages are concatenated with double newlines.
    """
    result: list[dict[str, Any]] = []
    system_parts: list[str] = []
    i = 0

    while i < len(messages):
        msg = messages[i]
        role = msg["role"]

        if role == "system":
            content = msg.get("content", "")
            if content:
                system_parts.append(content)
            i += 1

        elif role == "user":
            result.append({"role": "user", "content": msg["content"]})
            i += 1

        elif role == "assistant":
            content_blocks: list[dict] = []
            # Preserve thinking blocks so reasoning models maintain their chain across turns
            for tb in msg.get("_thinking_blocks", []):
                content_blocks.append(tb)
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

    system_text = "\n\n".join(system_parts) if system_parts else None
    return system_text, result
