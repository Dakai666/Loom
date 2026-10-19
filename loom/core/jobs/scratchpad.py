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
        *,
        head: int | None = None,
        tail: int | None = None,
        lines: str | None = None,
        grep: str | None = None,
        max_bytes: int | None = None,
    ) -> str:
        """Read a scratchpad entry as text.

        At most one line filter applies, each with a single meaning:
        ``head``/``tail`` = first/last N lines, ``lines`` = ``"N-M"``
        (1-indexed, inclusive), ``grep`` = lines containing the substring.
        Filters always see the full body (2026-10-19 diagnosis: filtering a
        byte-prefix made ``tail`` land mid-file and ``grep`` miss).

        ``max_bytes`` caps the *output* after filtering — a safety net for
        multi-megabyte bodies. The cut lands on a character boundary.

        Whenever a filter or the cap is involved, a trailing
        ``[scratchpad_read: ...]`` note says what was selected, how many
        lines matched, and whether output was truncated — so "matched 0"
        is never mistaken for "truncated, retry with more bytes".

        Raises ``ValueError`` for conflicting filters or a malformed range.
        """
        ref = self._strip_uri(ref)
        if ref not in self._data:
            raise KeyError(f"Scratchpad ref not found: {ref}")
        raw = self._data[ref]
        text = raw.decode("utf-8", errors="replace")

        notes: list[str] = []
        chosen = [n for n, v in (("head", head), ("tail", tail),
                                 ("lines", lines), ("grep", grep)) if v is not None]
        if len(chosen) > 1:
            raise ValueError(
                f"Use at most one of head/tail/lines/grep (got {', '.join(chosen)})"
            )
        if chosen:
            all_lines = text.splitlines()
            selected, desc = _apply_filter(all_lines, head, tail, lines, grep)
            text = "\n".join(selected)
            notes.append(
                f"{desc} matched {len(selected)} of {len(all_lines)} lines "
                f"(full body, {len(raw)} bytes)"
            )

        encoded = text.encode("utf-8")
        if max_bytes is not None and len(encoded) > max_bytes:
            text = encoded[:max_bytes].decode("utf-8", errors="ignore")
            notes.append(
                f"output truncated at {max_bytes} of {len(encoded)} bytes; "
                "narrow with lines='N-M' or raise max_bytes"
            )

        if notes:
            text = text + f"\n\n[scratchpad_read: {'; '.join(notes)}]"
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
        # Handle both canonical ``scratchpad://ref`` and the single-colon
        # ``scratchpad:ref`` format that JIT / masking placeholders emit.
        # Issue #243: agents copy the ref from placeholder text and the
        # single-colon variant was silently failing lookups.
        if ref.startswith(URI_PREFIX):
            return ref[len(URI_PREFIX):]
        if ref.startswith("scratchpad:"):
            return ref[len("scratchpad:"):]
        return ref


def _apply_filter(
    lines: list[str],
    head: int | None,
    tail: int | None,
    line_range: str | None,
    grep: str | None,
) -> tuple[list[str], str]:
    """Select lines by exactly one filter; return (selected, description).

    Replaces the old single ``section`` string that guessed between
    head/tail/range/keyword from its content — "2024-2025" was silently
    read as a line range (2026-10-19 diagnosis, T4).
    """
    if head is not None:
        return lines[:max(head, 0)], f"head={head}"
    if tail is not None:
        return (lines[-tail:] if tail > 0 else []), f"tail={tail}"
    if line_range is not None:
        lo_s, sep, hi_s = line_range.partition("-")
        try:
            if not sep:
                raise ValueError
            i, j = int(lo_s), int(hi_s)
        except ValueError:
            raise ValueError(
                f"lines must be 'N-M' (1-indexed, inclusive), got {line_range!r}"
            ) from None
        # Clamp i to 1 so "0-500" reads from the start (#302 F2). Out-of-range
        # j is harmless thanks to slice semantics.
        i = max(i, 1)
        if j < i:
            raise ValueError(f"lines range is inverted: {line_range!r}")
        return lines[i - 1:j], f"lines={line_range}"
    assert grep is not None
    return [line for line in lines if grep in line], f"grep={grep!r}"
