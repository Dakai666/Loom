"""Default text renderer for SkillUsageDigest (doc/54 §4.3.1).

Produces a dense, agent-readable transcript of skill activity. Used by
the ``skill_review`` tool; the weekly worker can reuse or replace as
needed for richer markdown.

Format choices:
- ISO-ish timestamps (UTC) — readable, sortable, no timezone surprises
- Per-episode block — agent quotes/cites blocks back during discussion
- Compact event lines — no syntactic clutter; one event per line
"""

from __future__ import annotations

from datetime import datetime, timezone

from loom.core.skill_review.query import SkillUsageDigest, SkillEpisode


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_event_line(e) -> str:
    """One-line summary of a single LedgerEvent."""
    ts = datetime.fromtimestamp(e.timestamp, tz=timezone.utc).strftime("%H:%M:%S")
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
    lines = [
        f"## Episode {idx} — {ep.session_id} / {ep.turn_id} — {_fmt_ts(ep.loaded_at)}",
        f"loaded_at:   {_fmt_ts(ep.loaded_at)}",
        f"unloaded_at: {_fmt_ts(ep.unloaded_at) if ep.unloaded_at else '<no explicit unload>'}",
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
