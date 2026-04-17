"""
Scratchpad — session-scoped ephemeral store for in-flight job artifacts.

Strictly separated from the long-term memory system. Intermediate job
outputs live here and are cleared on session end; final products go
through the memory system or are written to disk by the agent.

Design origin: Issue #154.
"""

from __future__ import annotations

URI_PREFIX = "scratchpad://"


class Scratchpad:
    """In-memory, session-scoped key→bytes store.

    Not thread-safe by design — the harness drives everything through a
    single asyncio loop. Writes are bytes; text convenience helpers
    encode as UTF-8.
    """

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def write(self, ref: str, content: str | bytes) -> str:
        if not ref or "/" in ref or ref.startswith("."):
            raise ValueError(f"Invalid scratchpad ref: {ref!r}")
        payload = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        self._data[ref] = payload
        return f"{URI_PREFIX}{ref}"

    def read(
        self,
        ref: str,
        section: str | None = None,
        max_bytes: int | None = None,
    ) -> str:
        """Read a scratchpad entry as text.

        ``max_bytes`` caps the decoded payload before section filtering — a
        safety net for tools that may return multi-megabyte bodies. When the
        cap trims content, a truncation notice is appended.
        """
        ref = self._strip_uri(ref)
        if ref not in self._data:
            raise KeyError(f"Scratchpad ref not found: {ref}")
        raw = self._data[ref]
        truncated = False
        if max_bytes is not None and len(raw) > max_bytes:
            raw = raw[:max_bytes]
            truncated = True
        text = raw.decode("utf-8", errors="replace")
        if section is not None:
            text = _apply_section(text, section)
        if truncated:
            text = text + f"\n\n[scratchpad_read: output truncated at {max_bytes} bytes]"
        return text

    def size(self, ref: str) -> int:
        ref = self._strip_uri(ref)
        if ref not in self._data:
            raise KeyError(f"Scratchpad ref not found: {ref}")
        return len(self._data[ref])

    def list_refs(self) -> list[str]:
        return sorted(self._data.keys())

    def clear(self) -> None:
        self._data.clear()

    def __contains__(self, ref: str) -> bool:
        return self._strip_uri(ref) in self._data

    @staticmethod
    def _strip_uri(ref: str) -> str:
        return ref[len(URI_PREFIX):] if ref.startswith(URI_PREFIX) else ref


def _apply_section(text: str, section: str) -> str:
    """Apply a section filter matching task_read semantics.

    Supported:
      - "head"     → first 50 lines
      - "tail"     → last 50 lines
      - "N-M"      → lines N..M inclusive (1-indexed)
      - any other  → treat as keyword; return lines containing it
    """
    lines = text.splitlines()
    if section == "head":
        return "\n".join(lines[:50])
    if section == "tail":
        return "\n".join(lines[-50:])
    if "-" in section:
        try:
            lo, hi = section.split("-", 1)
            i, j = int(lo), int(hi)
            if i >= 1 and j >= i:
                return "\n".join(lines[i - 1:j])
        except ValueError:
            pass
    matches = [line for line in lines if section in line]
    return "\n".join(matches)
