"""Check permission names against the tool registry (#690 S4).

``allowed_tools`` in ``schedules.toml``, ``rhythm.toml`` and ``room.toml`` are
plain strings. Nothing used to compare them with the tools that actually
exist, so a skill name (``async_jobs``), a retired MCP server's tool
(``openai__text_to_image``) or a tool that only registers with a key
(``web_search``) sat in the grant lists silently doing nothing.

This module only *reports*. It never corrects, aliases or widens a grant: what
a schedule or anchor is authorised to do stays exactly what the file says.

Each name gets one :class:`NameStatus`:

- ``registered`` — the reference session has a tool by that name.
- ``conditional`` — a known tool that is off right now: a native tool that
  registers only with some config, env var or session kind
  (:data:`CONDITIONAL_TOOLS`), or a tool of a configured MCP server that did
  not connect.
- ``pending`` — a configured MCP server's tool, checked before the session
  finished loading MCP. Not reported as unknown until MCP has had its chance.
- ``unknown`` — nothing registers this name here.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from loom.autonomy.permission_fields import parse_permission_fields

logger = logging.getLogger(__name__)


class NameStatus(Enum):
    REGISTERED = "registered"
    CONDITIONAL = "conditional"
    PENDING = "pending"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class NameCheck:
    name: str
    status: NameStatus
    sources: tuple[str, ...]  # e.g. ("schedule morning_briefing", "rhythm dawn")
    reason: str = ""


# Native tools whose registration depends on config, env or the kind of
# session. Absent from the registry they are "off", not misspelled. Kept in
# step with the factories by tests/test_permission_names_690.py.
CONDITIONAL_TOOLS: dict[str, str] = {
    "web_search": "needs BRAVE_SEARCH_KEY in .env",
    "image_generate": "needs [tools.image] enabled with default_provider = \"openai\"",
    "tts_generate": "needs [tools.tts] enabled with default_provider = \"xai\"",
    "memory_attend": "needs [memory.attention] enabled",
    "agent_health": "needs telemetry",
    "request_model_tier": "needs [cognition.tiers]",
    "clear_model_tier": "needs [cognition.tiers]",
    "set_reasoning_effort": "needs a configured model that supports effort",
    "create_discord_forum_post": "needs DISCORD_NEWS_FORUM_ID",
    "read_discord_forum": "needs DISCORD_NEWS_FORUM_ID",
    "send_discord_file": "registered only in Discord thread sessions",
    "send_discord_embed": "registered only in Discord thread sessions",
    "send_discord_select": "registered only in Discord thread sessions",
    "add_discord_reaction": "registered only in Discord thread sessions",
}

_MCP_SEPARATOR = "__"  # mcp_client registers "<server>__<tool>"

# Always-registered native tools across CLI / Memory / Skills / Circadian / Weather.
# Combined with CONDITIONAL_TOOLS this forms the full native catalogue used for
# load-time drift warnings (#708 S4b), kept in lockstep with ToolDefinition
# factories by tests/test_allowed_tool_drift_708_s4b.py.
CORE_NATIVE_TOOLS: frozenset[str] = frozenset({
    # Filesystem / vision / shell
    "read_file",
    "write_file",
    "list_dir",
    "probe_file",
    "see_image",
    "run_bash",
    # Memory / ledger
    "recall",
    "memorize",
    "relate",
    "memory_health",
    "memory_prune",
    "ledger_query",
    # Skills
    "load_skill",
    "unload_skill",
    "skill_review",
    # Web / weather / subagent
    "fetch_url",
    "weather",
    "spawn_agent",
    # Tasks / pursuits / background jobs / scratchpad
    "task_write",
    "pursuit_list",
    "pursuit_read",
    "pursuit_write",
    "jobs_list",
    "jobs_status",
    "jobs_await",
    "jobs_cancel",
    "scratchpad_read",
    # Circadian
    "weave_revise",
    "journal_append",
})

KNOWN_NATIVE_TOOLS: frozenset[str] = CORE_NATIVE_TOOLS | frozenset(CONDITIONAL_TOOLS)


def workspace_skill_names(workspace: Path) -> frozenset[str]:
    """Return workspace skill names (``<workspace>/skills/<name>/SKILL.md``)."""
    skills_dir = workspace / "skills"
    if not skills_dir.is_dir():
        return frozenset()
    names: set[str] = set()
    try:
        for entry in skills_dir.iterdir():
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            if (entry / "SKILL.md").is_file():
                names.add(entry.name)
    except OSError:
        return frozenset()
    return frozenset(names)


def configured_mcp_servers(workspace: Path) -> frozenset[str]:
    """Read configured ``[[mcp.servers]]`` names from ``<workspace>/loom.toml`` (tolerant)."""
    config_path = workspace / "loom.toml"
    if not config_path.is_file():
        return frozenset()
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return frozenset()
    mcp_cfg = raw.get("mcp")
    if not isinstance(mcp_cfg, dict):
        return frozenset()
    servers = mcp_cfg.get("servers")
    if not isinstance(servers, list):
        return frozenset()
    return frozenset(
        s["name"].strip()
        for s in servers
        if isinstance(s, dict) and isinstance(s.get("name"), str) and s["name"].strip()
    )


def warn_allowed_tool_drift(
    source_name: str,
    allowed_tools: Iterable[str],
    *,
    workspace: Path | None = None,
    known_tools: Collection[str] | None = None,
    mcp_servers: Collection[str] | None = None,
    skill_names: Collection[str] | None = None,
) -> list[str]:
    """Emit WARNINGs for ``allowed_tools`` entries that look like skill names
    or drifted/retired/unknown tool names (#708 S4b).

    Report-only: never mutates or rejects the entry. Returns the drifted names.
    """
    native = KNOWN_NATIVE_TOOLS if known_tools is None else known_tools
    if mcp_servers is None:
        servers: Collection[str] = (
            configured_mcp_servers(workspace) if workspace is not None else frozenset()
        )
    else:
        servers = mcp_servers
    if skill_names is None:
        skills: Collection[str] = (
            workspace_skill_names(workspace) if workspace is not None else frozenset()
        )
    else:
        skills = skill_names

    drifted: list[str] = []
    for raw in allowed_tools:
        name = raw.strip()
        if not name or name in native:
            continue
        if _MCP_SEPARATOR in name:
            server = name.split(_MCP_SEPARATOR, 1)[0]
            if server in servers:
                continue
            drifted.append(name)
            logger.warning(
                "[autonomy] %s allowed_tools includes %r, a drifted or retired MCP tool "
                "(MCP server %r is not configured); remove or update this entry",
                source_name, name, server,
            )
            continue
        if name in skills:
            drifted.append(name)
            logger.warning(
                "[autonomy] %s allowed_tools includes %r, which is a workspace skill name, "
                "not a tool name; use skills = [%r] or load_skill instead of allowed_tools",
                source_name, name, name,
            )
            continue
        drifted.append(name)
        logger.warning(
            "[autonomy] %s allowed_tools includes unknown/drifted tool %r "
            "(not a known native tool or configured MCP server prefix); check for typos "
            "or retired tools",
            source_name, name,
        )
    return drifted


def collect_permission_uses(
    schedules_cfg: Mapping[str, Any],
    anchors: Iterable[Any],
    furniture: Iterable[Any],
) -> dict[str, tuple[str, ...]]:
    """Map each granted tool name to the places that grant it."""
    uses: dict[str, list[str]] = {}

    def add(names: Iterable[str], source: str) -> None:
        for name in names:
            places = uses.setdefault(name, [])
            if source not in places:
                places.append(source)

    for kind, key in (("schedule", "schedules"), ("trigger", "triggers")):
        for entry in schedules_cfg.get(key, []):
            name = entry.get("name", "?")
            add(parse_permission_fields(entry, name)["allowed_tools"], f"{kind} {name}")
    for anchor in anchors:  # recurring anchors repeat per slot; add() dedupes
        add(anchor.allowed_tools, f"rhythm {anchor.name}")
    for item in furniture:
        add(item.allowed_tools, f"room {item.name}")
    return {name: tuple(places) for name, places in uses.items()}


def classify_permission_names(
    uses: Mapping[str, tuple[str, ...]],
    *,
    registered: Collection[str],
    mcp_configured: Collection[str],
    mcp_connected: Collection[str] | None,
) -> list[NameCheck]:
    """Classify every name in *uses*.

    ``mcp_connected`` is ``None`` while the session has not finished loading
    MCP: tools of configured servers are then ``pending``.
    """
    checks: list[NameCheck] = []
    for name, sources in uses.items():
        server = name.split(_MCP_SEPARATOR, 1)[0] if _MCP_SEPARATOR in name else None
        if name in registered:
            status, reason = NameStatus.REGISTERED, ""
        elif name in CONDITIONAL_TOOLS:
            status, reason = NameStatus.CONDITIONAL, CONDITIONAL_TOOLS[name]
        elif server in mcp_configured and mcp_connected is None:
            status, reason = NameStatus.PENDING, f"MCP server {server} still loading"
        elif server in mcp_configured and server not in mcp_connected:
            status, reason = NameStatus.CONDITIONAL, f"MCP server {server} not connected"
        elif server in mcp_configured:
            status, reason = NameStatus.UNKNOWN, f"MCP server {server} has no such tool"
        else:
            status, reason = NameStatus.UNKNOWN, ""
        checks.append(NameCheck(name, status, sources, reason))
    return checks


_LABELS = {
    NameStatus.UNKNOWN: "unknown",
    NameStatus.CONDITIONAL: "off",
    NameStatus.PENDING: "pending",
}


def format_report(checks: list[NameCheck], limit: int | None = None) -> str:
    """One line naming every non-registered name; ``""`` when all register.

    With *limit*, at most that many names are listed and the rest become one
    "另有 N 個" tail. The chime takes the capped line, the log the full one:
    the line is longest exactly when something is broken (an empty registry
    puts every granted name in it), and the chime repeats it every dawn.
    """
    parts = []
    for status, label in _LABELS.items():
        for c in checks:
            if c.status is status:
                why = f"; {c.reason}" if c.reason else ""
                parts.append(f"{label}: {c.name} ({', '.join(c.sources)}{why})")
    if limit is not None and len(parts) > limit:
        hidden = len(parts) - limit
        parts = [*parts[:limit], f"…另有 {hidden} 個（完整清單見 log）"]
    return " | ".join(parts)
