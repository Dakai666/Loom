"""
Scratchpad — session-scoped ephemeral store for in-flight job artifacts.

Strictly separated from the long-term memory system. Intermediate job
outputs live here and are cleared on session end; final products go
through the memory system or are written to disk by the agent.

Design origin: Issue #154.
"""

from __future__ import annotations

import codecs

URI_PREFIX = "scratchpad://"
MAX_READ_BYTES = 200_000


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
        payload = (
            content.encode("utf-8") if isinstance(content, str) else bytes(content)
        )
        self._data[ref] = payload
        return f"{URI_PREFIX}{ref}"

    def read(
        self,
        ref: str,
        *,
        offset: int | None = None,
        limit: int | None = None,
        grep: str | None = None,
    ) -> str:
        """Read a 1-indexed window, optionally over substring-matching lines.

        Zero/None offset and limit, and empty grep, are unset. Grep output
        retains original line numbers; its offset counts matching lines.
        A single footer gives the source range and the next window offset.
        Whole-line pages never overlap or skip. An over-cap first line is
        shown as a UTF-8-safe prefix, explicitly marked, then skipped.
        """
        ref = self._strip_uri(ref)
        if ref not in self._data:
            raise KeyError(f"Scratchpad ref not found: {ref}")
        raw = self._data[ref]
        raw_lines = raw.split(b"\n") if raw else []
        if raw_lines and raw_lines[-1] == b"":
            raw_lines.pop()
        lines = [line.decode("utf-8", errors="replace") for line in raw_lines]
        selected = [
            (n, line) for n, line in enumerate(lines, 1) if not grep or grep in line
        ]
        start = max(1, offset or 1) - 1
        window = selected[start : start + (limit or len(selected))]
        # Fixed room for the footer and the tool's external-content wrapper.
        budget = MAX_READ_BYTES - 256
        rendered: list[str] = []
        truncated = ""
        for n, line in window:
            value = f"{n}: {line}" if grep else line
            cost = len(value.encode("utf-8")) + (1 if rendered else 0)
            if cost > budget:
                if not rendered:
                    rendered.append(_decode_prefix(value.encode("utf-8"), budget))
                    truncated = f"; line {n} truncated ({len(raw_lines[n - 1])} bytes)"
                break
            rendered.append(value)
            budget -= cost
        kept = len(rendered)
        first = window[0][0] if kept else 0
        last = window[kept - 1][0] if kept else 0
        note = f"lines {first}–{last} of {len(lines)}"
        if grep:
            note += f"; matched {len(selected)}"
        note += truncated
        if kept and (start + kept < len(selected) or truncated):
            note += f"; continue with offset={start + kept + 1}"
        return "\n".join(rendered) + f"\n\n[{note}]"

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
        # Handle both canonical ``scratchpad://ref`` and the single-colon
        # ``scratchpad:ref`` format that JIT / masking placeholders emit.
        # Issue #243: agents copy the ref from placeholder text and the
        # single-colon variant was silently failing lookups.
        if ref.startswith(URI_PREFIX):
            return ref[len(URI_PREFIX) :]
        if ref.startswith("scratchpad:"):
            return ref[len("scratchpad:") :]
        return ref


def _decode_prefix(raw: bytes, n: int) -> str:
    """Decode ``raw[:n]`` without splitting a UTF-8 char at the cut."""
    # An incremental decoder holds back an incomplete trailing sequence
    # instead of emitting U+FFFD for it; invalid bytes elsewhere still show.
    return codecs.getincrementaldecoder("utf-8")("replace").decode(raw[:n])
