"""
LLM request forensics.

Keeps a small ring buffer of recent LLM requests in memory; when an API call
fails (e.g. MiniMax error 2013 — "tool call result does not follow tool call"),
dumps the failed payload + the recent buffer to ``~/.loom/debug/`` so the
exact message structure is recoverable for offline diagnosis.

Captures both the OpenAI-canonical input (what the harness held) and the
provider-specific converted output (what was actually wired to the API), so a
conversion-side bug can be told apart from a harness-side one.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BUFFER_SIZE = 5
_DEBUG_DIR = Path.home() / ".loom" / "debug"


def _summarize_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Reduce a message to its structural skeleton — useful for spotting
    orphaned tool_use / tool_result pairs at a glance."""
    role = msg.get("role")
    summary: dict[str, Any] = {"role": role}

    content = msg.get("content")
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for b in content:
            if not isinstance(b, dict):
                blocks.append({"type": type(b).__name__})
                continue
            btype = b.get("type", "?")
            entry: dict[str, Any] = {"type": btype}
            if btype == "tool_use":
                entry["id"] = b.get("id")
                entry["name"] = b.get("name")
            elif btype == "tool_result":
                entry["tool_use_id"] = b.get("tool_use_id")
                tc = b.get("content")
                if isinstance(tc, str):
                    entry["content_len"] = len(tc)
                elif isinstance(tc, list):
                    entry["content_blocks"] = len(tc)
            elif btype == "text":
                entry["text_len"] = len(b.get("text", ""))
            elif btype == "thinking":
                entry["thinking_len"] = len(b.get("thinking", ""))
                entry["has_signature"] = "signature" in b
            blocks.append(entry)
        summary["content"] = blocks
    elif isinstance(content, str):
        summary["content_len"] = len(content)

    if msg.get("tool_calls"):
        summary["tool_calls"] = [
            {"id": tc.get("id"), "name": (tc.get("function") or {}).get("name")}
            for tc in msg["tool_calls"]
        ]
    if msg.get("tool_call_id"):
        summary["tool_call_id"] = msg["tool_call_id"]
    if "_thinking_blocks" in msg:
        summary["_thinking_blocks"] = [
            {
                "thinking_len": len(tb.get("thinking", "")),
                "has_signature": "signature" in tb,
            }
            for tb in msg["_thinking_blocks"]
        ]
    return summary


class LLMForensics:
    """In-memory ring buffer of recent LLM request payloads.

    A single instance is shared per process via :func:`get_forensics`.
    """

    def __init__(self, buffer_size: int = _BUFFER_SIZE) -> None:
        self._buffer: deque[dict[str, Any]] = deque(maxlen=buffer_size)

    def record(
        self,
        *,
        provider: str,
        model: str,
        canonical_messages: list[dict[str, Any]],
        wire_messages: list[dict[str, Any]],
        tools_count: int,
    ) -> None:
        entry = {
            "ts": time.time(),
            "ts_iso": datetime.now().isoformat(),
            "provider": provider,
            "model": model,
            "tools_count": tools_count,
            "canonical_count": len(canonical_messages),
            "wire_count": len(wire_messages),
            "canonical_summary": [_summarize_message(m) for m in canonical_messages],
            "wire_summary": [_summarize_message(m) for m in wire_messages],
        }
        self._buffer.append(entry)

    def dump_on_failure(
        self,
        *,
        provider: str,
        model: str,
        canonical_messages: list[dict[str, Any]],
        wire_messages: list[dict[str, Any]],
        error: BaseException,
    ) -> Path | None:
        try:
            _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
            err_text = f"{type(error).__name__}: {error}"
            payload = {
                "ts": datetime.now().isoformat(),
                "provider": provider,
                "model": model,
                "error": err_text,
                "status_code": (
                    getattr(error, "status_code", None)
                    or getattr(error, "status", None)
                ),
                "failed_request": {
                    "canonical_count": len(canonical_messages),
                    "wire_count": len(wire_messages),
                    "canonical_summary": [
                        _summarize_message(m) for m in canonical_messages
                    ],
                    "wire_summary": [_summarize_message(m) for m in wire_messages],
                    "canonical_full": canonical_messages,
                    "wire_full": wire_messages,
                },
                "recent_requests": list(self._buffer),
            }
            path = _DEBUG_DIR / f"llm_failure_{ts}.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str)
            )
            logger.warning(
                "LLM request failed (%s) — forensics dumped to %s",
                err_text[:160], path,
            )
            return path
        except Exception as exc:
            logger.error("Failed to write LLM forensics dump: %s", exc, exc_info=True)
            return None


_global: LLMForensics | None = None


def get_forensics() -> LLMForensics:
    global _global
    if _global is None:
        _global = LLMForensics()
    return _global
