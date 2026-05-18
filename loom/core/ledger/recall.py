"""Ledger recall — human-readable rendering over EventQuery results.

doc/53 Pull API (`LedgerStore.events`) covers the structured query layer.
This module is the rendering layer the agent uses when it wants to
"recall" past activity in story form, not as a SQL result set.

Three output formats:

- ``narrative`` — per-turn paragraph story. Header + user intent + agent
  action chain. Default for queries scoped to ``session_id`` /
  ``correlation_id`` (i.e. "rebuild what happened in this slice").
- ``summary``  — aggregate stats over the result set (tool counts,
  success/failure ratio, turn outcomes). Default for queries scoped to
  ``skill_id`` / ``tool_name`` / time range.
- ``raw``      — one-line-per-event timeline. Escape hatch when the agent
  wants to inspect events directly.

Cross-database join (events in ``ledger.db`` + user messages in
``memory.db.session_log``) happens in the tool layer; the renderer takes
``messages_by_turn`` as a plain dict so it stays pure-data and trivially
testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from loom.core.ledger.schema import LedgerEvent


OutputFormat = Literal["narrative", "summary", "raw"]

_USER_TEXT_CAP = 120        # user message preview length
_ERR_TEXT_CAP = 60          # error text length in action chain
_COMPACT_TOOL_THRESHOLD = 8  # actions per turn before we condense
_RAW_DEFAULT_MAX = 100

_TRUNCATION_WARNING = (
    "[!] 結果觸及 ledger 查詢上限；以下內容可能不完整，"
    "請收窄 filter（時間範圍 / session_id / correlation_id）取得完整結果。"
)


# ── Turn grouping ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class TurnSlice:
    """One turn's window: bookends + events in between.

    ``started_at`` / ``ended_at`` come from ``turn_start`` / ``turn_end``
    when present; missing bookends fall back to the first/last event
    timestamp in the slice.
    """

    turn_id: str
    session_id: str
    started_at: float
    ended_at: float | None
    outcome: str | None
    events: list[LedgerEvent] = field(default_factory=list)


def group_events_by_turn(events: list[LedgerEvent]) -> list[TurnSlice]:
    """Split an event list into per-turn slices, preserving turn order.

    Order is determined by the first event seen per ``turn_id``. The
    caller is responsible for sorting ``events`` by timestamp ASC if
    chronological ordering matters.
    """
    by_turn: dict[str, list[LedgerEvent]] = {}
    order: list[str] = []
    for e in events:
        if e.turn_id not in by_turn:
            by_turn[e.turn_id] = []
            order.append(e.turn_id)
        by_turn[e.turn_id].append(e)

    slices: list[TurnSlice] = []
    for turn_id in order:
        turn_events = by_turn[turn_id]
        start_ts: float | None = None
        end_ts: float | None = None
        outcome: str | None = None
        for e in turn_events:
            if e.event_type == "turn_start":
                start_ts = e.timestamp
            elif e.event_type == "turn_end":
                end_ts = e.timestamp
                outcome = e.payload.get("outcome")
        if start_ts is None:
            start_ts = turn_events[0].timestamp
        slices.append(
            TurnSlice(
                turn_id=turn_id,
                session_id=turn_events[0].session_id,
                started_at=start_ts,
                ended_at=end_ts,
                outcome=outcome,
                events=turn_events,
            )
        )
    return slices


# ── Time formatting ───────────────────────────────────────────────────


def _fmt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")


def _fmt_full(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# ── Narrative renderer ────────────────────────────────────────────────


def render_narrative(
    slices: list[TurnSlice],
    messages_by_turn: dict[str, list[dict[str, Any]]],
    *,
    verbose: bool = False,
    truncated: bool = False,
) -> str:
    """Per-turn paragraph narrative.

    Each turn renders as a three-line block: header (round + HH:MM),
    user intent (first user message preview, or fallback), and an agent
    action chain summarising the tool calls.

    ``messages_by_turn`` is keyed by ``turn_id`` and holds session_log
    rows shaped like :meth:`SessionLog.messages_between` output.

    ``truncated=True`` prepends a warning so the agent knows the
    underlying ledger fetch hit the per-query cap.
    """
    if not slices:
        return _maybe_prepend_warning(_empty_window(), truncated)

    sessions_seen: list[str] = []
    for sl in slices:
        if sl.session_id not in sessions_seen:
            sessions_seen.append(sl.session_id)

    parts: list[str] = []
    for sid in sessions_seen:
        sid_slices = [sl for sl in slices if sl.session_id == sid]
        first_ts = sid_slices[0].started_at
        parts.append(f"## session {sid} — {_fmt_date(first_ts)}")
        parts.append("")
        for idx, sl in enumerate(sid_slices, start=1):
            parts.append(
                _render_turn(
                    sl, idx,
                    messages_by_turn.get(sl.turn_id, []),
                    verbose=verbose,
                )
            )
            parts.append("")
    return _maybe_prepend_warning("\n".join(parts).rstrip(), truncated)


def _render_turn(
    sl: TurnSlice,
    idx: int,
    messages: list[dict[str, Any]],
    *,
    verbose: bool,
) -> str:
    header = f"第 {idx} 輪 ({_fmt_time(sl.started_at)})"
    user_line = _render_user_line(messages)
    action_line = _render_actions(sl.events, verbose=verbose)
    if sl.outcome and sl.outcome not in {"clean", None}:
        action_line += f"  ← turn 結局 {sl.outcome}"
    return f"{header}\n{user_line}\n{action_line}"


def _render_user_line(messages: list[dict[str, Any]]) -> str:
    for m in messages:
        if m.get("role") != "user":
            continue
        text = (m.get("content") or "").strip()
        if not text:
            continue
        if len(text) > _USER_TEXT_CAP:
            text = text[:_USER_TEXT_CAP] + "…"
        return f"你說：{text}"
    return "（沒有捕捉到你的訊息）"


def _render_actions(events: list[LedgerEvent], *, verbose: bool) -> str:
    ends = [
        e for e in events
        if e.event_type == "tool_lifecycle"
        and e.payload.get("phase") == "END"
    ]
    if not ends:
        return "我這回合沒有動工具。"

    parts: list[str] = []
    successes = 0
    failures = 0
    rolled = 0
    for e in ends:
        p = e.payload
        tool = p.get("tool_name", "?")
        if p.get("rolled_back"):
            parts.append(f"{tool}（rollback）")
            rolled += 1
        elif p.get("error"):
            err = (p.get("error") or "")[:_ERR_TEXT_CAP]
            parts.append(f"{tool}（失敗：{err}）")
            failures += 1
        else:
            parts.append(tool)
            successes += 1

    total = len(ends)
    duration_s = ends[-1].timestamp - ends[0].timestamp if total >= 2 else 0
    summary_bits = [f"{total} events"]
    if duration_s > 0:
        summary_bits.append(f"{duration_s:.1f}s")
    if failures or rolled:
        outcome_bits = f"{successes}成 {failures}敗"
        if rolled:
            outcome_bits += f" {rolled}回滾"
        summary_bits.append(outcome_bits)
    suffix = f"（{', '.join(summary_bits)}）"

    if not verbose and total > _COMPACT_TOOL_THRESHOLD:
        unique_tools: list[str] = []
        for e in ends:
            t = e.payload.get("tool_name", "?")
            if t not in unique_tools:
                unique_tools.append(t)
        body = f"我做了 {total} 件事，主要用 " + "、".join(unique_tools[:5])
    else:
        body = "我做了：" + " → ".join(parts)
    return f"{body}{suffix}"


# ── Summary renderer ──────────────────────────────────────────────────


def render_summary(
    events: list[LedgerEvent], *, truncated: bool = False,
) -> str:
    """Aggregate stats: window, sessions, per-tool success/failure, turn outcomes.

    ``truncated=True`` prepends a warning so the agent doesn't quote
    success rates / totals as if they cover the full population.
    """
    if not events:
        return _maybe_prepend_warning(_empty_window(), truncated)

    start_ts = min(e.timestamp for e in events)
    end_ts = max(e.timestamp for e in events)

    by_tool: dict[str, dict[str, int]] = {}
    sessions: set[str] = set()
    turn_outcomes: dict[str, int] = {}

    for e in events:
        sessions.add(e.session_id)
        if (
            e.event_type == "tool_lifecycle"
            and e.payload.get("phase") == "END"
        ):
            t = e.payload.get("tool_name", "?")
            slot = by_tool.setdefault(t, {"ok": 0, "err": 0})
            if e.payload.get("error") or e.payload.get("rolled_back"):
                slot["err"] += 1
            else:
                slot["ok"] += 1
        elif e.event_type == "turn_end":
            o = e.payload.get("outcome", "?")
            turn_outcomes[o] = turn_outcomes.get(o, 0) + 1

    sessions_sorted = sorted(sessions)
    sessions_preview = ", ".join(sessions_sorted[:5])
    if len(sessions_sorted) > 5:
        sessions_preview += "…"

    lines = [
        f"## 摘要：{_fmt_full(start_ts)} ~ {_fmt_full(end_ts)}",
        f"session 數：{len(sessions_sorted)}（{sessions_preview}）",
        f"事件總數：{len(events)}",
    ]

    if by_tool:
        lines.append("")
        lines.append("工具使用：")
        for tool, stats in sorted(
            by_tool.items(),
            key=lambda kv: -(kv[1]["ok"] + kv[1]["err"]),
        ):
            total = stats["ok"] + stats["err"]
            rate = (stats["ok"] / total * 100) if total else 0
            lines.append(
                f"  - {tool}: {total} 次"
                f"（成功 {stats['ok']}, 失敗 {stats['err']}, {rate:.0f}%）"
            )

    if turn_outcomes:
        lines.append("")
        lines.append("turn 結局：")
        for outcome, n in sorted(
            turn_outcomes.items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"  - {outcome}: {n}")

    return _maybe_prepend_warning("\n".join(lines), truncated)


# ── Raw renderer (escape hatch) ───────────────────────────────────────


def render_raw(
    events: list[LedgerEvent],
    *,
    max_events: int = _RAW_DEFAULT_MAX,
    truncated: bool = False,
) -> str:
    """One-line-per-event timeline. Truncates beyond ``max_events``.

    ``truncated=True`` (the fetch-level cap was hit) is distinct from
    ``max_events`` (the render-level cap shown in the header); both can
    apply simultaneously and the warning makes the distinction explicit.
    """
    if not events:
        return _maybe_prepend_warning(_empty_window(), truncated)

    cap_hit = len(events) > max_events
    header = f"## Raw events ({len(events)} total"
    if cap_hit:
        header += f", showing first {max_events}"
    header += ")"

    lines = [header]
    for e in events[:max_events]:
        lines.append(_render_raw_line(e))
    return _maybe_prepend_warning("\n".join(lines), truncated)


def _render_raw_line(e: LedgerEvent) -> str:
    p = e.payload
    extras: list[str] = []
    if "tool_name" in p:
        extras.append(f"tool={p['tool_name']}")
    if p.get("phase"):
        extras.append(f"phase={p['phase']}")
    if p.get("verdict"):
        extras.append(f"verdict={p['verdict']}")
    if p.get("outcome"):
        extras.append(f"outcome={p['outcome']}")
    if p.get("error"):
        extras.append(f"err={(p['error'] or '')[:40]}")
    extras_s = (" " + " ".join(extras)) if extras else ""
    return f"  {_fmt_full(e.timestamp)}  [{e.event_type}]{extras_s}"


# ── Output format inference ───────────────────────────────────────────


def infer_output_format(
    *,
    session_id: str | None = None,
    correlation_id: str | None = None,
    skill_id: str | None = None,
    tool_name: str | None = None,
    verdict: str | None = None,
    since: str | None = None,
    until: str | None = None,
    explicit: str | None = None,
) -> OutputFormat:
    """Pick the renderer from the query dimensions the agent supplied.

    Explicit override wins. Otherwise: scoped-slice queries
    (``session_id`` / ``correlation_id``) get narrative; the rest fall
    through to summary.
    """
    if explicit in ("narrative", "summary", "raw"):
        return explicit  # type: ignore[return-value]
    if session_id or correlation_id:
        return "narrative"
    return "summary"


# ── Helpers ───────────────────────────────────────────────────────────


def _empty_window() -> str:
    return "(這段時間沒有可回憶的事件)"


def _maybe_prepend_warning(body: str, truncated: bool) -> str:
    if not truncated:
        return body
    return f"{_TRUNCATION_WARNING}\n\n{body}"
