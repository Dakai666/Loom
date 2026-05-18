"""Default text renderer for SkillUsageDigest (doc/54 §4.3.1).

Produces a dense, agent-readable transcript of skill activity. Used by
the ``skill_review`` tool; the weekly worker can reuse or replace as
needed for richer markdown.

Format choices:
- Timestamps in the user's configured timezone (matches the rest of the
  agent-facing surface; see issue #388 boundary)
- Per-episode block — agent quotes/cites blocks back during discussion
- Compact event lines — no syntactic clutter; one event per line
- Inferred-boundary tag on episodes that lack an explicit unload, so
  reviewers notice the load/unload bookend gap
"""

from __future__ import annotations

from datetime import datetime

from loom.core.skill_review.query import SkillUsageDigest, SkillEpisode
from loom.core.timezone import user_zone


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=user_zone()).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_event_line(e) -> str:
    """One-line summary of a single LedgerEvent."""
    ts = datetime.fromtimestamp(e.timestamp, tz=user_zone()).strftime("%H:%M:%S")
    et = e.event_type
    p = e.payload

    if et == "tool_lifecycle":
        phase = p.get("phase", "?")
        tool = p.get("tool_name", "?")
        err = p.get("error")
        rolled = p.get("rolled_back")
        flags: list[str] = []
        if err:
            flags.append(f'err="{err[:80]}"')
        if rolled:
            flags.append("rolled_back")
        result = "failure" if err else "success"
        suffix = (" " + " ".join(flags)) if flags else ""
        return f"  + {ts} tool_lifecycle:{phase:5} {tool:20} {result}{suffix}"

    if et == "memory_op":
        op = p.get("operation", "?")
        trust = p.get("trust_tier")
        trust_s = f" trust={trust}" if trust else ""
        return f"  + {ts} memory_op:{op:5}{trust_s}"

    if et == "judge_verdict":
        v = p.get("verdict", "?")
        conf = p.get("confidence")
        conf_s = f" conf={conf:.2f}" if isinstance(conf, (int, float)) else ""
        return f"  + {ts} judge_verdict   {v}{conf_s}"

    if et == "turn_end":
        outcome = p.get("outcome", "?")
        return f"  + {ts} turn_end        outcome={outcome}"

    return f"  + {ts} {et}"


def _render_episode(ep: SkillEpisode, idx: int) -> str:
    if ep.unloaded_at is None:
        unload_line = "<no explicit unload>"
        if ep.unload_inferred:
            unload_line += f" (inferred: {ep.unload_inferred_reason})"
    elif ep.unload_inferred:
        unload_line = f"{_fmt_ts(ep.unloaded_at)} (inferred: {ep.unload_inferred_reason})"
    else:
        unload_line = _fmt_ts(ep.unloaded_at)
    lines = [
        f"## Episode {idx} — {ep.session_id} / {ep.turn_id} — {_fmt_ts(ep.loaded_at)}",
        f"loaded_at:   {_fmt_ts(ep.loaded_at)}",
        f"unloaded_at: {unload_line}",
        f"turn_outcome: {ep.turn_outcome or '<no turn_end>'}",
    ]
    suffix = " (truncated)" if ep.truncated else ""
    lines.append(f"events_after_load ({len(ep.events_after_load)}{suffix}):")
    if not ep.events_after_load:
        lines.append("  (none)")
    else:
        lines.extend(_fmt_event_line(e) for e in ep.events_after_load)
    return "\n".join(lines)


def render_digest_as_text(digest: SkillUsageDigest) -> str:
    """Render a SkillUsageDigest into a dense text block for the agent."""
    header = [
        f"# Skill review: {digest.skill_id}",
        f"Window: {_fmt_ts(digest.since_ts)} ~ {_fmt_ts(digest.until_ts)}",
        f"Loads: {digest.load_count}, Unloads: {digest.unload_count}",
        f"Sessions: {len(digest.sessions)}"
        + (f" ({', '.join(digest.sessions)})" if digest.sessions else ""),
        f"Episodes: {len(digest.episodes)}"
        + (" (truncated)" if digest.episodes_truncated else ""),
    ]
    if not digest.episodes:
        header.append("\n(no activations in this window)")
        return "\n".join(header)

    blocks = [_render_episode(ep, i + 1) for i, ep in enumerate(digest.episodes)]
    return "\n".join(header) + "\n\n" + "\n\n".join(blocks)
