"""
Weave revision — agent edits tomorrow's daily_weave.md, with audit trail.

PR4 #462. DK's design override (2026-05-28): the original spec had a
proposal → confirm → apply flow; DK reversed it — "我不想 confirm, 只需要
跟我報告就". So this module collapses propose-and-apply into one atomic
tool action:

    weave_revise(rationale, changes)
        ↓ snapshot daily_weave.md (mtime + prelude + sections)
        ↓ write proposal artifact (TOML, audit trail)
        ↓ apply changes
        ↓ atomic rewrite daily_weave.md
        ↓ archive proposal to applied/
        ↓ tomorrow's dawn chime reports "你昨夜改了什麼"

The proposal artifact stays as a full audit trail — DK can ``git diff`` or
revert from ``proposals/applied/`` if a revise was wrong. The mtime guard
keeps DK's hand-edits to ``daily_weave.md`` from being silently overwritten:
if mtime moved between snapshot and apply, the proposal goes to
``conflicts/`` instead and the file is left alone.

TOML over markdown-diff (DK preference + PR3 fence-bug lesson, see
[[feedback_prefer_structured_over_parser]]): no second parser to write, no
second parser to bug-fix later.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from loom.autonomy.circadian.weave import (
    DEFAULT_WEAVE_PATH,
    load_weave_for_revision,
)
from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import ToolCapability, TrustLevel
from loom.core.harness.registry import ToolDefinition

logger = logging.getLogger(__name__)

PROPOSALS_DIR = Path("autonomy/circadian/proposals")
APPLIED_SUBDIR = "applied"
CONFLICTS_SUBDIR = "conflicts"

VALID_ACTIONS = {"add", "remove", "rename", "replace"}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Change:
    """One section-level edit. ``action`` decides which of ``to`` / ``new_body``
    is consulted; validate() enforces the matrix so a malformed change is
    rejected up-front, not mid-apply with a half-written file."""

    section: str
    action: str
    to: str | None = None
    new_body: str | None = None

    def validate(self) -> str | None:
        if self.action not in VALID_ACTIONS:
            return f"invalid action {self.action!r}; must be one of {sorted(VALID_ACTIONS)}"
        if not self.section.strip():
            return "section name is empty"
        if self.action == "rename":
            if not self.to or not self.to.strip():
                return "rename requires non-empty 'to'"
        if self.action in {"add", "replace"} and self.new_body is None:
            return f"{self.action} requires 'new_body'"
        return None


@dataclass
class WeaveProposal:
    """One evening_closure's batch of edits, with the metadata needed to (a)
    detect a hand-edit conflict at apply time and (b) reconstruct the day
    for DK at dawn-of-tomorrow."""

    date: str                 # the day evening_closure fired (audit timestamp)
    phase: str                # "evening_closure" — fixed for now
    based_on_mtime: float     # daily_weave.md mtime at snapshot
    rationale: str
    changes: list[Change] = field(default_factory=list)

    def to_toml_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "phase": self.phase,
            "based_on_mtime": self.based_on_mtime,
            "rationale": self.rationale,
            "changes": [
                {k: v for k, v in asdict(c).items() if v is not None}
                for c in self.changes
            ],
        }

    @classmethod
    def from_toml_dict(cls, raw: dict[str, Any]) -> "WeaveProposal":
        changes = [
            Change(
                section=str(c["section"]),
                action=str(c["action"]),
                to=c.get("to"),
                new_body=c.get("new_body"),
            )
            for c in raw.get("changes", [])
        ]
        return cls(
            date=str(raw["date"]),
            phase=str(raw["phase"]),
            based_on_mtime=float(raw["based_on_mtime"]),
            rationale=str(raw.get("rationale", "")),
            changes=changes,
        )

    def summary_lines(self) -> list[str]:
        """Human-readable one-liner per change, for the dawn chime report."""
        out: list[str] = []
        for c in self.changes:
            if c.action == "add":
                out.append(f"+ {c.section} (新增)")
            elif c.action == "remove":
                out.append(f"- {c.section} (刪除)")
            elif c.action == "rename":
                out.append(f"~ {c.section} → {c.to} (改名)")
            elif c.action == "replace":
                out.append(f"~ {c.section} (內容換)")
        return out


# ---------------------------------------------------------------------------
# Pure transformations
# ---------------------------------------------------------------------------

def apply_changes(
    sections: dict[str, str], changes: list[Change]
) -> tuple[dict[str, str], str | None]:
    """Mutate-by-copy: returns ``(new_sections, error_or_None)``.

    On any error (unknown section, name collision) returns the unchanged
    dict + an error string. All-or-nothing semantics — the caller writes
    the new sections only when error is None.

    Insertion order: existing sections keep their position; ``add`` appends;
    ``rename`` keeps the original slot (Python dict preserves insertion
    order, so we rebuild to swap in-place).
    """
    items = list(sections.items())
    by_name = dict(items)

    for change in changes:
        err = change.validate()
        if err:
            return sections, f"invalid change {change.section!r}: {err}"

        if change.action == "add":
            if change.section in by_name:
                return sections, (
                    f"add: section {change.section!r} already exists "
                    "(use 'replace' to overwrite or 'rename')"
                )
            items.append((change.section, change.new_body or ""))
            by_name[change.section] = change.new_body or ""

        elif change.action == "remove":
            if change.section not in by_name:
                return sections, f"remove: section {change.section!r} not found"
            items = [(k, v) for k, v in items if k != change.section]
            del by_name[change.section]

        elif change.action == "replace":
            if change.section not in by_name:
                return sections, f"replace: section {change.section!r} not found"
            items = [
                (k, change.new_body if k == change.section else v)
                for k, v in items
            ]
            by_name[change.section] = change.new_body or ""

        elif change.action == "rename":
            if change.section not in by_name:
                return sections, f"rename: section {change.section!r} not found"
            if change.to in by_name and change.to != change.section:
                return sections, (
                    f"rename: target {change.to!r} already exists"
                )
            new_body = change.new_body if change.new_body is not None else by_name[change.section]
            items = [
                (change.to, new_body) if k == change.section else (k, v)
                for k, v in items
            ]
            del by_name[change.section]
            by_name[change.to] = new_body

    return dict(items), None


def render_weave_markdown(prelude: str, sections: dict[str, str]) -> str:
    """Rebuild daily_weave.md text from prelude + sections.

    Prelude is preserved verbatim (so DK's free-form header survives a tool
    rewrite). Each section becomes ``## name\\n{body}\\n`` with one blank
    line between sections for readability. Empty body still emits the
    heading so a placeholder section stays visible.
    """
    parts: list[str] = []
    if prelude:
        parts.append(prelude.rstrip())
        parts.append("")
    for name, body in sections.items():
        parts.append(f"## {name}")
        if body.strip():
            parts.append(body.rstrip())
        parts.append("")
    text = "\n".join(parts).rstrip() + "\n"
    return text


# ---------------------------------------------------------------------------
# Disk IO
# ---------------------------------------------------------------------------

def proposal_path(date_str: str, base_dir: Path | None = None) -> Path:
    return (base_dir or PROPOSALS_DIR) / f"{date_str}-evening.toml"


def _save_proposal_toml(proposal: WeaveProposal, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # tomllib is read-only in stdlib; we hand-write a minimal serializer to
    # keep the dep surface (and the artifact format) under our control.
    body = _render_proposal_toml(proposal)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".prop-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _render_proposal_toml(proposal: WeaveProposal) -> str:
    """Minimal TOML writer for WeaveProposal. JSON-encodes strings so embedded
    quotes / newlines round-trip safely through tomllib on read."""
    lines: list[str] = [
        f"date = {json.dumps(proposal.date)}",
        f"phase = {json.dumps(proposal.phase)}",
        f"based_on_mtime = {proposal.based_on_mtime!r}",
        f"rationale = {json.dumps(proposal.rationale)}",
        "",
    ]
    for c in proposal.changes:
        lines.append("[[changes]]")
        lines.append(f"section = {json.dumps(c.section)}")
        lines.append(f"action = {json.dumps(c.action)}")
        if c.to is not None:
            lines.append(f"to = {json.dumps(c.to)}")
        if c.new_body is not None:
            lines.append(f"new_body = {json.dumps(c.new_body)}")
        lines.append("")
    return "\n".join(lines)


def load_proposal(path: Path) -> WeaveProposal | None:
    """Read a proposal TOML; ``None`` when missing or malformed."""
    if not path.exists():
        return None
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        return WeaveProposal.from_toml_dict(raw)
    except (OSError, tomllib.TOMLDecodeError, KeyError, ValueError) as exc:
        logger.warning("[circadian] proposal at %s unreadable (%s)", path, exc)
        return None


def _atomic_write_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".wv-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _archive_proposal(src: Path, subdir: str) -> Path:
    dest_dir = src.parent / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    os.replace(src, dest)
    return dest


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

def make_weave_revise_tool(
    *,
    timezone: str = "Asia/Taipei",
    weave_path: Path | None = None,
    proposals_dir: Path | None = None,
) -> ToolDefinition:
    """Build the ``weave_revise`` tool. Trust = SAFE because DK explicitly
    designed this to run unattended (no confirm, "just report"); the safety
    is structural — mtime guard + audit trail + DK hand-edit always wins.

    Parameters are factory-time dependencies so tests can redirect IO and
    so the timezone for the date stamp follows the engine config rather
    than wall-clock UTC.
    """
    target_weave = weave_path or DEFAULT_WEAVE_PATH
    target_proposals = proposals_dir or PROPOSALS_DIR

    async def _weave_revise(call: ToolCall) -> ToolResult:
        rationale = str(call.args.get("rationale", "")).strip()
        raw_changes = call.args.get("changes") or []

        if not rationale:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=False,
                error="'rationale' is required — DK reads this at dawn to "
                      "understand why the weave changed.",
            )
        if not isinstance(raw_changes, list) or not raw_changes:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=False,
                error="'changes' must be a non-empty list.",
            )

        try:
            changes = [
                Change(
                    section=str(c["section"]),
                    action=str(c["action"]),
                    to=c.get("to"),
                    new_body=c.get("new_body"),
                )
                for c in raw_changes
            ]
        except (KeyError, TypeError) as exc:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=False,
                error=f"change spec malformed: {exc}",
            )

        snapshot = load_weave_for_revision(target_weave)
        if snapshot is None:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=False,
                error=f"daily weave at {target_weave} not found — "
                      "create it (or copy daily_weave.example.md) before revising.",
            )
        prelude, sections, mtime_before = snapshot

        date_str = datetime.now(ZoneInfo(timezone)).strftime("%Y-%m-%d")
        proposal = WeaveProposal(
            date=date_str,
            phase="evening_closure",
            based_on_mtime=mtime_before,
            rationale=rationale,
            changes=changes,
        )

        new_sections, err = apply_changes(sections, changes)
        if err is not None:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=False,
                error=f"changes rejected: {err}",
            )

        # Always persist the proposal artifact first — even if apply fails
        # downstream, DK can see what was attempted.
        artifact = proposal_path(date_str, target_proposals)
        try:
            _save_proposal_toml(proposal, artifact)
        except OSError as exc:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=False,
                error=f"could not write proposal artifact: {exc}",
            )

        # Mtime conflict guard: if daily_weave.md moved between snapshot
        # and write, DK (or another tool) hand-edited it. DK's edit wins;
        # park the proposal under conflicts/ so DK sees it at dawn.
        try:
            mtime_now = target_weave.stat().st_mtime
        except OSError:
            mtime_now = mtime_before  # treat unstattable as no-conflict
        if mtime_now != mtime_before:
            conflict_path = _archive_proposal(artifact, CONFLICTS_SUBDIR)
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=False,
                error=(
                    f"daily_weave.md changed under us "
                    f"(mtime {mtime_before} → {mtime_now}); proposal parked at "
                    f"{conflict_path} for DK review. Hand-edit takes priority."
                ),
                output=str(conflict_path),
            )

        new_text = render_weave_markdown(prelude, new_sections)
        try:
            _atomic_write_text(target_weave, new_text)
        except OSError as exc:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=False,
                error=f"could not write daily_weave.md: {exc}",
            )

        applied = _archive_proposal(artifact, APPLIED_SUBDIR)
        summary = " | ".join(proposal.summary_lines()) or "(no visible changes)"
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name, success=True,
            output=(
                f"daily_weave.md revised ({len(changes)} change(s)): {summary}\n"
                f"audit: {applied}\n"
                f"rationale: {rationale}"
            ),
        )

    return ToolDefinition(
        name="weave_revise",
        description=(
            "Atomically revise tomorrow's daily_weave.md (add / remove / "
            "rename / replace H2 sections) and write an audit-trail TOML "
            "proposal under autonomy/circadian/proposals/applied/. "
            "Call this in the evening_closure phase when you want to adjust "
            "the next day's plan. DK does not confirm — your rationale is "
            "what reaches them at next dawn. If daily_weave.md changed "
            "between read and write (DK hand-edit), the proposal is parked "
            "under proposals/conflicts/ and the file is left untouched."
        ),
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.MUTATES,
        input_schema={
            "type": "object",
            "properties": {
                "rationale": {
                    "type": "string",
                    "description": "Why these edits — DK reads this at dawn.",
                },
                "changes": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "section": {
                                "type": "string",
                                "description": "H2 phase name to act on.",
                            },
                            "action": {
                                "type": "string",
                                "enum": sorted(VALID_ACTIONS),
                                "description": (
                                    "add: insert new H2 with new_body. "
                                    "remove: drop H2 + body. "
                                    "rename: change H2 name (to), optionally body. "
                                    "replace: keep H2 name, swap body."
                                ),
                            },
                            "to": {
                                "type": "string",
                                "description": "Rename target (required for rename).",
                            },
                            "new_body": {
                                "type": "string",
                                "description": "Section body markdown "
                                               "(required for add / replace; optional for rename).",
                            },
                        },
                        "required": ["section", "action"],
                    },
                },
            },
            "required": ["rationale", "changes"],
        },
        executor=_weave_revise,
        tags=["circadian", "weave", "write"],
        impact_scope="filesystem",
    )
