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

        Line numbers count ``\\n`` only (matching read_file / grep -n);
        grep hits are prefixed ``"N: "`` so they can be followed up with
        ``lines``.

        ``max_bytes`` caps the *output* after filtering — a safety net for
        multi-megabyte bodies. Filtered output keeps whole lines; the cut
        never splits a UTF-8 character.

        Whenever a filter or the cap is involved, a trailing
        ``[scratchpad_read: ...]`` note says what was selected, how many
        lines matched, and where truncation stopped — so "matched 0" is never
        mistaken for "truncated, retry with more bytes". An unfiltered read
        under the cap returns the body untouched.

        Raises ``ValueError`` for conflicting filters, a malformed range, a
        non-positive head/tail, or an empty grep.
        """
        ref = self._strip_uri(ref)
        if ref not in self._data:
            raise KeyError(f"Scratchpad ref not found: {ref}")
        raw = self._data[ref]

        chosen = [n for n, v in (("head", head), ("tail", tail),
                                 ("lines", lines), ("grep", grep)) if v is not None]
        if len(chosen) > 1:
            raise ValueError(
                f"Use at most one of head/tail/lines/grep (got {', '.join(chosen)})"
            )

        if not chosen:
            if max_bytes is None or len(raw) <= max_bytes:
                return raw.decode("utf-8", errors="replace")
            # Fast path: slice bytes first — never decode a multi-MB body
            # just to throw most of it away.
            shown = _decode_prefix(raw, max_bytes)
            last = shown.count("\n") + 1
            total = _line_count(raw)
            return shown + (
                f"\n\n[scratchpad_read: output truncated at {max_bytes} of "
                f"{len(raw)} bytes, mid-line {last} of {total}; continue with "
                f"lines='{last}-{total}' or raise max_bytes]"
            )

        all_lines = raw.decode("utf-8", errors="replace").split("\n")
        if all_lines and all_lines[-1] == "":
            all_lines.pop()  # a trailing newline does not start another line
        selected, desc = _apply_filter(all_lines, head, tail, lines, grep)
        verb = "matched" if grep is not None else "selected"
        note = (f"{desc} {verb} {len(selected)} of {len(all_lines)} lines "
                f"(full body, {len(raw)} bytes)")
        # grep output is numbered so a hit can be followed up with lines='N-M'.
        rendered = [f"{n}: {t}" if grep is not None else t for n, t in selected]

        if max_bytes is not None:
            budget, kept = max_bytes, 0
            for line in rendered:
                cost = len(line.encode("utf-8")) + (1 if kept else 0)
                if cost > budget:
                    break
                budget -= cost
                kept += 1
            if kept < len(rendered):
                if kept == 0:
                    # A single line larger than the cap: show its head.
                    out = _decode_prefix(rendered[0].encode("utf-8"), max_bytes)
                    shown = f"a partial line {selected[0][0]}"
                else:
                    out = "\n".join(rendered[:kept])
                    shown = f"{kept} lines through line {selected[kept - 1][0]}"
                nxt = selected[kept][0] if kept else selected[0][0]
                return out + (
                    f"\n\n[scratchpad_read: {note}; output truncated at "
                    f"{max_bytes} bytes, showing {shown}; continue from line "
                    f"{nxt} or raise max_bytes]"
                )
        return "\n".join(rendered) + f"\n\n[scratchpad_read: {note}]"

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


def _decode_prefix(raw: bytes, n: int) -> str:
    """Decode ``raw[:n]`` without splitting a UTF-8 char at the cut."""
    # An incremental decoder holds back an incomplete trailing sequence
    # instead of emitting U+FFFD for it; invalid bytes elsewhere still show.
    return codecs.getincrementaldecoder("utf-8")("replace").decode(raw[:n])


def _line_count(raw: bytes) -> int:
    return raw.count(b"\n") + (0 if raw.endswith(b"\n") else 1)


def _apply_filter(
    lines: list[str],
    head: int | None,
    tail: int | None,
    line_range: str | None,
    grep: str | None,
) -> tuple[list[tuple[int, str]], str]:
    """Select lines by exactly one filter; return ((lineno, text)…, description).

    Replaces the old single ``section`` string that guessed between
    head/tail/range/keyword from its content — "2024-2025" was silently
    read as a line range (2026-10-19 diagnosis, T4). Line numbers are
    1-indexed over ``\\n`` only, matching read_file / grep -n.
    """
    numbered = list(enumerate(lines, 1))
    if head is not None or tail is not None:
        name, n = ("head", head) if head is not None else ("tail", tail)
        if n < 1:
            raise ValueError(f"{name} must be a positive line count, got {n}")
        return (numbered[:n] if name == "head" else numbered[-n:]), f"{name}={n}"
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
        return numbered[i - 1:j], f"lines={line_range}"
    assert grep is not None
    if not grep:
        raise ValueError("grep must be a non-empty string")
    return [(n, t) for n, t in numbered if grep in t], f"grep={grep!r}"
