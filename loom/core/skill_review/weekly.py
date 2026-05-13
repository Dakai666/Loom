"""Weekly skill review worker (doc/54 §4.2, §5 P0-6).

Pure SQL + template — no LLM in this layer. Scans the ledger for the
preceding window, queries per-skill digests via :mod:`skill_review.query`,
applies the structural "attention list" criteria from doc/54 §4.4, and
renders a markdown report to ``outputs/self_check/``.

This is what the user reads when asking "回顧一下這週技能用得怎麼樣". It
does **not** score skills — it surfaces structural observations and lets
the user decide where to put attention.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from loom.core.skill_review.query import SkillUsageDigest, query_skill_ledger

if TYPE_CHECKING:
    from loom.core.ledger.store import LedgerStore


_WEEK_SECONDS = 7 * 24 * 3600

# doc/54 §8 Q2: thresholds are placeholders; recalibrate after the first
# few weekly reports surface real distributions.
_MUFFLED_LOAD_THRESHOLD = 3            # loaded ≥N times with zero feedback
_UNDIGESTED_FEEDBACK_THRESHOLD = 1     # ≥N feedback events but SKILL.md not touched since
_ABNORMAL_OUTCOME_RATIO = 0.30         # (abandoned + error) / outcome_known > X
_STALE_DAYS = 30                       # no load in past N days = "久未使用"


# Distinct reason codes that may appear on a skill's "attention" entry.
# Stable identifiers so future tooling can filter / aggregate.
ATTENTION_MUFFLED = "muffled_run"            # 悶頭跑
ATTENTION_UNDIGESTED = "undigested_feedback"  # 反饋未消化
ATTENTION_ABNORMAL = "abnormal_outcome"      # 異常結尾
ATTENTION_STALE = "stale"                    # 久未使用
ATTENTION_UNUSED = "exists_but_unused"       # 存在但未啟動


@dataclass(frozen=True)
class SkillAttention:
    skill_id: str
    reasons: list[str]                 # ATTENTION_* codes
    details: dict[str, str]            # human-readable detail per reason


@dataclass(frozen=True)
class WeeklyReport:
    window_start_ts: float
    window_end_ts: float
    skills_seen: list[str]             # appeared in ledger
    skills_on_disk: list[str]          # exist as directories under skills_root
    digests: dict[str, SkillUsageDigest]
    attention: list[SkillAttention]
    markdown: str
    output_path: Path | None = None    # set when written to disk


def _outcome_counts(digest: SkillUsageDigest) -> tuple[int, int]:
    """Return (abnormal, total_known) outcome counts from episodes."""
    abnormal = 0
    total_known = 0
    for ep in digest.episodes:
        if ep.turn_outcome is None:
            continue
        total_known += 1
        if ep.turn_outcome in ("abandoned", "error"):
            abnormal += 1
    return abnormal, total_known


def _feedback_count(digest: SkillUsageDigest) -> int:
    """Total memory_op:write events across all episodes — feedback density signal."""
    n = 0
    for ep in digest.episodes:
        for e in ep.events_after_load:
            if e.event_type == "memory_op" and e.payload.get("operation") == "write":
                n += 1
    return n


def _last_load_ts(digest: SkillUsageDigest) -> float | None:
    if not digest.episodes:
        return None
    return max(ep.loaded_at for ep in digest.episodes)


def _skill_md_mtime(skill_dir: Path) -> float | None:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        return skill_md.stat().st_mtime
    except OSError:
        return None


def _scan_filesystem_skills(skills_roots: list[Path]) -> dict[str, Path]:
    """Map skill_id → its directory across all roots."""
    found: dict[str, Path] = {}
    for root in skills_roots:
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name.startswith("."):
                continue
            if not (entry / "SKILL.md").is_file():
                continue
            # First-wins: earlier roots take precedence.
            found.setdefault(entry.name, entry)
    return found


def compute_attention(
    digest: SkillUsageDigest,
    skill_dir: Path | None,
    *,
    now_ts: float,
) -> SkillAttention | None:
    """Apply doc/54 §4.4 structural rules. Returns None if no rule fires."""
    reasons: list[str] = []
    details: dict[str, str] = {}

    fb_count = _feedback_count(digest)
    last_load = _last_load_ts(digest)

    # 悶頭跑: 載入次數高且零反饋
    if digest.load_count >= _MUFFLED_LOAD_THRESHOLD and fb_count == 0:
        reasons.append(ATTENTION_MUFFLED)
        details[ATTENTION_MUFFLED] = (
            f"{digest.load_count} loads with no user feedback in window"
        )

    # 反饋未消化: 有反饋但 SKILL.md 自首筆反饋後未變更
    if fb_count >= _UNDIGESTED_FEEDBACK_THRESHOLD and skill_dir is not None:
        mtime = _skill_md_mtime(skill_dir)
        first_fb_ts: float | None = None
        for ep in digest.episodes:
            for e in ep.events_after_load:
                if e.event_type == "memory_op" and e.payload.get("operation") == "write":
                    if first_fb_ts is None or e.timestamp < first_fb_ts:
                        first_fb_ts = e.timestamp
        if mtime is not None and first_fb_ts is not None and mtime < first_fb_ts:
            reasons.append(ATTENTION_UNDIGESTED)
            details[ATTENTION_UNDIGESTED] = (
                f"{fb_count} feedback events, but SKILL.md untouched since "
                f"{datetime.fromtimestamp(first_fb_ts, tz=timezone.utc):%Y-%m-%d}"
            )

    # 異常結尾
    abn, total_known = _outcome_counts(digest)
    if total_known > 0 and abn / total_known > _ABNORMAL_OUTCOME_RATIO:
        reasons.append(ATTENTION_ABNORMAL)
        details[ATTENTION_ABNORMAL] = (
            f"{abn}/{total_known} turns ended abandoned/error "
            f"({abn / total_known:.0%})"
        )

    # 久未使用: 在硬碟存在但 STALE_DAYS 內沒被載入
    if skill_dir is not None and last_load is not None:
        days_since = (now_ts - last_load) / 86400.0
        if days_since > _STALE_DAYS:
            reasons.append(ATTENTION_STALE)
            details[ATTENTION_STALE] = f"last loaded {days_since:.0f} days ago"

    if not reasons:
        return None
    return SkillAttention(
        skill_id=digest.skill_id, reasons=reasons, details=details,
    )


async def _digest_all_skills(
    ledger: "LedgerStore",
    skill_ids: list[str],
    *,
    since_ts: float,
    until_ts: float,
) -> dict[str, SkillUsageDigest]:
    digests: dict[str, SkillUsageDigest] = {}
    for sid in skill_ids:
        digests[sid] = await query_skill_ledger(
            ledger, sid, since_ts=since_ts, until_ts=until_ts,
        )
    return digests


async def _distinct_skills_in_window(
    ledger: "LedgerStore", *, since_ts: float, until_ts: float,
) -> list[str]:
    """Find every skill_id that has a load/unload event in window."""
    events = (
        await ledger.events
        .where(event_type="tool_lifecycle")
        .since(since_ts).until(until_ts)
        .all()
    )
    seen: set[str] = set()
    for e in events:
        sid = e.payload.get("skill_id")
        if isinstance(sid, str) and sid:
            seen.add(sid)
    return sorted(seen)


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def render_weekly_markdown(report: WeeklyReport) -> str:
    lines = [
        f"# Skill Weekly Review",
        f"",
        f"- Window: {_fmt_ts(report.window_start_ts)} ~ {_fmt_ts(report.window_end_ts)}",
        f"- Skills seen in ledger: {len(report.skills_seen)}",
        f"- Skills on disk: {len(report.skills_on_disk)}",
        f"- Attention items: {len(report.attention)}",
        f"",
        f"---",
        f"",
        f"## 該關注清單",
        f"",
    ]
    if not report.attention:
        lines.append("(no skills triggered structural attention rules this week)")
    else:
        for item in report.attention:
            lines.append(f"### {item.skill_id}")
            for code in item.reasons:
                lines.append(f"- **{code}**: {item.details.get(code, '')}")
            lines.append("")

    lines.extend([
        f"",
        f"---",
        f"",
        f"## 技能活動 — 本週載入次數",
        f"",
    ])
    if not report.digests:
        lines.append("(no skill activity in window)")
    else:
        lines.append("| skill | loads | unloads | sessions | episodes_truncated |")
        lines.append("|-------|-------|---------|----------|--------------------|")
        for sid in sorted(report.digests.keys()):
            d = report.digests[sid]
            trunc = "yes" if d.episodes_truncated else ""
            lines.append(
                f"| {sid} | {d.load_count} | {d.unload_count} | "
                f"{len(d.sessions)} | {trunc} |"
            )

    # 存在但未啟動: 在 disk 但沒進 digests
    unused = [s for s in report.skills_on_disk if s not in report.digests]
    if unused:
        lines.extend([
            f"",
            f"---",
            f"",
            f"## 存在但未啟動 ({len(unused)})",
            f"",
            f"檔案系統存在但本週未被載入：",
        ])
        for sid in sorted(unused):
            lines.append(f"- {sid}")

    lines.append("")
    return "\n".join(lines)


async def generate_weekly_report(
    ledger: "LedgerStore",
    *,
    skills_roots: list[Path] | None = None,
    output_dir: Path | None = None,
    window_days: int = 7,
    now_ts: float | None = None,
    write_to_disk: bool = True,
) -> WeeklyReport:
    """Run a full weekly skill review.

    Args:
        ledger: open LedgerStore.
        skills_roots: directories containing skill folders (``SKILL.md`` per dir).
            None = skip filesystem comparisons (no "exists_but_unused" or
            "undigested_feedback" detection).
        output_dir: where to write the markdown. None = don't write.
        window_days: review window length.
        now_ts: anchor "now" for the window end. Defaults to current time.
            Test-only override.
        write_to_disk: if True and output_dir is given, write the report.

    Returns:
        :class:`WeeklyReport` with markdown + attention list. ``output_path``
        is populated when the report was written.
    """
    end_ts = now_ts if now_ts is not None else time.time()
    start_ts = end_ts - window_days * 86400

    skills_seen = await _distinct_skills_in_window(
        ledger, since_ts=start_ts, until_ts=end_ts,
    )
    fs_skills = (
        _scan_filesystem_skills(skills_roots) if skills_roots else {}
    )

    # Union: ledger-seen + disk-seen so "exists_but_unused" gets noticed.
    all_skill_ids = sorted(set(skills_seen) | set(fs_skills.keys()))

    digests = await _digest_all_skills(
        ledger, skills_seen,            # only query digests for skills with events
        since_ts=start_ts, until_ts=end_ts,
    )

    attention: list[SkillAttention] = []
    for sid in all_skill_ids:
        d = digests.get(sid)
        skill_dir = fs_skills.get(sid)
        if d is None:
            # Skill on disk, no ledger events.
            if skill_dir is not None:
                attention.append(SkillAttention(
                    skill_id=sid,
                    reasons=[ATTENTION_UNUSED],
                    details={ATTENTION_UNUSED: "exists on disk but never loaded in window"},
                ))
            continue
        item = compute_attention(d, skill_dir, now_ts=end_ts)
        if item is not None:
            attention.append(item)

    report = WeeklyReport(
        window_start_ts=start_ts,
        window_end_ts=end_ts,
        skills_seen=skills_seen,
        skills_on_disk=sorted(fs_skills.keys()),
        digests=digests,
        attention=attention,
        markdown="",  # filled below
    )
    # Markdown render uses the WeeklyReport itself; rebuild with content.
    markdown = render_weekly_markdown(report)
    report = WeeklyReport(
        window_start_ts=report.window_start_ts,
        window_end_ts=report.window_end_ts,
        skills_seen=report.skills_seen,
        skills_on_disk=report.skills_on_disk,
        digests=report.digests,
        attention=report.attention,
        markdown=markdown,
    )

    if write_to_disk and output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        out_path = output_dir / f"{stamp}-skill-weekly.md"
        out_path.write_text(markdown, encoding="utf-8")
        report = WeeklyReport(
            window_start_ts=report.window_start_ts,
            window_end_ts=report.window_end_ts,
            skills_seen=report.skills_seen,
            skills_on_disk=report.skills_on_disk,
            digests=report.digests,
            attention=report.attention,
            markdown=markdown,
            output_path=out_path,
        )

    return report
