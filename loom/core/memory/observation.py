"""
Prediction Spine — observation_ref contract (epic #528, issue #531).

An ``observation_ref`` ties a reconciled prediction's score to the exact
runtime observation that produced it. It is the auditable backbone of I2:
ground truth comes only from runtime observation tables, never from an LLM
self-narrative store, and every score can be reverse-looked-up to the row it
came from.

Format
------
``"<source>:<row_id>"`` where ``source`` is on ``OBSERVATION_SOURCES``. The
whitelist *is* the I2 boundary — ``action`` → ``action_records`` and
``session_log`` → ``session_log`` are the only runtime observation tables a
prediction may be reconciled against.

Reverse lookup
--------------
``resolve_observation_ref(db, ref)`` returns a *normalized observation* (the
dict shape ``loom.core.memory.resolvers.resolve`` consumes), or ``None`` when
the row is gone — an unreconcilable bet, **not** a correct one.

Layering
--------
This module lives in the memory layer (the bottom of
Platform→Cognition→Harness→Memory) and must **not** import the harness. The
``ActionState`` failure values are mirrored locally as plain strings; a drift
guard in ``tests/test_observation_ref.py`` keeps the mirror honest.
"""

from __future__ import annotations

import hashlib
import json
import re

import aiosqlite

# I2 boundary: the only sources a prediction may be reconciled against. A ref
# prefix outside this set is rejected at parse time.
OBSERVATION_SOURCES = ("action", "session_log")

_PREFIX_TO_TABLE = {"action": "action_records", "session_log": "session_log"}

# Local mirror of harness ActionState._FAILURE_STATES / _TERMINAL_STATES values
# (see loom/core/harness/lifecycle.py). Duplicated rather than imported to
# preserve the one-way memory-layer dependency. Both drift-guarded in the suite.
_FAILURE_STATE_VALUES = frozenset({"denied", "aborted", "timed_out", "reverted"})
_TERMINAL_STATE_VALUES = frozenset({"memorialized", "denied", "aborted", "timed_out"})

_REF_RE = re.compile(r"^(?P<prefix>[a-z_]+):(?P<row_id>.+)$")

# Semantic observation surface (#569, spec docs/designs/59 §9 Q2): how much of a
# tool's raw return the action row keeps verbatim. The digest + full length are
# always kept, so a needle that misses a truncated capture is *unresolvable*
# (honest), never a false "absent". Full text stays in session_log — this cap is
# about not doubling multi-MB outputs into every action row.
OUTPUT_PREFIX_MAX_CHARS = 16_384


def capture_output_fields(
    output: object, *, success: bool, error: str | None
) -> dict:
    """Project a tool's raw return into the action-row capture columns (#569).

    The canonical text is the same world-reply the agent saw in-conversation
    (``str(result.output)`` on success, the error string on failure — an error
    body like a 429 is world-generated too, and bettable). ``output_rows`` is a
    pure world function: element count for list/tuple, line count for text —
    no semantic guessing (I2).
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


def make_observation_ref(source: str, row_id: object) -> str:
    """Build a ``"<source>:<row_id>"`` ref. Rejects non-whitelist sources."""
    if source not in OBSERVATION_SOURCES:
        raise ValueError(
            f"observation source {source!r} not in whitelist {OBSERVATION_SOURCES}"
        )
    rid = str(row_id)
    if not rid.strip():
        raise ValueError("observation_ref row_id is required")
    return f"{source}:{rid}"


def parse_observation_ref(ref: str) -> tuple[str, str]:
    """Validate + split a ref into ``(source, row_id)``.

    Raises ``ValueError`` for malformed refs and for any source outside the
    runtime-observation whitelist (the I2 boundary).
    """
    m = _REF_RE.match(ref or "")
    if not m:
        raise ValueError(
            f"malformed observation_ref {ref!r}; expected '<source>:<row_id>'"
        )
    prefix = m.group("prefix")
    if prefix not in OBSERVATION_SOURCES:
        raise ValueError(
            f"observation_ref source {prefix!r} not in whitelist "
            f"{OBSERVATION_SOURCES} (I2: ground truth may only come from "
            "runtime observation tables, never an LLM-narrative store)"
        )
    return prefix, m.group("row_id")


async def resolve_observation_ref(
    db: aiosqlite.Connection, ref: str
) -> dict | None:
    """Reverse-look-up a ref to a normalized observation, or ``None`` if gone."""
    source, row_id = parse_observation_ref(ref)
    if source == "action":
        return await _resolve_action(db, row_id)
    return await _resolve_session_log(db, row_id)


async def _find_after_action(
    db: aiosqlite.Connection, due_condition: dict
) -> str | None:
    """Settle once the named tool call has a **terminal** action_records row.
    Returns the latest such row's ref, or ``None`` if not settled yet."""
    call_id = due_condition.get("call_id")
    if not call_id:
        raise ValueError("after_action due_condition requires call_id")
    placeholders = ",".join("?" * len(_TERMINAL_STATE_VALUES))
    cur = await db.execute(
        f"SELECT id FROM action_records WHERE call_id = ? "
        f"AND final_state IN ({placeholders}) "
        "ORDER BY created_at DESC LIMIT 1",
        (call_id, *sorted(_TERMINAL_STATE_VALUES)),
    )
    r = await cur.fetchone()
    return make_observation_ref("action", r[0]) if r else None


async def _find_next_action(
    db: aiosqlite.Connection, due_condition: dict
) -> str | None:
    """Settle against the *next* terminal action for a (session, tool) pair.

    The explicit ``predict`` tool (P0.5-a slice A) bets *before* its target runs,
    so it has no ``call_id`` to key on like ``after_action`` does. It binds by
    ``session_id`` + ``tool`` + an ``after`` anchor instead, and settles against
    the **earliest terminal** ``action_records`` row for that pair created
    *strictly after* the anchor — i.e. the very next time that tool runs. The
    predicting tool-call's own row is created at/before the anchor, so the
    ``created_at > after`` guard excludes it. Still I2-clean: ground truth is an
    ``action_records`` row, never an LLM narrative.
    """
    session_id = due_condition.get("session_id")
    tool = due_condition.get("tool")
    after = due_condition.get("after")
    if not session_id or not tool or not after:
        raise ValueError(
            "next_action due_condition requires session_id, tool, and after"
        )
    placeholders = ",".join("?" * len(_TERMINAL_STATE_VALUES))
    cur = await db.execute(
        f"SELECT id FROM action_records WHERE session_id = ? AND tool_name = ? "
        f"AND created_at > ? AND final_state IN ({placeholders}) "
        "ORDER BY created_at ASC LIMIT 1",
        (session_id, tool, after, *sorted(_TERMINAL_STATE_VALUES)),
    )
    r = await cur.fetchone()
    return make_observation_ref("action", r[0]) if r else None


# due_condition kind → finder. The extension point: adding ``at_time`` /
# ``within_window`` later is a registry entry, not an if/elif chain (絲絲
# review, PR #532 OQ1). ``next_action`` (P0.5-a slice A) joined here.
DUE_CONDITION_KINDS = {
    "after_action": _find_after_action,
    "next_action": _find_next_action,
}


async def find_settling_observation(
    db: aiosqlite.Connection, due_condition: dict
) -> str | None:
    """Find the observation_ref that *settles* a bet, or ``None`` if not yet.

    Dispatches on ``due_condition['kind']`` via ``DUE_CONDITION_KINDS``. An
    unknown kind raises (whitelist, like resolvers).
    """
    kind = due_condition.get("kind")
    finder = DUE_CONDITION_KINDS.get(kind)
    if finder is None:
        raise ValueError(
            f"unknown due_condition kind {kind!r}; "
            f"P0 supports {sorted(DUE_CONDITION_KINDS)}"
        )
    return await finder(db, due_condition)


async def _resolve_action(db: aiosqlite.Connection, row_id: str) -> dict | None:
    cur = await db.execute(
        "SELECT id, tool_name, final_state, duration_ms, state_history, "
        "output_prefix, output_digest, output_len, output_rows "
        "FROM action_records WHERE id = ?",
        (row_id,),
    )
    r = await cur.fetchone()
    if r is None:
        return None
    history = json.loads(r[4] or "[]")
    # Terminal-state-hides-failure: a failed action is eventually MEMORIALIZED,
    # so final_state alone can read as success. Scan the full transition history
    # for any failure state (feedback_terminal_state_hides_failure).
    failed = r[2] in _FAILURE_STATE_VALUES or any(
        t.get("to") in _FAILURE_STATE_VALUES for t in history
    )
    obs = {
        "observation_ref": make_observation_ref("action", r[0]),
        "tool_name": r[1],
        "final_state": r[2],
        "tool_success": not failed,
        "duration_ms": r[3],
    }
    # #569 capture columns. A legacy row (NULL output_len) must NOT grow fake
    # fields — semantic resolvers stay unresolvable against it, never
    # retro-scored (spec 59 §9.1).
    if r[7] is not None:
        prefix = r[5] or ""
        obs["output"] = prefix
        obs["output_truncated"] = r[7] > len(prefix)
        obs["output_digest"] = r[6]
        obs["output_len"] = r[7]
    if r[8] is not None:
        obs["row_count"] = r[8]
    return obs


async def _resolve_session_log(db: aiosqlite.Connection, row_id: str) -> dict | None:
    cur = await db.execute(
        "SELECT id, role, content, raw_json FROM session_log WHERE id = ?",
        (row_id,),
    )
    r = await cur.fetchone()
    if r is None:
        return None
    return {
        "observation_ref": make_observation_ref("session_log", r[0]),
        "role": r[1],
        "output": r[2],
        "raw_json": r[3],
    }
