"""Shared interaction-language helpers for Loom UI surfaces.

This module is intentionally platform-adjacent rather than core session logic:
it translates runtime events into user-facing labels and display states for CLI
and Discord without teaching the LLM how to author system status text.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from loom.core.envelope_outcome import EnvelopeOutcome, derive_envelope_outcome
from loom.core.security.sandbox_runtime import extract_command_root

# Re-exported so UI consumers (Discord bot, tests) keep their existing
# ``from loom.platform.interaction_language import EnvelopeOutcome``
# import shape. The canonical home is ``loom.core.envelope_outcome`` —
# the ledger projector needs the enum at projection time and core
# cannot import from platform (CLAUDE.md layering).
__all__ = [
    "EnvelopeOutcome",
    "derive_envelope_outcome",
]


class HeartbeatState(str, Enum):
    """Runtime heartbeat states.

    The spec also mentions a ``compacting`` state, but slice 1 keeps compaction
    display on the legacy ``FooterState.compacting`` flag (``⚡ 壓縮中…``) to avoid
    expanding the wiring surface. ``compacting`` will be re-added here when
    ``CompressStarted`` is wired into heartbeat in a later slice.
    """

    IDLE = "idle"
    THINKING = "thinking"
    TOOLING = "tooling"
    LONG_TOOLING = "long_tooling"
    STALLED = "stalled"
    PAUSED_BLOCKING = "paused_blocking"


class ParallelReason(str, Enum):
    SERIAL = "serial"
    FAN_OUT_REPLICAS = "fan_out_replicas"
    FAN_OUT_INDEPENDENT = "fan_out_independent"
    COMPETING_STRATEGIES = "competing_strategies"
    MULTI_TARGET = "multi_target"
    UNSPECIFIED = "unspecified"


@dataclass(frozen=True)
class ToolAction:
    label: str
    subject: str = ""
    stale_after_s: float = 30.0
    long_after_s: float = 30.0


_LONG_RUNNER_THRESHOLD_S = 90.0

# Prompt contract for envelope-level intent and outcome.
# Lives next to the data and UI behaviour it instructs about so prompt_stack
# stays a pure layering mechanism. Future localization follows the same
# registry pattern as the label dicts below.
INTERACTION_LANGUAGE_INSTRUCTIONS = (
    "When you dispatch a multi-tool batch, provide a one-line intent before "
    "the batch and an outcome judgement after the batch completes. "
    "Single-tool calls do not need an intent header. Keep both lines short "
    "enough to display in one UI line."
)

_LABELS_ZH_TW: dict[str, tuple[str, ...]] = {
    "read_file": ("查詢檔案",),
    "write_file": ("寫入檔案",),
    "edit_file": ("編輯檔案",),
    "edit": ("編輯檔案",),
    "run_bash": ("執行指令",),
    "grep": ("搜尋",),
    "ripgrep": ("搜尋",),
    "find": ("列檔",),
    "glob": ("列檔",),
    "list_dir": ("列出目錄",),
    "gitnexus": ("查詢 GitNexus",),
    "memorize": ("寫入記憶",),
    "governed_write": ("寫入記憶",),
    "task_write": ("更新任務",),
    "impact_analysis": ("分析影響",),
    "compact": ("整理 context",),
}

_PARALLEL_REASON_LABELS_ZH_TW: dict[str, str] = {
    ParallelReason.FAN_OUT_REPLICAS.value: "多次驗證",
    ParallelReason.FAN_OUT_INDEPENDENT.value: "並行調查",
    ParallelReason.COMPETING_STRATEGIES.value: "多策略對比",
    ParallelReason.MULTI_TARGET.value: "多目標處理",
}


def _truncate(value: Any, *, max_len: int = 48) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _command_root(command: str) -> str:
    # Reuse sandbox_runtime's parser so heartbeat labels stay aligned with how
    # the same command would be recognised by the sandbox layer. It skips
    # leading KEY=value env assignments (e.g. ``GH_HOST=github.com gh ...``)
    # and returns the executable basename, both of which we want for display.
    return extract_command_root(command) or ""


def _first_string_arg(args: dict[str, Any]) -> str:
    for value in args.values():
        if isinstance(value, str) and value.strip():
            return _truncate(value)
    return ""


def stale_threshold_for_tool(tool_name: str) -> float:
    if tool_name == "run_bash":
        return _LONG_RUNNER_THRESHOLD_S
    if tool_name.startswith("gitnexus_"):
        return _LONG_RUNNER_THRESHOLD_S
    if tool_name in {"pytest", "compact"}:
        return _LONG_RUNNER_THRESHOLD_S
    return 30.0


def format_parallel_reason(reason: str) -> str:
    return _PARALLEL_REASON_LABELS_ZH_TW.get(reason, "")


def synthesize_envelope_intent(*, parallel_reason: str, tool_names: list[str]) -> str:
    dominant = tool_names[0] if tool_names else "工具"
    if parallel_reason == ParallelReason.FAN_OUT_REPLICAS.value:
        return f"重複跑同一組 {dominant}"
    label = format_parallel_reason(parallel_reason)
    return f"{label} · {dominant}" if label else f"執行 {dominant}"


def resolve_tool_action(tool_name: str, args: dict[str, Any] | None = None) -> ToolAction:
    args = args or {}
    stale_after = stale_threshold_for_tool(tool_name)

    if tool_name == "read_file":
        return ToolAction(_LABELS_ZH_TW["read_file"][0], _truncate(args.get("path")), stale_after)
    if tool_name == "write_file":
        return ToolAction(_LABELS_ZH_TW["write_file"][0], _truncate(args.get("path")), stale_after)
    if tool_name in {"edit_file", "edit"}:
        return ToolAction(_LABELS_ZH_TW[tool_name][0], _truncate(args.get("path")), stale_after)
    if tool_name == "run_bash":
        return ToolAction(_LABELS_ZH_TW["run_bash"][0], _command_root(str(args.get("command") or "")), stale_after)
    if tool_name in {"grep", "ripgrep"}:
        return ToolAction(_LABELS_ZH_TW[tool_name][0], _truncate(args.get("pattern")), stale_after)
    if tool_name in {"find", "glob"}:
        return ToolAction(_LABELS_ZH_TW[tool_name][0], _truncate(args.get("pattern")), stale_after)
    if tool_name == "list_dir":
        return ToolAction(_LABELS_ZH_TW["list_dir"][0], _truncate(args.get("path")), stale_after)
    if tool_name.startswith("gitnexus_"):
        return ToolAction(_LABELS_ZH_TW["gitnexus"][0], tool_name.removeprefix("gitnexus_"), stale_after)
    if tool_name in {"memorize", "governed_write"}:
        return ToolAction(_LABELS_ZH_TW[tool_name][0], _truncate(args.get("type")), stale_after)
    if tool_name == "task_write":
        return ToolAction(_LABELS_ZH_TW["task_write"][0], "", stale_after)
    if tool_name == "impact_analysis":
        return ToolAction(_LABELS_ZH_TW["impact_analysis"][0], _truncate(args.get("target")), stale_after)
    if tool_name == "compact":
        return ToolAction(_LABELS_ZH_TW["compact"][0], "", stale_after)
    if "__" in tool_name:
        return ToolAction(tool_name, _first_string_arg(args), stale_after)
    return ToolAction(tool_name, "", stale_after)


def format_elapsed(seconds: float) -> str:
    seconds_i = max(0, int(seconds))
    if seconds_i < 60:
        return f"{seconds_i}s"
    minutes, sec = divmod(seconds_i, 60)
    return f"{minutes}:{sec:02d}"
