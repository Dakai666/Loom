"""
Action-row output capture (#569).

``capture_output_fields`` projects a tool's raw return into the
``action_records.output_*`` columns. Recall reads ``output_prefix`` to show what
a past action returned (``loom/core/memory/search.py``).

The ``predict`` bet machinery this module used to host (observation refs,
settlement lookup) was retired in #674.
"""

from __future__ import annotations

import hashlib

# How much of a tool's raw return the action row keeps verbatim. The digest +
# full length are always kept. Full text stays in session_log — this cap is
# about not doubling multi-MB outputs into every action row.
OUTPUT_PREFIX_MAX_CHARS = 16_384


def capture_output_fields(
    output: object, *, success: bool, error: str | None
) -> dict:
    """Project a tool's raw return into the action-row capture columns (#569).

    The canonical text is the same world-reply the agent saw in-conversation
    (``str(result.output)`` on success, the error string on failure).
    ``output_rows`` is element count for list/tuple, line count for text.
    """
    if success:
        raw = output
        text = "" if raw is None else (raw if isinstance(raw, str) else str(raw))
        rows = len(raw) if isinstance(raw, (list, tuple)) else len(text.splitlines())
    else:
        text = error or ""
        rows = len(text.splitlines())
    return {
        "output_prefix": text[:OUTPUT_PREFIX_MAX_CHARS],
        "output_digest": hashlib.sha256(
            text.encode("utf-8", "replace")
        ).hexdigest(),
        "output_len": len(text),
        "output_rows": rows,
    }
