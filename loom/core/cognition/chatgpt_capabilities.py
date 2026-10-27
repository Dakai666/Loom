"""Account catalog metadata and opt-in synthetic inference checks.

Listed, advertised, and actually verified capabilities are kept separate.
Audio and image generation are unavailable on the SIWC preview route even
if a model's generic metadata advertises them.
"""
from __future__ import annotations

import base64
import random
import re
import struct
import zlib
from typing import Any

from .chatgpt_provider import ChatGPTResponsesProvider
from .responses_sse import ResponsesProviderError

LIMITATIONS_URL = "https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations"


class ModelVerificationError(RuntimeError):
    def __init__(self, result: dict[str, Any], cause: Exception):
        super().__init__(str(cause))
        self.result = {**result, "verification": "failed", "error": str(cause)}


def describe_model(entry: dict[str, Any]) -> dict[str, Any]:
    modalities = entry.get("supported_input_modalities", entry.get("input_modalities"))
    if not isinstance(modalities, list):
        modalities = (entry.get("modalities") or {}).get("input") if isinstance(entry.get("modalities"), dict) else None
    def advertised(kind: str) -> str:
        return ("advertised" if kind in modalities else "not_advertised") if isinstance(modalities, list) else "unknown"
    return {"model": entry["slug"], "display_name": entry.get("display_name", entry["slug"]),
            "context_window": entry.get("context_window"),
            "listed": True, "text_input": advertised("text"), "image_input": advertised("image"),
            "audio_input": "unsupported_by_route", "audio_output": "unsupported_by_route",
            "image_generation": "unsupported_by_route", "verification": "not_tested"}


def _test_image() -> tuple[str, str, str]:
    palette = {"red": (255, 0, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0), "green": (0, 180, 0)}
    left, right = random.sample(list(palette), 2)
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    row = bytes(palette[left]) * 128 + bytes(palette[right]) * 128
    raw = (b"\0" + row) * 128
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 256, 128, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode(), left, right


async def verify_image(provider: ChatGPTResponsesProvider) -> dict[str, Any]:
    image, left, right = _test_image()
    reply = await provider.chat([{"role": "user", "content": [
        {"type": "text", "text": "What are the two colors in this image? Reply with exactly two English color words: left half first, then right half, separated by a comma."},
        {"type": "image", "source": {"kind": "url", "ref": "data:image/png;base64," + image}},
    ]}])
    observation = reply.text or ""
    colors = re.findall(r"\b(red|blue|yellow|green)\b", observation.lower())
    return {"image_transport": "accepted", "image_input": "verified" if colors == [left, right] else "inconclusive",
            "image_check": {"expected": [left, right], "observation": observation}}


async def verify_model(provider: ChatGPTResponsesProvider) -> dict[str, Any]:
    """Four small synthetic requests: text, image, tool call, tool-result replay.

    No actual shell/file tools are executed. A failed assertion is inconclusive,
    not a claim the model lacks the capability. Transport/quota errors propagate.
    """
    result: dict[str, Any] = {"model": provider._api_model(), "text": "not_tested", "image_input": "not_tested",
                              "tools": "not_tested", "audio_input": "unsupported_by_route",
                              "audio_output": "unsupported_by_route", "limitations": LIMITATIONS_URL}
    async def request(messages, tools=None):
        try:
            return await provider.chat(messages, tools=tools)
        except ResponsesProviderError as exc:
            raise ModelVerificationError(result, exc) from exc

    text = await request([{"role": "user", "content": "Reply with exactly: loom-text-ok"}])
    result["text"] = "verified" if "loom-text-ok" in (text.text or "").lower() else "inconclusive"
    try:
        result.update(await verify_image(provider))
    except ResponsesProviderError as exc:
        if "subscription_sharing_unsupported_capability" not in str(exc):
            raise ModelVerificationError(result, exc) from exc
        result["image_input"] = "unsupported"
        result["image_error"] = str(exc)
    tools = [{"name": "loom_probe", "description": "Return a test nonce for the connectivity check.",
              "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}]
    messages = [{"role": "user", "content": "Call loom_probe once, then repeat the exact nonce it returns."}]
    try:
        call = await request(messages, tools=tools)
    except ModelVerificationError as exc:
        if "subscription_sharing_unsupported_capability" not in str(exc):
            raise
        result["tools"] = "unsupported"
        result["tools_error"] = str(exc)
        return result
    if len(call.tool_uses) == 1 and call.tool_uses[0].name == "loom_probe":
        nonce = "loom-tool-" + str(random.randrange(10**8, 10**9))
        messages.extend([call.raw_message, provider.format_tool_result(call.tool_uses[0].id, nonce)])
        replay = await request(messages, tools=tools)
        result["tools"] = "verified" if nonce in (replay.text or "") else "inconclusive"
    else:
        result["tools"] = "inconclusive"
    return result
