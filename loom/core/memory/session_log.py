




"""
SessionLog — persistent, lossless conversation history.

Two tables:
  sessions     — one row per session (metadata + title)
  session_log  — one row per message (full OpenAI-canonical history)

Design principles:
  - log_message() swallows all exceptions (hot-path, never blocks the agent)
  - INSERT OR IGNORE in create_session handles resume cleanly
  - COALESCE(title, ?) in update_session preserves the first-set title
  - load_messages() returns only non-system rows, ordered for replay
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, UTC
from typing import Any

logger = logging.getLogger(__name__)

import aiosqlite


# ---------------------------------------------------------------------------
# Vision: strip base64 from list content before persistence
# ---------------------------------------------------------------------------

def _strip_vision_blocks(content, metadata):
    """Reduce canonical list content to (text_summary, augmented_metadata)."""
    text_parts = []
    image_refs = []
    for block in content:
        btype = block.get("type")
        if btype == "text":
            txt = block.get("text", "")
            if txt:
                text_parts.append(txt)
        elif btype == "image":
            src = block.get("source", {}) or {}
            image_refs.append({
                "kind": src.get("kind"),
                "ref": src.get("ref") if src.get("kind") in {"url", "file"} else None,
                "media_type": src.get("media_type"),
                "digest": src.get("digest"),
            })
    text_summary = "\n".join(text_parts)
    if not text_summary and image_refs:
        text_summary = f"[vision input: {len(image_refs)} image(s)]"
    md = dict(metadata or {})
    if image_refs:
        md["vision_inputs"] = image_refs
    return text_summary, md



# ---------------------------------------------------------------------------
# Issue #92: secret redaction for session log persistence
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    # key=value / JSON patterns: api_key, token, password, secret, etc.
    (re.compile(
        r'(?i)(["\']?(?:api[_-]?key|token|password|passwd|secret|'
        r'authorization|bearer|credentials|private[_-]?key|access[_-]?key'
        r')["\']?\s*[:=]\s*)(["\']?)([^\s"\',}{]{8,})\2',
    ), r'\1\2[REDACTED]\2'),
    # Bearer token in header value
    (re.compile(r'(?i)(Bearer\s+)\S{8,}'), r'\1[REDACTED]'),
    # Known token prefixes (AWS, GitHub, Slack, OpenAI, Anthropic, etc.)
    (re.compile(
        r'["\'](?:sk-|pk-|ak-|AKIA|ghp_|gho_|ghs_|xoxb-|xoxp-|xapp-)'
        r'[A-Za-z0-9_\-]{20,}["\']'
    ), '"[REDACTED]"'),
    # JSON-escaped quotes: \"password\": \"sk-abc123...\"
    (re.compile(
        r'(?i)(?:password|token|secret|api[_-]?key|credentials)'
        r'\\?"\s*:\s*\\?"((?:sk-|pk-|AKIA|ghp_|xoxb-)[A-Za-z0-9_\-]{20,})\\?"'
    ), r'[REDACTED]"'),
]


def _redact_secrets(text: str | None) -> str | None:
    """Best-effort redaction of common secret patterns.  Never raises.

    #335 — applied at read time on plain-text values *after* JSON has
    been parsed (see ``_redact_in_place``); the patterns assume an
    unstructured string. Running them on a JSON-encoded blob can break
    syntax (e.g. ``Bearer\\s+\\S{8,}`` greedy-matches through escaped
    quotes), so callers walking nested structures should redact at the
    leaf-string level, not on the serialised form.
    """
    if not text:
        return text
    try:
        for pattern, replacement in _SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
    except Exception as exc:
        logger.debug("Secret redaction regex failed: %s", exc)
    return text


def _redact_in_place(node: Any) -> Any:
    """Walk a parsed-JSON structure and redact every leaf string.

    Returns the same shape with strings filtered through
    ``_redact_secrets``. Lists and dicts are mutated in place for
    cheapness; the return value is the same object so callers can use
    it as an expression.

    Intended for freshly parsed / caller-owned structures — anything
    sharing the dict will see its strings rewritten. Today's only call
    site is ``load_messages`` operating on objects it just produced
    via ``json.loads``.
    """
    if isinstance(node, str):
        return _redact_secrets(node)
    if isinstance(node, list):
        for i, v in enumerate(node):
            node[i] = _redact_in_place(v)
        return node
    if isinstance(node, dict):
        for k, v in node.items():
            node[k] = _redact_in_place(v)
        return node
    return node


class SessionLog:
    """Read/write access to the sessions + session_log tables."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._health: Any = None  # Optional MemoryHealthTracker, set post-init

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def create_session(self, session_id: str, model: str, title: str | None = None) -> None:
        """Insert a new session row.  INSERT OR IGNORE is safe for resume."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """
            INSERT OR IGNORE INTO sessions
                (session_id, model, title, started_at, last_active, turn_count)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (session_id, model, title, now, now),
        )
        await self._db.commit()

    async def log_message(
        self,
        session_id: str,
        turn_index: int,
        role: str,
        content: str | list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
        raw_json: str | None = None,
    ) -> None:
        """Append one message row.  Swallows all exceptions — never blocks the loop.

        Parameters
        ----------
        raw_json:
            For ``role="assistant"`` messages that contain tool_call blocks, pass the
            full serialised ``raw_message`` JSON here so it can be stored in the
            dedicated ``raw_json`` column (rather than the human-readable ``content``).
            ``load_messages()`` will prefer this column for assistant-message replay.
        """
        try:
            # Vision turns arrive as canonical *list* content
            # (``[{type:text...}, {type:image...}]``). SQLite cannot bind
            # a list, so reduce it to a text summary + image refs in
            # metadata before the INSERT — no base64 ever reaches disk.
            # #506 added ``_strip_vision_blocks`` for exactly this but
            # never wired it in here, so the list hit the bind and the
            # write was silently dropped ("type 'list' is not supported").
            # Kept inside the try so a malformed block can't break the
            # method's "never blocks the loop" contract.
            if isinstance(content, list):
                content, metadata = _strip_vision_blocks(content, metadata)

            # #335 — secret redaction is applied at read time in
            # ``load_messages``. Keeping the raw text on disk lets
            # future regex improvements re-redact older rows accurately
            # (a bad write-time redaction would have been one-shot).
            #
            # SECURITY NOTE: this is a deliberate threat-model change
            # from Issue #92. #92's write-time redaction protected
            # against at-rest exposure (DB backup leak, lost laptop,
            # file-permission misconfig); this read-time path only
            # covers the load_messages presentation boundary. Plaintext
            # secrets remain in ``~/.loom/memory.db`` on disk. At-rest
            # mitigation tracked in #342.
            await self._db.execute(
                """
                INSERT INTO session_log
                    (session_id, turn_index, role, content, raw_json, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    turn_index,
                    role,
                    content,
                    raw_json,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )
            await self._db.commit()
        except Exception as exc:
            logger.warning("Session log write failed (turn=%d): %s", turn_index, exc)
            if self._health:
                self._health.record_failure("session_log_write", str(exc))

    async def update_session(
        self,
        session_id: str,
        turn_count: int,
        last_active: str,
        title: str | None,
    ) -> None:
        """Update metadata after each turn or on stop().
        COALESCE(title, ?) preserves the title once it has been set."""
        await self._db.execute(
            """
            UPDATE sessions
            SET turn_count = ?, last_active = ?, title = COALESCE(title, ?)
            WHERE session_id = ?
            """,
            (turn_count, last_active, title, session_id),
        )
        await self._db.commit()

    async def delete_session(self, session_id: str) -> None:
        """Delete session metadata and all message rows."""
        await self._db.execute(
            "DELETE FROM session_log WHERE session_id = ?", (session_id,)
        )
        await self._db.execute(
            "DELETE FROM sessions WHERE session_id = ?", (session_id,)
        )
        await self._db.commit()

    async def update_title(self, session_id: str, title: str) -> None:
        """Overwrite the title of an existing session."""
        await self._db.execute(
            "UPDATE sessions SET title = ? WHERE session_id = ?",
            (title, session_id),
        )
        await self._db.commit()

        await self._db.commit()

    async def fork_session(self, old_session_id: str, new_session_id: str, target_turn_index: int) -> None:
        """
        Duplicate an existing session up to a specific turn_index.
        This enables "Time-Travel", allowing branching off the conversation.
        """
        now = datetime.now(UTC).isoformat()
        parent = await self.get_session(old_session_id)
        if not parent:
            raise ValueError(f"Session {old_session_id} not found")
            
        new_title = f"{parent['title'] or 'Session'} (分岐)"
        
        await self._db.execute(
            """
            INSERT INTO sessions
                (session_id, model, title, started_at, last_active, turn_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_session_id, parent["model"], new_title, now, now, target_turn_index),
        )
        
        await self._db.execute(
            """
            INSERT INTO session_log
                (session_id, turn_index, role, content, raw_json, metadata, created_at)
            SELECT ?, turn_index, role, content, raw_json, metadata, created_at
            FROM session_log
            WHERE session_id = ? AND turn_index <= ?
            """,
            (new_session_id, old_session_id, target_turn_index)
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    async def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return sessions ordered by last_active DESC."""
        cursor = await self._db.execute(
            "SELECT session_id, model, title, started_at, last_active, turn_count "
            "FROM sessions ORDER BY last_active DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "session_id": r[0],
                "model": r[1],
                "title": r[2],
                "started_at": r[3],
                "last_active": r[4],
                "turn_count": r[5],
            }
            for r in rows
        ]

    async def list_sessions_between(
        self,
        since: datetime,
        until: datetime | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return sessions whose ``last_active`` falls in ``[since, until)``."""
        where = ["last_active >= ?"]
        params: list[Any] = [since.isoformat()]
        if until is not None:
            where.append("last_active < ?")
            params.append(until.isoformat())
        params.append(limit)
        cursor = await self._db.execute(
            "SELECT session_id, model, title, started_at, last_active, turn_count "
            f"FROM sessions WHERE {' AND '.join(where)} "
            "ORDER BY last_active DESC LIMIT ?",
            tuple(params),
        )
        rows = await cursor.fetchall()
        return [
            {
                "session_id": r[0],
                "model": r[1],
                "title": r[2],
                "started_at": r[3],
                "last_active": r[4],
                "turn_count": r[5],
            }
            for r in rows
        ]

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Return session metadata for one session, or None if not found."""
        cursor = await self._db.execute(
            "SELECT session_id, model, title, started_at, last_active, turn_count "
            "FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "session_id": row[0],
            "model": row[1],
            "title": row[2],
            "started_at": row[3],
            "last_active": row[4],
            "turn_count": row[5],
        }

    async def messages_between(
        self,
        since: datetime,
        until: datetime | None = None,
        *,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return raw message rows created in ``[since, until)``.

        This intentionally returns rows rather than reconstructed chat
        messages. It is a diary/forensics primitive for time-window recall,
        while ``load_messages`` remains the replay API.
        """
        where = ["created_at >= ?", "role != 'system'"]
        params: list[Any] = [since.isoformat()]
        if until is not None:
            where.append("created_at < ?")
            params.append(until.isoformat())
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        params.append(limit)
        cursor = await self._db.execute(
            "SELECT session_id, turn_index, role, content, raw_json, metadata, created_at "
            f"FROM session_log WHERE {' AND '.join(where)} "
            "ORDER BY created_at ASC, id ASC LIMIT ?",
            tuple(params),
        )
        rows = await cursor.fetchall()
        result: list[dict[str, Any]] = []
        for r in rows:
            try:
                metadata: dict[str, Any] = json.loads(r[5] or "{}")
            except Exception:
                metadata = {}
            result.append({
                "session_id": r[0],
                "turn_index": r[1],
                "role": r[2],
                "content": _redact_secrets(r[3]) or "",
                "raw_json": r[4],
                "metadata": metadata,
                "created_at": r[6],
            })
        return result

    async def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Return all non-system messages in replay order (turn_index ASC, id ASC).

        System messages are excluded — the system prompt is always rebuilt
        fresh from PromptStack + MemoryIndex on resume.

        Reconstruction priority for each row:
        1. ``raw_json`` column (non-NULL) — parsed directly; preserves tool_calls.
        2. ``metadata.format == "raw_message"`` — legacy fallback; ``content`` is
           the full JSON blob (written before the raw_json column existed).
        3. Plain text reconstruction (user / tool messages).

        #335 — secret redaction is applied here, on the way out, rather
        than at write time. Raw text on disk is the ground truth; if
        the redaction regex misses a pattern today, improving it later
        still lets old rows surface redacted on the next load.

        Structured rows (raw_json, legacy raw_message in content) are
        parsed first, then ``_redact_in_place`` walks the dict / list
        and redacts every leaf string. Plain content is redacted via
        ``_redact_secrets`` directly. The leaf-level walk is required
        because some patterns (e.g. ``Bearer\\s+\\S{8,}``) greedy-match
        through escaped quotes if applied to a raw JSON blob, breaking
        syntax.

        SECURITY: this guards the presentation boundary, not at-rest
        storage — see ``log_message`` SECURITY NOTE and #342.
        """
        cursor = await self._db.execute(
            """
            SELECT role, content, raw_json, metadata
            FROM session_log
            WHERE session_id = ? AND role != 'system'
            ORDER BY turn_index ASC, id ASC
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        result: list[dict[str, Any]] = []
        for role, content, raw_json, metadata_raw in rows:
            # #335 review S2 — best-effort metadata parse; load_messages
            # contract is "never raises", so a corrupted metadata blob
            # falls through to empty rather than aborting the whole
            # replay.
            try:
                meta: dict[str, Any] = json.loads(metadata_raw or "{}")
            except Exception as meta_exc:
                logger.debug(
                    "metadata parse failed; using empty: %s", meta_exc,
                )
                meta = {}

            # Priority 1: raw_json column (structured, new path)
            if raw_json:
                try:
                    parsed = json.loads(raw_json)
                    # #335 — redact at the leaf level after parse so
                    # patterns can't accidentally chew through escaped
                    # quotes and break JSON.
                    result.append(_redact_in_place(parsed))
                    continue
                except Exception as parse_exc:
                    logger.debug("raw_json parse failed, falling back: %s", parse_exc)

            # Priority 2: legacy raw_message stored in content column
            if role == "assistant" and meta.get("format") == "raw_message":
                try:
                    msg = json.loads(content)
                    result.append(_redact_in_place(msg))
                    continue
                except Exception as legacy_exc:
                    logger.debug("Legacy raw_message parse failed, falling back: %s", legacy_exc)

            # Priority 3: plain text
            msg: dict[str, Any] = {"role": role, "content": _redact_secrets(content)}
            # Re-attach tool_call_id for tool messages (required by OpenAI format)
            if role == "tool" and "tool_call_id" in meta:
                msg["tool_call_id"] = meta["tool_call_id"]
            # Issue #222: re-attach observation-masking tags so resumed
            # sessions retain turn provenance for _apply_observation_masking
            # and _sanitize_history Pass 4. Underscore prefix keeps them out
            # of provider conversion (see session.py:1925 commentary).
            if role == "tool":
                if "emit_turn" in meta:
                    msg["_emit_turn"] = meta["emit_turn"]
                if "tool_name" in meta:
                    msg["_tool_name"] = meta["tool_name"]
            result.append(msg)
        return result
