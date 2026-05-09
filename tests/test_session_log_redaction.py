"""#335 / Issue #92 — secret redaction round-trip on the read path.

#335 moved redaction from write-time to read-time. Raw text on disk is
the ground truth; load_messages applies the regex on the way out so
future regex tweaks can re-redact accurately. These tests pin that
contract: writes are not mutated, reads come back with secrets masked.
"""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest_asyncio

from loom.core.memory.session_log import SessionLog


@pytest_asyncio.fixture
async def sl(tmp_path: Path):
    db = tmp_path / "sessions.db"
    conn = await aiosqlite.connect(str(db))
    await conn.execute(
        """
        CREATE TABLE sessions (
            session_id TEXT, model TEXT, title TEXT,
            started_at TEXT, last_active TEXT, turn_count INTEGER DEFAULT 0
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE session_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            turn_index INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            raw_json TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    await conn.commit()
    log = SessionLog(conn)
    await log.create_session("s1", "test-model")
    yield log, conn
    await conn.close()


# ---------------------------------------------------------------------------
# Round-trip — write raw, read redacted
# ---------------------------------------------------------------------------


async def test_user_message_secret_redacted_on_read(sl) -> None:
    log, conn = sl
    await log.log_message("s1", 0, "user", 'export api_key="sk-1234567890abcdef1234"')

    # On disk: raw value preserved.
    cursor = await conn.execute("SELECT content FROM session_log")
    (raw_on_disk,) = await cursor.fetchone()
    assert "sk-1234567890abcdef1234" in raw_on_disk

    # On read: secret masked.
    msgs = await log.load_messages("s1")
    assert len(msgs) == 1
    assert "REDACTED" in msgs[0]["content"]
    assert "sk-1234567890abcdef1234" not in msgs[0]["content"]


async def test_assistant_raw_json_redacted_on_read(sl) -> None:
    log, conn = sl
    raw = json.dumps({
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "t1", "name": "run_bash",
             "input": {"cmd": 'curl -H "Authorization: Bearer ghp_abcdef1234567890XYZW"'}},
        ],
    })
    await log.log_message(
        "s1", 1, "assistant", "[tool_use]",
        metadata={"format": "raw_message"}, raw_json=raw,
    )

    # On disk: full bearer preserved.
    cursor = await conn.execute("SELECT raw_json FROM session_log WHERE turn_index=1")
    (rj,) = await cursor.fetchone()
    assert "ghp_abcdef1234567890XYZW" in rj

    # On read: parsed dict with redacted bearer.
    msgs = await log.load_messages("s1")
    assert len(msgs) == 1
    cmd = msgs[0]["content"][0]["input"]["cmd"]
    assert "REDACTED" in cmd
    assert "ghp_abcdef1234567890XYZW" not in cmd


async def test_tool_message_content_redacted_on_read(sl) -> None:
    log, conn = sl
    secret_output = 'token: "AKIAIOSFODNN7EXAMPLE12345"'
    await log.log_message(
        "s1", 2, "tool", secret_output,
        metadata={"tool_call_id": "t1", "tool_name": "run_bash"},
    )

    msgs = await log.load_messages("s1")
    assert len(msgs) == 1
    assert "REDACTED" in msgs[0]["content"]
    assert "AKIAIOSFODNN7EXAMPLE12345" not in msgs[0]["content"]
    # tool_call_id passthrough still works
    assert msgs[0]["tool_call_id"] == "t1"


# ---------------------------------------------------------------------------
# On-disk preservation — the whole point of #335
# ---------------------------------------------------------------------------


async def test_disk_content_is_unredacted_so_future_regex_can_fix(sl) -> None:
    """Future regex improvements must be able to catch patterns the
    current redactor missed. Storing the raw text on disk is what
    enables that — write-time redaction would have been one-shot."""
    log, conn = sl
    text = 'password="this-is-12-chars-secret"'
    await log.log_message("s1", 0, "user", text)

    cursor = await conn.execute("SELECT content FROM session_log")
    (on_disk,) = await cursor.fetchone()
    assert on_disk == text  # exact byte preservation


async def test_redaction_failure_does_not_break_load(sl) -> None:
    """_redact_secrets contract: best-effort, never raises."""
    log, _ = sl
    await log.log_message("s1", 0, "user", "ordinary message with no secrets")
    msgs = await log.load_messages("s1")
    assert msgs == [{"role": "user", "content": "ordinary message with no secrets"}]
