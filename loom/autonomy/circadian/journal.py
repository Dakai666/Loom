"""
Weave journal — the daily life-log artifact (PR5, issue #463).

Per doc/56 §10 the rule is: semantic memory holds *structural insights*, the
journal holds *the texture of the day*. Without a journal, every life fragment
the agent wants to keep ("10:00 餵了喵吉", "讀了一篇好玩的論文") would either
be lost or pushed into semantic memory and dilute it. With a journal those go
to a dated markdown file instead — agent-readable when curiosity calls for it,
but never recalled by the routine memory pipeline.

Shape (doc/57 §7 PR5):

    autonomy/circadian/journal/YYYY-MM-DD.md
        # 2026-05-28 Journal

        ## 14:30 · 有趣發現
        ⋯

        ## 21:00 · 明日想試
        ⋯

True append at file level (POSIX ``O_APPEND``) — never read-modify-write, so
no mtime guard needed and concurrent calls don't tear (within one
``write()``). Lazy file creation: the H1 header is written on the first call
of the day. Dated paths, not rolling, so yesterday's file freezes the moment
midnight rolls.

Soft phase gating: ``journal_append`` is always callable. The
evening_closure chime body steers the agent toward this tool for
non-structural memories; nothing here enforces phase. Same pattern as
weave_revise (PR4 #462) — the tool is honest about "use me here", the
lifecycle doesn't police it.

Trust = SAFE because the worst case is "a journal file accumulates a wrong
entry" — recoverable by hand-editing the file. No memory pollution
(structurally — this module never talks to MemoryGovernor), no overwrites of
DK's edits (append-only).
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import ToolCapability, TrustLevel
from loom.core.harness.registry import ToolDefinition

logger = logging.getLogger(__name__)

DEFAULT_JOURNAL_DIR = Path("autonomy/circadian/journal")

# kind: stable English enum keys (tool args) → Chinese H2 labels (file output).
# Mirrors the four bullet list in doc/56 §10.3.
#   moment    — 生活片段     a piece of the day's texture
#   finding   — 有趣發現     something curious / interesting noticed today
#   keepsake  — 留念         something worth remembering, but not a structural insight
#   tomorrow  — 明日想試     a soft wish/note for tomorrow (commitment goes via weave_revise)
KIND_LABELS: dict[str, str] = {
    "moment": "生活片段",
    "finding": "有趣發現",
    "keepsake": "留念",
    "tomorrow": "明日想試",
}


def journal_path_for(date_str: str, base: Path | None = None) -> Path:
    """Dated journal file for ``date_str`` (YYYY-MM-DD). One file per day."""
    return (base or DEFAULT_JOURNAL_DIR) / f"{date_str}.md"


def make_journal_append_tool(
    *,
    timezone: str = "Asia/Taipei",
    journal_dir: Path | None = None,
) -> ToolDefinition:
    """Build the ``journal_append`` tool. Factory pattern matches
    ``make_weave_revise_tool`` so tests can redirect IO and so the date stamp
    follows engine timezone rather than wall-clock UTC.
    """
    target_dir = journal_dir or DEFAULT_JOURNAL_DIR

    async def _journal_append(call: ToolCall) -> ToolResult:
        kind = str(call.args.get("kind", "")).strip()
        body = str(call.args.get("body", "")).strip()

        if kind not in KIND_LABELS:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=False,
                error=(
                    f"'kind' must be one of {sorted(KIND_LABELS)}; got {kind!r}. "
                    "moment=生活片段, finding=有趣發現, keepsake=留念, "
                    "tomorrow=明日想試。"
                ),
            )
        if not body:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=False,
                error="'body' is empty — journal entries must have content.",
            )

        now = datetime.now(ZoneInfo(timezone))
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")
        label = KIND_LABELS[kind]
        path = journal_path_for(date_str, target_dir)

        # Build the full payload first so the actual write() is one syscall —
        # POSIX O_APPEND is atomic for writes ≤ PIPE_BUF, and a single string
        # write keeps two concurrent journal_append calls from interleaving
        # mid-entry.
        new_file = not path.exists()
        chunk = ""
        if new_file:
            chunk += f"# {date_str} Journal\n\n"
        chunk += f"## {time_str} · {label}\n{body}\n\n"

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(chunk)
        except OSError as exc:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=False,
                error=f"could not write journal entry to {path}: {exc}",
            )

        return ToolResult(
            call_id=call.id, tool_name=call.tool_name, success=True,
            output=f"journal entry added → {path} [{time_str} · {label}]",
        )

    return ToolDefinition(
        name="journal_append",
        description=(
            "Append a dated entry to today's weave journal "
            "(autonomy/circadian/journal/YYYY-MM-DD.md). Use this for life "
            "fragments, curious findings, keepsakes worth remembering, and "
            "soft 'maybe try tomorrow' notes — anything you want to keep "
            "without polluting semantic memory. Structural insights still "
            "go to memorize. Commitments to actually change tomorrow's plan "
            "go to weave_revise. One file per day, append-only. Always "
            "callable; especially natural during evening_closure."
        ),
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.MUTATES,
        input_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": sorted(KIND_LABELS),
                    "description": (
                        "moment: a piece of today's texture. "
                        "finding: something curious noticed today. "
                        "keepsake: worth remembering, but not a structural "
                        "insight (those go to memorize). "
                        "tomorrow: a soft note about what to try tomorrow "
                        "(commitments go via weave_revise instead)."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Markdown body for the entry — bullets, blockquotes, "
                        "nested headings all preserved verbatim."
                    ),
                },
            },
            "required": ["kind", "body"],
        },
        executor=_journal_append,
        tags=["circadian", "journal", "write"],
        impact_scope="filesystem",
    )
