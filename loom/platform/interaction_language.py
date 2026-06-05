"""Shared interaction-language helpers for Loom UI surfaces.

This module is intentionally platform-adjacent rather than core session logic:
it translates runtime events into user-facing labels and display states for CLI
and Discord without teaching the LLM how to author system status text.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from loom.core.envelope_outcome import (
    INTERACTION_LANGUAGE_INSTRUCTIONS,
    EnvelopeOutcome,
    derive_envelope_outcome,
)
from loom.core.security.sandbox_runtime import extract_command_root

# Re-exported so UI consumers (Discord bot, tests, prompt_stack) keep
# their existing ``from loom.platform.interaction_language import …``
# import shape. The canonical home is ``loom.core.envelope_outcome`` —
# both the ledger projector AND PromptStack need this vocabulary and
# core cannot import from platform (CLAUDE.md layering).
__all__ = [
    "EnvelopeOutcome",
    "INTERACTION_LANGUAGE_INSTRUCTIONS",
    "LivenessSensor",
    "derive_envelope_outcome",
    "stale_threshold_for_tool",
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


# Action families: the animation identity axis, orthogonal to HeartbeatState
# (which drives stall/suppression semantics). ``resolve_tool_action`` tags
# each tool with a family; the footer picks one variant from the family pool
# per tool call (rotating for freshness) and holds it for that call's
# duration. Family names are stable contract values — the CLI footer and
# tests key off them.
class ActionFamily(str, Enum):
    THINK = "think"        # reasoning between tool calls
    PROBE = "probe"        # read / search / inspect (read-only)
    WRITE = "write"        # file + memory + task mutation
    EXECUTE = "execute"    # run_bash and shell-shaped work
    TIDY = "tidy"          # compaction / housekeeping
    TOOL = "tool"          # generic fallback (MCP, unknown tools)


# family -> ordered pool of animation names defined in
# ``loom.platform.cli.ui._ANIMATION_FRAMES``. Order is the rotation order.
ANIMATION_FAMILIES: dict[str, tuple[str, ...]] = {
    ActionFamily.THINK.value:   ("breathing_focus", "dots_grow"),
    ActionFamily.PROBE.value:   ("classic_spinner", "dots_wobble", "dots_corner"),
    ActionFamily.WRITE.value:   ("pen_stroke", "rising_columns"),
    ActionFamily.EXECUTE.value: ("dots_heavy", "dots_edge"),
    ActionFamily.TIDY.value:    ("cascade_drop",),
    ActionFamily.TOOL.value:    ("classic_spinner",),
}


def family_variants(family: str) -> tuple[str, ...]:
    """Animation pool for a family, falling back to the generic TOOL pool."""
    return ANIMATION_FAMILIES.get(family, ANIMATION_FAMILIES[ActionFamily.TOOL.value])


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
    # Animation family this action belongs to (drives the footer's
    # variant pick). Defaults to the generic TOOL pool.
    family: str = ActionFamily.TOOL.value


_LONG_RUNNER_THRESHOLD_S = 90.0

# ``INTERACTION_LANGUAGE_INSTRUCTIONS`` lives in ``loom.core.envelope_outcome``
# now and is re-exported above. PromptStack (core/cognition) needed it
# at layer-injection time; keeping it in core also keeps the architecture
# guard happy (#423).

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

    _PROBE = ActionFamily.PROBE.value
    _WRITE = ActionFamily.WRITE.value

    if tool_name == "read_file":
        return ToolAction(_LABELS_ZH_TW["read_file"][0], _truncate(args.get("path")), stale_after, family=_PROBE)
    if tool_name == "write_file":
        return ToolAction(_LABELS_ZH_TW["write_file"][0], _truncate(args.get("path")), stale_after, family=_WRITE)
    if tool_name in {"edit_file", "edit"}:
        return ToolAction(_LABELS_ZH_TW[tool_name][0], _truncate(args.get("path")), stale_after, family=_WRITE)
    if tool_name == "run_bash":
        return ToolAction(
            _LABELS_ZH_TW["run_bash"][0], _command_root(str(args.get("command") or "")), stale_after,
            family=ActionFamily.EXECUTE.value,
        )
    if tool_name in {"grep", "ripgrep"}:
        return ToolAction(_LABELS_ZH_TW[tool_name][0], _truncate(args.get("pattern")), stale_after, family=_PROBE)
    if tool_name in {"find", "glob"}:
        return ToolAction(_LABELS_ZH_TW[tool_name][0], _truncate(args.get("pattern")), stale_after, family=_PROBE)
    if tool_name == "list_dir":
        return ToolAction(_LABELS_ZH_TW["list_dir"][0], _truncate(args.get("path")), stale_after, family=_PROBE)
    if tool_name.startswith("gitnexus_"):
        return ToolAction(_LABELS_ZH_TW["gitnexus"][0], tool_name.removeprefix("gitnexus_"), stale_after, family=_PROBE)
    if tool_name in {"memorize", "governed_write"}:
        return ToolAction(_LABELS_ZH_TW[tool_name][0], _truncate(args.get("type")), stale_after, family=_WRITE)
    if tool_name == "task_write":
        return ToolAction(_LABELS_ZH_TW["task_write"][0], "", stale_after, family=_WRITE)
    if tool_name == "impact_analysis":
        return ToolAction(_LABELS_ZH_TW["impact_analysis"][0], _truncate(args.get("target")), stale_after, family=_PROBE)
    if tool_name == "compact":
        return ToolAction(_LABELS_ZH_TW["compact"][0], "", stale_after, family=ActionFamily.TIDY.value)
    if "__" in tool_name:
        return ToolAction(tool_name, _first_string_arg(args), stale_after)
    return ToolAction(tool_name, "", stale_after)


def format_elapsed(seconds: float) -> str:
    seconds_i = max(0, int(seconds))
    if seconds_i < 60:
        return f"{seconds_i}s"
    minutes, sec = divmod(seconds_i, 60)
    return f"{minutes}:{sec:02d}"


class LivenessSensor:
    """Pure, platform-agnostic runtime-stall sensor.

    Owns the single observation timeline that both the CLI footer heartbeat
    (#418/#419) and the Discord stall watchdog (#422) previously open-coded
    in divergent ways. The sensor consumes ONLY monotonic timestamps and
    caller-supplied progress fingerprints — it never imports prompt_toolkit,
    discord, or inspects any rendered/debounced UI variable. That is the
    structural guard against codex #422's bug class: observed-vs-displayed is
    decided here by comparing against the sensor's OWN stored observed
    fingerprint, never against what a surface happens to have painted.

    State owned:

    - ``_last_observed``: monotonic timestamp of the last reset (a genuine
      observable event).
    - ``_last_fingerprint``: the render-agnostic string the surface computed
      for the most-recently OBSERVED state. ``observe`` compares against this
      to decide whether an event carries new progress.
    - ``_stalled_emitted``: emit-once latch for the Discord watchdog
      semantics (``should_emit_stalled``). The CLI footer reads the
      non-latching ``is_stalled`` instead.
    - ``_suppressed``: pause-window guard (CLI PAUSED_BLOCKING / Discord
      ``_stall_watchdog_suppressed``).
    - ``_active_threshold_s``: the per-tool quiet threshold currently in
      force (set via ``set_threshold``).

    Two read methods sit over one observation state. ``should_emit_stalled``
    latches (Discord, emit-once-per-quiet-stretch); ``is_stalled`` does not
    (CLI footer re-renders the prefix every redraw). Both share the same
    threshold + suppression + observation-timeline arithmetic.

    The monotonic ``clock`` is injectable so tests pass synthetic readings
    instead of sleeping.
    """

    def __init__(
        self,
        *,
        default_threshold_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._default_threshold_s = default_threshold_s
        self._active_threshold_s = default_threshold_s
        self._last_observed = clock()
        self._last_fingerprint: str | None = None
        self._stalled_emitted = False
        self._suppressed = False

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now

    @property
    def stalled_emitted(self) -> bool:
        """Whether a stall line has been emitted for the current quiet stretch.

        True between a ``should_emit_stalled`` firing and the next ``observe``
        that resets the clock. Discord reads this to drive the overlay-clobber
        edit gate (whether re-painting would erase the 'still waiting' line)
        without re-deriving the latch by hand.
        """
        return self._stalled_emitted

    def observe(
        self,
        *,
        now: float | None = None,
        fingerprint: str | None = None,
        is_progress: bool = True,
    ) -> bool:
        """Record a stream event against the observation timeline.

        Generalizes the old ``_event_resets_stall_clock`` helper. The surface
        decides per-event whether the event is a candidate for resetting the
        clock:

        - ``is_progress=False`` for events that are silent-by-design (e.g.
          Discord ``ActionStateChange``) — never resets.
        - ``fingerprint`` is the render-agnostic string the surface computed
          for the current observable state. The clock resets ONLY when
          ``is_progress`` is True AND (``fingerprint`` is None OR
          ``fingerprint != self._last_fingerprint``). This rejects the
          synthetic ~1Hz no-op ``EnvelopeUpdated`` ticks whose fingerprint is
          unchanged.

        When a fingerprint is supplied it is stored regardless of whether the
        clock reset, so the next comparison sees the latest observed state.

        Returns True when the clock was reset (callers can mirror old
        ``if reset:`` branches).
        """
        if fingerprint is not None:
            changed = fingerprint != self._last_fingerprint
            self._last_fingerprint = fingerprint
        else:
            changed = True
        if not (is_progress and changed):
            return False
        self._last_observed = self._now(now)
        self._stalled_emitted = False
        return True

    def should_emit_stalled(self, *, now: float | None = None) -> bool:
        """Latching stall check for the Discord watchdog.

        Generalizes ``_should_emit_stalled_status``. Returns False if
        suppressed (pause window) or if the already-emitted latch is set;
        otherwise returns whether quiet time has reached the active threshold.
        On a True result it sets the latch internally so the caller gets
        emit-once-per-quiet-stretch without managing the latch by hand.
        """
        if self._suppressed or self._stalled_emitted:
            return False
        if self.quiet_for(now=now) >= self._active_threshold_s:
            self._stalled_emitted = True
            return True
        return False

    def is_stalled(self, *, now: float | None = None) -> bool:
        """Continuous (non-latching) stall check for the CLI footer.

        Returns False if suppressed; otherwise whether quiet time has reached
        the active threshold. Does NOT touch the latch — the CLI footer
        re-renders the 'still waiting' prefix on every redraw rather than
        emitting once.
        """
        if self._suppressed:
            return False
        return self.quiet_for(now=now) >= self._active_threshold_s

    def quiet_for(self, *, now: float | None = None) -> float:
        """Seconds since the last observed event, clamped to >= 0.0."""
        return max(0.0, self._now(now) - self._last_observed)

    def set_threshold(
        self,
        *,
        tool_name: str | None = None,
        seconds: float | None = None,
    ) -> None:
        """Set the active quiet threshold.

        With ``tool_name``, looks it up via ``stale_threshold_for_tool`` (the
        existing per-tool seam shared with the CLI). With explicit
        ``seconds``, sets directly. With neither, resets to the default.
        """
        if tool_name is not None:
            self._active_threshold_s = stale_threshold_for_tool(tool_name)
        elif seconds is not None:
            self._active_threshold_s = seconds
        else:
            self._active_threshold_s = self._default_threshold_s

    def suppress(self) -> None:
        """Enter the pause-suppression window (no stall emitted while set)."""
        self._suppressed = True

    def resume(self, *, now: float | None = None) -> None:
        """Leave the pause-suppression window.

        Clears the suppressed flag AND resets the observation clock + latch so
        the post-resume window does not inherit pre-pause quiet time. Bind to
        the TurnPaused try/finally as a matched pair with ``suppress`` per
        Loom's shared-session preemption-race convention.
        """
        self._suppressed = False
        self._last_observed = self._now(now)
        self._stalled_emitted = False

    def reset(self, *, now: float | None = None) -> None:
        """Reset to a fresh quiet stretch.

        ``last_observed=now``, clear the latch, clear suppression, and return
        the threshold to the default. Used at turn start (Discord turn-begin
        init; CLI ``start_heartbeat`` for a new state).
        """
        self._last_observed = self._now(now)
        self._last_fingerprint = None
        self._stalled_emitted = False
        self._suppressed = False
        self._active_threshold_s = self._default_threshold_s
