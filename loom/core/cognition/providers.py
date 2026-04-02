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

import asyncio
import json
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


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
            # Try to deserialise JSON values; fall back to raw string.
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

    def __init__(self, api_key: str, model: str = "MiniMax-M2.7") -> None:
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key, base_url=self.BASE_URL)
        self.model = model

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 8096,
    ) -> LLMResponse:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._sync_chat, messages, tools, max_tokens
        )

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

        # --- Normalize tool calls ---
        tool_uses: list[ToolUse] = []

        if msg.tool_calls:
            # Standard OpenAI-style tool_calls (preferred)
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
            # XML fallback
            tool_uses = _parse_xml_tool_calls(content_text)

        # --- Normalize stop_reason ---
        finish = choice.finish_reason or "stop"
        if finish in ("tool_calls", "function_call"):
            stop_reason = "tool_use"
        elif finish == "length":
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"

        if tool_uses and stop_reason != "tool_use":
            stop_reason = "tool_use"

        # --- Build raw_message for history ---
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

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6") -> None:
        import anthropic as _anthropic
        self._client = _anthropic.Anthropic(api_key=api_key)
        self.model = model

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 8096,
    ) -> LLMResponse:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._sync_chat, messages, tools, max_tokens
        )

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

        # Build OpenAI-style raw_message
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

    def format_tool_result(
        self, tool_use_id: str, content: str, success: bool = True
    ) -> dict[str, Any]:
        # Anthropic tool results also go back in OpenAI format at the router level;
        # conversion happens inside _sync_chat via _to_anthropic_messages.
        return {
            "role": "tool",
            "tool_call_id": tool_use_id,
            "content": content if success else f"Error: {content}",
        }

    def format_tools(self, tools: list[dict]) -> list[dict]:
        # Anthropic does not use the OpenAI wrapper format.
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

    OpenAI tool result: {"role": "tool", "tool_call_id": "...", "content": "..."}
    Anthropic tool result: {"role": "user", "content": [{"type": "tool_result", ...}]}

    OpenAI assistant with tool_calls:
        {"role": "assistant", "content": "...", "tool_calls": [...]}
    Anthropic:
        {"role": "assistant", "content": [{"type": "text"}, {"type": "tool_use", ...}]}
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
            result.append({"role": "assistant",
                           "content": content_blocks or msg.get("content", "")})
            i += 1

        elif role == "tool":
            # Collect consecutive tool results into one user message
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
