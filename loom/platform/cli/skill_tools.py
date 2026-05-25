"""
Skill loading / lifecycle / review tools.

Extracted from ``loom/platform/cli/tools.py`` during audit-B (#399) because
the skill-tool cluster (~500 lines) was physically nested inside SECTION 3
(MEMORY TOOLS) but is a distinct concern: skill activation, precondition
mounting, and ledger-backed usage review.

Registered by ``LoomSession.start()`` alongside the other tool factories.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolDefinition

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    from loom.core.harness.skill_checks import SkillCheckManager
    from loom.core.ledger import LedgerStore
    from loom.core.memory.procedural import ProceduralMemory, SkillGenome
    from loom.core.memory.semantic import SemanticMemory
    from loom.core.memory.skill_outcome import SkillOutcomeTracker


# ------------------------------------------------------------------


def _strip_frontmatter(body: str) -> str:
    """Remove YAML frontmatter (--- delimited) from skill body."""
    if not body.startswith("---"):
        return body
    parts = body.split("---", 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return body


def _find_skill_resources(
    skill_name: str, skills_dirs: list[Path],
) -> tuple[str | None, list[str]]:
    """Locate skill directory and list bundled files (scripts/, references/, assets/)."""
    resource_dirs = ("scripts", "references", "assets")
    # Try both hyphenated and underscored variants
    variants = [skill_name, skill_name.replace("-", "_"), skill_name.replace("_", "-")]

    for base_dir in skills_dirs:
        if not base_dir.is_dir():
            continue
        for variant in variants:
            skill_dir = base_dir / variant
            if not skill_dir.is_dir():
                continue

            resources: list[str] = []
            for res_name in resource_dirs:
                res_dir = skill_dir / res_name
                if res_dir.is_dir():
                    for f in sorted(res_dir.rglob("*")):
                        if f.is_file():
                            resources.append(str(f.relative_to(skill_dir)))

            # Also list top-level non-SKILL.md files
            for f in sorted(skill_dir.iterdir()):
                if f.is_file() and f.name != "SKILL.md":
                    rel = str(f.relative_to(skill_dir))
                    if rel not in resources:
                        resources.append(rel)

            return str(skill_dir), resources

    return None, []


async def _get_evolution_hints(
    procedural: "ProceduralMemory",
    semantic: "SemanticMemory | None",
    skill_name: str,
) -> list[str]:
    """Fetch evolution hints for a skill.

    Reads real evolution hints written by ``SkillEvolutionHook`` from
    SemanticMemory (key pattern ``skill:<name>:evolution_hint:*``).
    Falls back to a confidence-based generic warning if no stored hints.
    """
    hints: list[str] = []

    # Query real evolution hints from semantic memory
    if semantic is not None:
        try:
            entries = await semantic.list_by_prefix(
                f"skill:{skill_name}:evolution_hint:", limit=3,
            )
            for entry in entries:
                hints.append(entry.value)
        except Exception:
            pass  # semantic query failure must never block load_skill

    # Fallback: confidence-based warning if no stored hints
    if not hints:
        skill = await procedural.get(skill_name)
        if (skill is not None
                and skill.confidence < 0.6
                and skill.usage_count >= 3):
            hints.append(
                f"⚠ This skill's confidence is {skill.confidence:.2f} "
                f"(usage: {skill.usage_count}×). "
                f"Consider reviewing recent outcomes and improving the workflow."
            )
    return hints




# ------------------------------------------------------------------
# Skill loading tool (Issue #56 — Agent Skills spec Tier 2)
# ------------------------------------------------------------------

def make_load_skill_tool(
    procedural: "ProceduralMemory",
    skills_dirs: list[Path] | None = None,
    outcome_tracker: "SkillOutcomeTracker | None" = None,
    semantic: "SemanticMemory | None" = None,
    turn_index_fn: "Callable[[], int] | None" = None,
    skill_check_manager: "SkillCheckManager | None" = None,
    confirm_fn: "Callable | None" = None,
    on_loaded: "Callable[[str], None] | None" = None,
) -> ToolDefinition:
    """
    Create a SAFE ``load_skill`` tool that loads full skill instructions.

    Implements the Agent Skills spec Tier 2: on-demand activation.
    - Returns the full SKILL.md body wrapped in ``<skill_content>`` XML
    - Lists bundled resources (scripts/, references/, assets/)
    - Deduplicates: returns a short note on second activation in same session
    - Issue #64 Phase B: mounts skill-declared precondition checks
    """
    from loom.core.memory.procedural import ProceduralMemory  # type: ignore[attr-defined]

    # Per-session dedup tracking (set of already-loaded skill names)
    _loaded_this_session: set[str] = set()
    _dirs = skills_dirs or []

    async def _load_skill(call: ToolCall) -> ToolResult:
        name = call.args.get("name", "").strip()
        keep_existing = call.args.get("keep_existing", False)
        if not name:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="'name' argument is required",
            )

        # Dedup check
        if name in _loaded_this_session:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=True,
                output=f"Skill '{name}' is already loaded in this session. "
                       f"Refer to the previously loaded instructions.",
            )

        # Try ProceduralMemory first
        skill = await procedural.get(name)
        if skill is None:
            # Also try with underscores converted to hyphens and vice versa
            alt_name = name.replace("-", "_") if "-" in name else name.replace("_", "-")
            skill = await procedural.get(alt_name)
            if skill is not None:
                name = alt_name

        if skill is None:
            # List available skills to help
            active = await procedural.list_active()
            available = ", ".join(s.name for s in active[:10])
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error=f"Skill '{name}' not found. Available: {available or '(none)'}",
            )

        body = skill.body or "(no instructions)"
        # Strip YAML frontmatter from body (it's metadata, already parsed)
        body = _strip_frontmatter(body)

        # Find skill directory and list bundled resources
        skill_dir, resources = _find_skill_resources(name, _dirs)

        lines = [f'<skill_content name="{name}">']

        # Evolution hints (from semantic memory, if available)
        evolution_hints = await _get_evolution_hints(procedural, semantic, name)
        if evolution_hints:
            lines.append("<evolution_hints>")
            for hint in evolution_hints:
                lines.append(f"  {hint}")
            lines.append("</evolution_hints>")
            lines.append("")

        lines.append(body)

        if skill_dir:
            lines.append("")
            lines.append(f"Skill directory: {skill_dir}")
            lines.append(
                "Relative paths in this skill are relative to the skill directory."
            )

        if resources:
            lines.append("")
            lines.append("<skill_resources>")
            for res in resources:
                lines.append(f"  <file>{res}</file>")
            lines.append("</skill_resources>")

        # Issue #64 Phase B: mount skill-declared precondition checks.
        # Approval reads/writes go through the semantic store (#451 phase B).
        checks_summary = await _mount_skill_checks(
            name, skill, skill_dir, skill_check_manager, semantic,
            keep_existing=keep_existing,
        )
        if checks_summary:
            lines.append("")
            lines.append("<mounted_precondition_checks>")
            for desc in checks_summary:
                lines.append(f"  {desc}")
            lines.append("</mounted_precondition_checks>")

        lines.append("</skill_content>")

        output = "\n".join(lines)

        # Record activation
        _loaded_this_session.add(name)
        if on_loaded is not None:
            on_loaded(name)
        if outcome_tracker is not None:
            _turn = turn_index_fn() if turn_index_fn else 0
            outcome_tracker.record_activation(name, _turn)

        metadata: dict = {"skill_name": name, "skill_confidence": skill.confidence}
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True, output=output,
            metadata=metadata,
        )

    async def _mount_skill_checks(
        name: str,
        skill: "SkillGenome",
        skill_dir_str: str | None,
        manager: "SkillCheckManager | None",
        sem: "SemanticMemory | None",
        keep_existing: bool = False,
    ) -> list[str]:
        """Resolve and mount precondition checks for a skill. Returns descriptions."""
        if manager is None:
            return []

        # Harness invariant: every load_skill is a lifecycle event for the
        # manager, even when the new skill declares no checks.  Without this,
        # a skill with no checks would leave the previous skill's checks
        # stranded on tool definitions (Issue #184).
        if not skill.precondition_check_refs:
            manager.activate(name, keep_existing=keep_existing)
            return []

        from loom.core.harness.skill_checks import SkillPreconditionRef, SkillCheckManager
        from loom.core.memory.relational_bridge import get_triple, upsert_triple

        refs = [SkillPreconditionRef.from_dict(d) for d in skill.precondition_check_refs]

        # Approval gate: first-time approval (stored as a relational triple
        # in the semantic store via the bridge since #451 phase B).
        rel_key = f"skill_checks:{name}"
        approved = False
        if sem is not None:
            entry = await get_triple(sem, rel_key, "approved")
            approved = entry is not None and entry.object == "true"

        if not approved:
            # Build a description of what the skill wants to mount
            check_lines = []
            for ref in refs:
                tools_str = ", ".join(ref.applies_to)
                check_lines.append(f"  {ref.ref} → [{tools_str}]: {ref.description}")
            check_preview = "\n".join(check_lines)

            if confirm_fn is not None:
                # Use the platform-aware confirm callback (works on CLI, TUI, Discord)
                from loom.core.harness.middleware import ToolCall as _ToolCall
                from loom.core.harness.registry import TrustLevel
                synthetic_call = _ToolCall(
                    tool_name=f"load_skill({name})",
                    args={"action": "mount_precondition_checks", "checks": check_preview},
                    trust_level=TrustLevel.GUARDED,
                    session_id="",
                )
                try:
                    user_ok = await confirm_fn(synthetic_call)
                except (EOFError, KeyboardInterrupt):
                    user_ok = False
            else:
                user_ok = False

            if not user_ok:
                return []

            # Persist approval
            if sem is not None:
                from loom.core.memory.relational_bridge import RelationalEntry
                await upsert_triple(sem, RelationalEntry(
                    subject=rel_key,
                    predicate="approved",
                    object="true",
                    source="user",
                ))

        # Resolve callables from skill directory
        if not skill_dir_str:
            return []

        skill_dir_path = Path(skill_dir_str)
        try:
            callables = SkillCheckManager.resolve_all(skill_dir_path, refs)
        except (FileNotFoundError, AttributeError, ImportError, ValueError) as exc:
            _log.warning("Failed to resolve checks for skill %r: %s", name, exc)
            return []

        # Mount
        return manager.mount(name, refs, callables, keep_existing=keep_existing)

    return ToolDefinition(
        name="load_skill",
        description=(
            "Load a skill's full instructions into context. Call this when a task "
            "matches a skill listed in <available_skills>. The skill's workflow, "
            "principles, and output format will be returned for you to follow. "
            "When you finish using a skill, call `unload_skill` so the activation "
            "window closes cleanly — `skill_review` and the weekly report use "
            "that bookend to attribute tool usage and feedback to the right skill."
        ),
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the skill to load (from <available_skills>)",
                },
                "keep_existing": {
                    "type": "boolean",
                    "description": (
                        "If true, keep the previous skill's precondition checks "
                        "mounted alongside the new skill's. Default: false "
                        "(auto-unmount previous skill's checks)."
                    ),
                    "default": False,
                },
            },
            "required": ["name"],
        },
        executor=_load_skill,
        tags=["skill", "memory", "activation"],
        impact_scope="memory",
    )


def make_unload_skill_tool(
    skill_check_manager: "SkillCheckManager",
) -> ToolDefinition:
    """
    Create a SAFE ``unload_skill`` tool for explicit skill check removal.

    Issue #64 Phase B: allows the agent (or user) to manually unmount
    a skill's precondition checks without loading a replacement skill.
    """
    async def _unload_skill(call: ToolCall) -> ToolResult:
        name = call.args.get("name", "").strip()
        if not name:
            # No name → list currently mounted skills
            mounted = skill_check_manager.mounted_skills()
            if not mounted:
                return ToolResult(
                    call_id=call.id, tool_name=call.tool_name,
                    success=True,
                    output="No skill precondition checks are currently mounted.",
                )
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=True,
                output=f"Skills with mounted checks: {', '.join(mounted)}",
            )

        removed = skill_check_manager.unmount(name)
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True,
            output=(
                f"Unmounted {removed} precondition check(s) for skill '{name}'."
                if removed > 0
                else f"Skill '{name}' had no mounted precondition checks."
            ),
        )

    return ToolDefinition(
        name="unload_skill",
        description=(
            "Close out a previously loaded skill. Call this once you're done "
            "using a skill — it removes the skill's precondition checks AND "
            "bookends the activation in the ledger so `skill_review` can "
            "attribute the in-between tool calls and feedback to the right "
            "skill. Skipping this leaves the window open until the next "
            "re-load or the query end, which makes usage analytics unreliable. "
            "Call with no name to list skills with mounted checks."
        ),
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the skill to unload checks for",
                },
            },
        },
        executor=_unload_skill,
        tags=["skill", "memory"],
        impact_scope="memory",
    )


# ------------------------------------------------------------------
# Skill review tool (doc/54 §4.3, §5 P0-5) — read-only ledger digest
# ------------------------------------------------------------------


def make_skill_review_tool(
    ledger_store: "LedgerStore | None",
) -> ToolDefinition:
    """Create a SAFE ``skill_review`` tool — pull per-skill usage history.

    Conversational entry point for the optimization loop described in
    doc/54 §4.3: user asks "what happened with skill X recently?", agent
    calls this, agent reads digest, discusses, edits SKILL.md.

    Read-only. Backed by :func:`query_skill_ledger` — same query layer the
    weekly worker (doc/54 §4.2) will reuse.
    """
    from loom.core.skill_review import query_skill_ledger, render_digest_as_text

    async def _skill_review(call: ToolCall) -> ToolResult:
        skill_id = (call.args.get("skill_id") or "").strip()
        if not skill_id:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error="skill_id required (the skill name to review)",
            )
        if ledger_store is None:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error="Ledger is not available in this session; cannot review.",
            )

        days = call.args.get("days", 7)
        try:
            days_f = float(days)
        except (TypeError, ValueError):
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error=f"'days' must be a number; got {days!r}",
            )
        if days_f <= 0:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="'days' must be positive",
            )

        max_episodes = int(call.args.get("max_episodes", 30))
        max_events = int(call.args.get("max_events_per_episode", 30))

        since_ts = time.time() - days_f * 86400.0
        digest = await query_skill_ledger(
            ledger_store,
            skill_id,
            since_ts=since_ts,
            max_episodes=max_episodes,
            max_events_per_episode=max_events,
        )
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True,
            output=render_digest_as_text(digest),
        )

    return ToolDefinition(
        name="skill_review",
        description=(
            "Pull a per-skill usage digest from the event ledger over a "
            "recent time window. Use this when reviewing how a skill has "
            "been performing, or before discussing SKILL.md updates. "
            "Read-only — does not modify any skill."
        ),
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": "Skill name to review (e.g. 'code_weaver').",
                },
                "days": {
                    "type": "number",
                    "description": "Window size in days (default 7).",
                    "default": 7,
                },
                "max_episodes": {
                    "type": "integer",
                    "description": "Cap on episodes returned (default 30).",
                    "default": 30,
                },
                "max_events_per_episode": {
                    "type": "integer",
                    "description": "Cap on per-episode follow-on events (default 30).",
                    "default": 30,
                },
            },
            "required": ["skill_id"],
        },
        executor=_skill_review,
        tags=["skill", "review", "read-only"],
        impact_scope="read-only",
    )
