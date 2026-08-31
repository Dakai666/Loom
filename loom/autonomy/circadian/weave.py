"""
Daily weave plan — today's specific items per phase (PR3, issue #461).

The rhythm table from PR2 ([[rhythm.py]]) answers *what phases exist and why*
— stable, edited rarely. The weave plan answers *what does this phase look
like today* — content, can change daily. doc/56 §6: "Daily Weave 是
Circadian Autonomy 的日程內容層 … 類似遊戲『美少女夢工廠』的 time block".

Composition at chime time (lifecycle._deliver_phase_chime):
    intent = anchor.meaning   ← rhythm.toml, the *why*
           + weave.section_for(anchor.name)   ← daily_weave.md, today's *what*

Phase name is the bridge: `anchor.name` must match a markdown H2 heading
verbatim. doc/57 §8 Q2: rolling file (not dated) — the agent rewrites it
nightly via the proposal tool (#462 PR4); for now any caller (user or
agent) just edits the file in place.

Tolerant by contract: missing file, unreadable bytes, no H2 sections, all
return an empty WeavePlan so phase chimes keep firing with `meaning` alone.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_WEAVE_PATH = Path("autonomy/circadian/daily_weave.md")

# ATX H2: `## heading text` (two hashes, at least one space, then text).
# We intentionally do *not* support Setext (===/---) headings — the example
# template and proposal tool both write ATX, and Setext would muddy the
# regex without buying anything.
_H2_RE = re.compile(r"^##\s+(.+?)\s*$")


@dataclass(frozen=True)
class WeavePlan:
    """Parsed daily weave: phase name → markdown body for that section.

    ``section_for`` is the only consumer-facing method; everything else is
    internal to the parser. Empty plan (missing file / no H2s) returns
    ``None`` for every lookup so the composition site can do a simple
    truthy-check.
    """

    sections: dict[str, str] = field(default_factory=dict)

    def section_for(self, phase_name: str) -> str | None:
        body = self.sections.get(phase_name)
        return body if body else None


def load_weave_for_revision(
    path: Path | None = None,
) -> tuple[str, dict[str, str], int] | None:
    """Read the weave file for the revise tool: prelude + sections + mtime_ns.

    The revise tool (PR4 #462) needs three things load_weave() doesn't give:

    - **Prelude**: everything before the first H2 (typical: ``# 今日織程`` +
      ``date: ...`` + free-form blurb). Preserved verbatim so DK's hand-edits
      to the header survive a tool-driven rewrite.
    - **mtime_ns**: stamped onto the proposal artifact so apply-time can
      detect a concurrent hand-edit (mtime conflict guard). Nanosecond
      precision so two writes within the same wall-second still differ.
    - **Raw sections** as a dict (not the read-only ``WeavePlan`` wrapper)
      so the apply path can mutate it directly per the proposal's changes.

    Stable-snapshot contract (PR #481 review P1): stat-before, read,
    stat-after; if mtime drifted during the read we treat the whole
    snapshot as torn and return None. Without this guard, a hand-edit
    landing between ``read_text()`` and ``stat()`` would yield (stale
    content, fresh mtime) — the later guard would then pass and the
    tool would silently overwrite DK's edit.

    Returns ``None`` when the file doesn't exist, is unreadable, or
    changed mid-snapshot. Fence-aware H2 boundary detection (same lesson
    as PR3 review).
    """
    p = path or DEFAULT_WEAVE_PATH
    if not p.exists():
        return None
    try:
        mtime_before = p.stat().st_mtime_ns
        text = p.read_text(encoding="utf-8")
        mtime_after = p.stat().st_mtime_ns
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("[circadian] daily weave at %s unreadable (%s)", p, exc)
        return None
    if mtime_before != mtime_after:
        logger.warning(
            "[circadian] daily weave at %s changed during snapshot (mtime_ns %d → %d); "
            "treating as torn read", p, mtime_before, mtime_after,
        )
        return None
    mtime = mtime_after

    prelude_lines: list[str] = []
    rest_lines: list[str] = []
    in_fence = False
    in_prelude = True
    for line in text.splitlines():
        stripped = line.lstrip()
        is_fence = stripped.startswith("```") or stripped.startswith("~~~")
        if is_fence:
            in_fence = not in_fence
            (prelude_lines if in_prelude else rest_lines).append(line)
            continue
        if in_prelude and not in_fence and _H2_RE.match(line):
            in_prelude = False
        (prelude_lines if in_prelude else rest_lines).append(line)

    prelude = "\n".join(prelude_lines)
    if prelude and not prelude.endswith("\n"):
        prelude += "\n"
    sections = _parse_h2_sections("\n".join(rest_lines))
    return prelude, sections, mtime


def load_weave(path: Path | None = None) -> WeavePlan:
    """Read and parse the rolling daily weave file.

    Returns an empty ``WeavePlan`` for every failure mode the engine must
    survive: file absent, OS read error, decode error, no H2s. The lifecycle
    chime composer treats empty-plan the same as missing-section so the
    chime body falls through to ``anchor.meaning`` alone.
    """
    p = path or DEFAULT_WEAVE_PATH
    if not p.exists():
        logger.info(
            "[circadian] daily weave at %s not found — chimes use rhythm meaning only",
            p,
        )
        return WeavePlan()
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("[circadian] daily weave at %s unreadable (%s)", p, exc)
        return WeavePlan()
    return WeavePlan(sections=_parse_h2_sections(text))


# How many names each side of a broken join we quote before eliding. The
# warning has to fit inside a chime body next to the real content, so it
# names enough to be actionable and stops there.
_DIAGNOSE_SAMPLE = 5


def diagnose_join(plan: WeavePlan, anchor_names: Iterable[str]) -> str | None:
    """Report a *dead join key* between the weave file and the rhythm table.

    ``section_for`` looks sections up by ``anchor.name``, and a phase with no
    section is a supported, everyday state — the chime just falls through to
    ``meaning``. That tolerance is right, but it also means a file whose
    headings stopped being anchor names at all degrades into permanent
    silence with no distinguishable signal. Issue #565: the layout was
    reworked to program-shaped headings on 2026-07-16 and every chime lost
    its 今日織程 layer for six weeks; the only visible symptom was absence,
    which the agent then misread as a *write* failure and filed as such.

    A non-empty file whose section names overlap the anchor names *not at
    all* is the one unambiguous signature of that break — partial overlap is
    ordinary and must stay silent, or the warning becomes noise and gets
    tuned out. Returns the warning text, or ``None`` when the join is fine.
    """
    anchors = [str(a) for a in anchor_names]
    if not plan.sections or not anchors:
        return None
    if set(plan.sections) & set(anchors):
        return None

    def _sample(names: list[str]) -> str:
        shown = ", ".join(names[:_DIAGNOSE_SAMPLE])
        extra = len(names) - _DIAGNOSE_SAMPLE
        return f"{shown}…（另 {extra} 個）" if extra > 0 else shown

    return (
        f"daily_weave.md 有 {len(plan.sections)} 個 H2 section"
        f"（{_sample(list(plan.sections))}），"
        f"但沒有一個對得上 rhythm.toml 的 anchor name"
        f"（{_sample(anchors)}）。\n"
        "H2 標題就是兩邊的 join key —— 對不上時每個 phase chime 只會拿到 "
        "rhythm 的 meaning，讀不到今日織程。這是讀取端對不上，不是寫入失敗："
        "weave_revise 每條失敗路徑都會回明確錯誤。\n"
        "修法：把 daily_weave.md 的 H2 改成 anchor name（或用 weave_revise "
        "的 rename action）。"
    )


def _parse_h2_sections(text: str) -> dict[str, str]:
    """Walk the markdown line-by-line, slicing it on H2 boundaries.

    Body is every line between this H2 and the next (or EOF), with leading
    and trailing whitespace stripped but inner content preserved verbatim —
    bullets, code fences, nested headings, blank lines all survive. The
    agent reads the chime body as markdown so faithful preservation matters.

    Fenced-code-block awareness (PR #480 review P2): a line like
    ``## not_a_phase`` *inside* a ``` ``` `` or ``~~~`` fence is content, not
    a section boundary. Without this guard a weave section that shows a
    markdown example would lose every line after the fake heading. The
    fence delimiter lines themselves remain part of the body.

    Duplicate H2 names: last one wins with a warning. The agent's nightly
    weave proposal could in theory produce a duplicate, but the proposal
    flow is supposed to overwrite a section in place; a duplicate is a bug
    upstream and the user should see it in logs.
    """
    sections: dict[str, str] = {}
    current_name: str | None = None
    current_body: list[str] = []
    in_fence = False

    def _flush() -> None:
        nonlocal current_name
        if current_name is None:
            return
        body = "\n".join(current_body).strip()
        if current_name in sections:
            logger.warning(
                "[circadian] daily weave: H2 '%s' duplicated; keeping latest",
                current_name,
            )
        sections[current_name] = body

    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            if current_name is not None:
                current_body.append(line)
            continue
        if not in_fence:
            m = _H2_RE.match(line)
            if m:
                _flush()
                current_name = m.group(1).strip()
                current_body.clear()
                continue
        if current_name is not None:
            current_body.append(line)
    _flush()
    return sections
