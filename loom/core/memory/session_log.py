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
from datetime import datetime, UTC
from typing import Any

import aiosqlite


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
    ) -> None:
        """Append one message row.  Swallows all exceptions — never blocks the loop."""
        try:
            await self._db.execute(
                """
                INSERT INTO session_log
                    (session_id, turn_index, role, content, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    turn_index,
                    role,
                    content,
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
                (session_id, turn_index, role, content, metadata, created_at)
            SELECT ?, turn_index, role, content, metadata, created_at
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
        """
        cursor = await self._db.execute(
            """
            SELECT role, content, metadata
            FROM session_log
            WHERE session_id = ? AND role != 'system'
            ORDER BY turn_index ASC, id ASC
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        result: list[dict[str, Any]] = []
        for role, content, metadata_raw in rows:
            meta: dict[str, Any] = json.loads(metadata_raw)

            if role == "assistant" and meta.get("format") == "raw_message":
                # Full raw_message was stored as JSON — parse it back directly so
                # tool_calls, content lists, etc. are all intact for the API.
                try:
                    msg = json.loads(content)
                    result.append(msg)
                    continue
                except Exception:
                    pass  # fall through to plain text reconstruction

            msg: dict[str, Any] = {"role": role, "content": content}
            # Re-attach tool_call_id for tool messages (required by OpenAI format)
            if role == "tool" and "tool_call_id" in meta:
                msg["tool_call_id"] = meta["tool_call_id"]
            result.append(msg)
        return result
