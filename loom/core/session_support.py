"""Stateless helpers extracted from ``LoomSession``.

These are pure module-level functions and constants that never touched
``self`` — they only ever lived in ``session.py`` for historical reasons and
several of them already leaked out as a de-facto public API (imported by
``loom.platform.cli.tools``, ``loom.core.diagnostic.startup`` and the test
suite). Pulling them here tightens that surface and shrinks the
``LoomSession`` module without changing any behaviour.

``session.py`` re-exports everything in this module, so the historical import
paths (``from loom.core.session import _load_env`` etc.) keep working.

Grouped by concern:

  * Envelope metadata markers — parse the ``▸ intent`` / ``✓ outcome`` lines
    the agent embeds in its final text for the UI projection layer.
  * Think-block streaming — split-tag lookahead for ``<think>`` filtering.
  * Output-token resolution — per-model output cap precedence (#272).
  * Environment / config loading — ``.env`` discovery.
  * Skill frontmatter parsing — YAML header of a ``SKILL.md``.
  * Background jobs reminder — pre-final-response job-status injection (#154).
  * Scope panels — confirm-prompt formatting (Rich + plain variants).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import dotenv_values

if TYPE_CHECKING:
    from loom.core.cognition.router import LLMRouter
    from loom.core.harness.middleware import ToolCall


# ---------------------------------------------------------------------------
# Envelope metadata markers
# ---------------------------------------------------------------------------

_OUTCOME_MARKERS: dict[str, str] = {
    "✓": "fulfilled",
    "◐": "partial",
    "⚠": "unfulfilled",
    "↪": "pivoted",
    "🛑": "aborted",
}


def _clean_envelope_metadata_line(text: str, *, max_len: int = 140) -> str:
    line = " ".join(str(text or "").strip().split())
    if len(line) <= max_len:
        return line
    return line[: max_len - 1] + "…"


def _iter_nonempty_lines(text: str) -> Iterator[str]:
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            yield line


def _extract_envelope_intent_marker(text: str) -> str:
    for line in _iter_nonempty_lines(text):
        if line.startswith("▸"):
            return _clean_envelope_metadata_line(line.removeprefix("▸"))
    return ""


def _extract_envelope_outcome_marker(text: str) -> tuple[str, str]:
    for line in _iter_nonempty_lines(text):
        marker = line[0]
        outcome = _OUTCOME_MARKERS.get(marker)
        if outcome is None:
            continue
        summary = _clean_envelope_metadata_line(line[1:].lstrip("\ufe0f"))
        return outcome, summary
    return "", ""


# ---------------------------------------------------------------------------
# Think-block filter helpers
# ---------------------------------------------------------------------------


def _find_partial_tag_suffix(text: str, tag: str) -> int:
    """Return the length of the longest suffix of *text* that is a prefix of *tag*.

    Used to detect `<think>` / `</think>` tags split across streaming chunks so
    we can hold the partial tag in a lookahead buffer instead of emitting it.
    """
    for n in range(min(len(tag) - 1, len(text)), 0, -1):
        if text[-n:] == tag[:n]:
            return n
    return 0


# ---------------------------------------------------------------------------
# Output-token resolution
# ---------------------------------------------------------------------------

# Issue #181: per-model output cap. Different providers have very different
# output ceilings (MiniMax-M2.7 ~64K, Claude Sonnet 4.6 ~64K, smaller local
# models often 4-8K) so a single hardcoded value was either wasteful or
# clipping long tool-loop turns. Resolved once at session start.
_DEFAULT_OUTPUT_MAX_TOKENS = 8192

# Issue #271: max consecutive max_tokens-with-zero-tools recoveries within a
# single stream_turn() before falling through to TurnDropped. Two attempts
# is empirically enough for deep-reasoning prompts (10-question deductive
# quizzes) without risking unbounded loops on pathologically long inputs.
_MAX_REASONING_CONTINUATIONS = 2


def _resolve_output_max_tokens(
    cfg: dict, model: str, router: "LLMRouter | None" = None,
) -> int:
    """Return the output-token cap for *model*.

    Precedence (Issue #272):
      1. ``[cognition.output_max_tokens_overrides][<model>]`` — explicit
         user override, case-sensitive (intentional: user typed it).
      2. ``[cognition].output_max_tokens`` — global user-set cap.
      3. **Provider-declared native limit** — provider class knows what its
         own models support. Lookup is case-insensitive. New models become
         available without loom.toml edits.
      4. ``_DEFAULT_OUTPUT_MAX_TOKENS`` — last-resort fallback.

    The provider-native step (3) is the one that lets users drop the
    overrides table entirely once their models are in the provider's
    ``NATIVE_OUTPUT_LIMITS``.
    """
    cog = cfg.get("cognition", {}) or {}
    overrides = cog.get("output_max_tokens_overrides", {}) or {}
    if model in overrides:
        try:
            return int(overrides[model])
        except (TypeError, ValueError):
            pass
    default = cog.get("output_max_tokens")
    if default is not None:
        try:
            return int(default)
        except (TypeError, ValueError):
            pass
    # Step 3 — ask the provider what its native ceiling is for this model.
    if router is not None:
        try:
            provider = router.get_provider(model)
            native = provider.native_max_tokens(model)
            if native is not None and native > 0:
                return int(native)
        except Exception:
            # Routing failures or missing provider shouldn't break the call;
            # fall through to the constant default.
            pass
    return _DEFAULT_OUTPUT_MAX_TOKENS


# ---------------------------------------------------------------------------
# Environment / config loading
# ---------------------------------------------------------------------------


def _load_env(project_root: Path | None = None) -> dict[str, str]:
    """Load .env from project root or current directory."""
    search = [
        Path.cwd() / ".env",
        Path(__file__).parents[2] / ".env",
    ]
    if project_root:
        search.insert(0, project_root / ".env")

    for path in search:
        if path.exists():
            return dict(dotenv_values(str(path)))
    return {}


# ---------------------------------------------------------------------------
# Skill frontmatter parsing
# ---------------------------------------------------------------------------


def _parse_skill_frontmatter(
    raw: str,
) -> tuple[str, str, list[str], list[dict], int | None]:
    """
    Parse YAML frontmatter from a SKILL.md file.

    Returns ``(name, description, tags, precondition_check_refs, model_tier)``.
    On parse failure returns empty strings / lists / ``None``.
    Follows Agent Skills spec lenient validation: best-effort parsing.

    Issue #276: ``model_tier`` is an optional positive int declaring the
    LLM tier this skill expects (1 = daily, 2 = deep reasoning, …). The
    harness escalates the active model when the skill is loaded. ``None``
    means "no opinion".
    """
    if not raw.startswith("---"):
        return "", "", [], [], None

    parts = raw.split("---", 2)
    if len(parts) < 3:
        return "", "", [], [], None

    yaml_text = parts[1].strip()
    try:
        import yaml
        data = yaml.safe_load(yaml_text)
        if not isinstance(data, dict):
            return "", "", [], [], None
    except Exception:
        return "", "", [], [], None

    name = str(data.get("name", "")).strip()
    description = str(data.get("description", "")).strip()
    tags = data.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    elif not isinstance(tags, list):
        tags = []

    # Issue #64 Phase B: skill-declared precondition checks
    pc_refs = data.get("precondition_checks", [])
    if not isinstance(pc_refs, list):
        pc_refs = []
    # Validate each entry has at minimum 'ref' and 'applies_to'
    valid_refs: list[dict] = []
    for entry in pc_refs:
        if isinstance(entry, dict) and entry.get("ref") and entry.get("applies_to"):
            valid_refs.append(entry)

    # Issue #276: skill-declared LLM tier requirement
    raw_tier = data.get("model_tier")
    model_tier: int | None
    try:
        model_tier = int(raw_tier) if raw_tier is not None else None
        if model_tier is not None and model_tier < 1:
            model_tier = None  # invalid; treat as unspecified
    except (TypeError, ValueError):
        model_tier = None

    return name, description, tags, valid_refs, model_tier


# ---------------------------------------------------------------------------
# Background jobs reminder
# ---------------------------------------------------------------------------


def _build_jobs_inject_message(jobstore: Any) -> str | None:
    """Issue #154: render a pre-final-response reminder about background jobs.

    Returns None when there is nothing to report — no new completions and no
    still-running jobs. Only items finished since the last call are listed
    under 'Completed since last turn' (JobStore.reap_since_last is idempotent).
    """
    new_finished, running = jobstore.reap_since_last()
    if not new_finished and not running:
        return None

    lines = ["[Jobs update]"]
    if new_finished:
        lines.append("Completed since last turn:")
        for j in new_finished:
            if j.state.value == "done":
                ref = f"scratchpad://{j.result_ref}" if j.result_ref else "(no output)"
                size = f" ({j.result_summary})" if j.result_summary else ""
                lines.append(f"  - {j.id} ({j.fn_name}): done → {ref}{size}")
            elif j.state.value == "failed":
                lines.append(f"  - {j.id} ({j.fn_name}): failed — {j.error}")
            elif j.state.value == "cancelled":
                lines.append(f"  - {j.id} ({j.fn_name}): cancelled — {j.cancel_reason}")
    if running:
        if new_finished:
            lines.append("")
        lines.append("Still running:")
        for j in running:
            elapsed = j.elapsed_seconds
            elapsed_str = f" ({elapsed:.0f}s elapsed)" if elapsed else ""
            lines.append(f"  - {j.id} ({j.fn_name}): {j.state.value}{elapsed_str}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scope panels (confirm-prompt formatting)
# ---------------------------------------------------------------------------


def _format_scope_panel(call: "ToolCall") -> str:
    """
    Build a Rich-formatted string describing scope verdict + diff.

    Phase C (Issue #45): when scope metadata is available, the confirm
    prompt shows structured information instead of raw args.
    """
    from loom.core.harness.scope import (
        DiffReason, PermissionVerdict, ScopeDiff, ScopeRequest,
    )

    verdict = call.metadata.get("scope_verdict")
    diff: ScopeDiff | None = call.metadata.get("scope_diff")
    scope_req: ScopeRequest | None = call.metadata.get("scope_request")

    if verdict is None or scope_req is None:
        # Legacy path — no scope metadata available.
        # Format args with smart truncation so long paths remain readable.
        arg_lines: list[str] = []
        for k, v in call.args.items():
            if isinstance(v, str):
                display = v if len(v) <= 120 else v[:40] + "…" + v[-40:]
                arg_lines.append(f"  [cyan]{k}[/cyan]: {display}")
            else:
                arg_lines.append(f"  [cyan]{k}[/cyan]: {v!r}")
        args_display = "\n".join(arg_lines) if arg_lines else "  (no args)"
        return (
            f"[#c8a464]{call.tool_name}[/#c8a464]  {call.trust_level.label}\n"
            f"{args_display}"
        )

    # --- Verdict-aware header ---
    _REASON_LABELS: dict[DiffReason, str] = {
        DiffReason.FIRST_TIME: "First time accessing this resource",
        DiffReason.SELECTOR_EXPANSION: "Expanding beyond previously approved scope",
        DiffReason.CONSTRAINT_EXPANSION: "Exceeding previously approved limits",
        DiffReason.RESOURCE_TYPE_NEW: "New resource type not previously authorized",
    }

    _VERDICT_TITLES: dict[PermissionVerdict, tuple[str, str]] = {
        PermissionVerdict.CONFIRM: ("Tool requires confirmation", "yellow"),
        PermissionVerdict.EXPAND_SCOPE: ("Scope expansion required", "red"),
    }
    title_text, border = _VERDICT_TITLES.get(
        verdict, ("Tool requires confirmation", "yellow"),
    )

    lines: list[str] = [
        f"[#c8a464]{call.tool_name}[/#c8a464]  {call.trust_level.label}",
    ]

    # Scope summary: what the tool wants to access
    for req in scope_req.requirements:
        lines.append(
            f"  [cyan]{req.resource}[/cyan]:{req.action} → "
            f"[bold]{req.selector}[/bold]"
        )
        if req.constraints.get("scope_unknown"):
            lines.append("  [yellow]⚠ scope could not be fully resolved[/yellow]")

    # Diff reason
    if diff is not None and not diff.is_fully_covered:
        reason_label = _REASON_LABELS.get(diff.reason, str(diff.reason.value))
        lines.append(f"\n[dim]{reason_label}[/dim]")

        # Show what's already covered vs missing
        if diff.covered:
            covered_selectors = ", ".join(r.selector for r in diff.covered)
            lines.append(f"[green]✓ covered:[/green] {covered_selectors}")
        if diff.missing:
            missing_selectors = ", ".join(r.selector for r in diff.missing)
            lines.append(f"[yellow]● new:[/yellow] {missing_selectors}")

    return "\n".join(lines)


def _format_scope_summary_plain(call: "ToolCall") -> str:
    """Plain-text scope summary for the LoomApp confirm widget.

    Mirrors :func:`_format_scope_panel` but strips Rich markup so it
    renders cleanly inside a prompt_toolkit FormattedTextControl,
    which doesn't speak Rich's ``[bold]`` syntax.
    """
    from loom.core.harness.scope import (
        DiffReason, ScopeDiff, ScopeRequest,
    )

    scope_req: ScopeRequest | None = call.metadata.get("scope_request")
    diff: ScopeDiff | None = call.metadata.get("scope_diff")

    if scope_req is None:
        arg_lines: list[str] = []
        for k, v in call.args.items():
            if isinstance(v, str):
                display = v if len(v) <= 120 else v[:40] + "…" + v[-40:]
                arg_lines.append(f"  {k}: {display}")
            else:
                arg_lines.append(f"  {k}: {v!r}")
        return "\n".join(arg_lines) if arg_lines else "(no args)"

    _REASON_LABELS: dict[DiffReason, str] = {
        DiffReason.FIRST_TIME: "First time accessing this resource",
        DiffReason.SELECTOR_EXPANSION: "Expanding beyond previously approved scope",
        DiffReason.CONSTRAINT_EXPANSION: "Exceeding previously approved limits",
        DiffReason.RESOURCE_TYPE_NEW: "New resource type not previously authorized",
    }

    lines: list[str] = []
    for req in scope_req.requirements:
        line = f"  {req.resource}:{req.action} → {req.selector}"
        if req.constraints.get("scope_unknown"):
            line += "  ⚠ scope could not be fully resolved"
        lines.append(line)
    if diff is not None and not diff.is_fully_covered:
        reason_label = _REASON_LABELS.get(diff.reason, str(diff.reason.value))
        lines.append(reason_label)
        if diff.covered:
            lines.append(
                "  ✓ covered: " + ", ".join(r.selector for r in diff.covered)
            )
        if diff.missing:
            lines.append(
                "  ● new: " + ", ".join(r.selector for r in diff.missing)
            )
    return "\n".join(lines)


__all__ = [
    "_OUTCOME_MARKERS",
    "_clean_envelope_metadata_line",
    "_iter_nonempty_lines",
    "_extract_envelope_intent_marker",
    "_extract_envelope_outcome_marker",
    "_find_partial_tag_suffix",
    "_DEFAULT_OUTPUT_MAX_TOKENS",
    "_MAX_REASONING_CONTINUATIONS",
    "_resolve_output_max_tokens",
    "_load_env",
    "_parse_skill_frontmatter",
    "_build_jobs_inject_message",
    "_format_scope_panel",
    "_format_scope_summary_plain",
]
