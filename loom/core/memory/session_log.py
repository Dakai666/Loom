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
import re
from datetime import datetime, UTC
from typing import Any

import aiosqlite


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
    """Best-effort redaction of common secret patterns.  Never raises."""
    if not text:
        return text
    try:
        for pattern, replacement in _SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
    except Exception:
        pass  # redaction must never crash the agent
    return text


class SessionLog:
    """Read/write access to the sessions + session_log tables."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def create_session(self, session_id: str, model: str) -> None:
        """Insert a new session row.  INSERT OR IGNORE is safe for resume."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """
            INSERT OR IGNORE INTO sessions
                (session_id, model, title, started_at, last_active, turn_count)
            VALUES (?, ?, NULL, ?, ?, 0)
            """,
            (session_id, model, now, now),
        )
        await self._db.commit()

    async def log_message(
        self,
        session_id: str,
        turn_index: int,
        role: str,
        content: str,
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
            # Issue #92: redact secrets before persisting to disk
            content = _redact_secrets(content)
            raw_json = _redact_secrets(raw_json)

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
        except Exception:
            pass  # logging must never crash the agent

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

    async def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Return all non-system messages in replay order (turn_index ASC, id ASC).

        System messages are excluded — the system prompt is always rebuilt
        fresh from PromptStack + MemoryIndex on resume.

        Reconstruction priority for each row:
        1. ``raw_json`` column (non-NULL) — parsed directly; preserves tool_calls.
        2. ``metadata.format == "raw_message"`` — legacy fallback; ``content`` is
           the full JSON blob (written before the raw_json column existed).
        3. Plain text reconstruction (user / tool messages).
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
            meta: dict[str, Any] = json.loads(metadata_raw)

            # Priority 1: raw_json column (structured, new path)
            if raw_json:
                try:
                    result.append(json.loads(raw_json))
                    continue
                except Exception:
                    pass  # fall through to legacy / plain-text path

            # Priority 2: legacy raw_message stored in content column
            if role == "assistant" and meta.get("format") == "raw_message":
                try:
                    msg = json.loads(content)
                    result.append(msg)
                    continue
                except Exception:
                    pass  # fall through to plain text reconstruction

            # Priority 3: plain text
            msg: dict[str, Any] = {"role": role, "content": content}
            # Re-attach tool_call_id for tool messages (required by OpenAI format)
            if role == "tool" and "tool_call_id" in meta:
                msg["tool_call_id"] = meta["tool_call_id"]
            result.append(msg)
        return result
