# UI/UX Interaction Language Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Loom's shared interaction language foundation, starting with runtime heartbeat, envelope intent/outcome metadata, and CLI/Discord rendering alignment.

**Architecture:** Add small core view-model helpers in `loom.core.events` / a focused interaction-language module, then wire existing CLI and Discord consumers to those helpers. Keep the vertical-flow constraint: no side panes, no dashboard grid, no revived TUI. First implementation slice proves the grammar in CLI heartbeat and Discord status messages while preserving existing TaskList behavior.

**Tech Stack:** Python dataclasses, prompt_toolkit `FormattedText`, Rich console rendering, Discord.py message edits / typing indicator, pytest.

---

## Pre-Execution Notes

This plan is based on [2026-05-20-ui-ux-interaction-language-design.md](/Users/tsaidakai/Loom/Loom/docs/superpowers/specs/2026-05-20-ui-ux-interaction-language-design.md).

Before editing any function, class, or method, follow `AGENTS.md`: run GitNexus impact analysis for the exact symbol being modified and report blast radius. If GitNexus warns that the index is stale, run `npx gitnexus analyze` first.

Recommended execution style:

1. Keep each task as a small commit.
2. Add tests before implementation.
3. Preserve user edits in the current worktree.
4. Run `npx gitnexus detect-changes --scope staged` before every commit.

## File Structure

Create:

- `loom/platform/interaction_language.py`  
  Shared UX grammar helpers: heartbeat state enum, action-label resolver, elapsed formatting, stale threshold configuration, envelope outcome enums. This keeps rendering logic out of `loom.core.events` and avoids putting platform text rules into the session runtime.

- `tests/test_interaction_language.py`  
  Unit coverage for label resolution, elapsed formatting, stale thresholds, and envelope outcome helpers.

Modify:

- `loom/core/events.py`  
  Add additive fields to `ExecutionEnvelopeView` and update Consumer Map / docstrings.

- `loom/platform/cli/app.py`  
  Replace `_ActiveEnvelope` footer display with a heartbeat state model while preserving existing footer geometry, compaction override, grants, context pressure, tier badge, and last-turn stats.

- `loom/platform/cli/main.py`  
  Update stream event handling so `ToolBegin`, `ToolEnd`, `TurnDone`, `CompressDone`, `TurnPaused`, and turn start drive heartbeat transitions.

- `loom/platform/discord/bot.py`  
  Update `_format_envelope_status()` and `_run_turn()` status message behavior to render envelope intent/outcome and one-time stalled status edits.

- `tests/test_app.py`  
  Replace footer active-envelope assertions with heartbeat assertions.

- `tests/test_event_consumer_map.py`  
  Update expectations when new stream events are introduced.

- `tests/test_discord_interaction_language.py`  
  Pure-function coverage around Discord envelope formatting and stalled-status decisions.

## Task 1: Shared Interaction Language Helpers

**Files:**
- Create: `loom/platform/interaction_language.py`
- Create: `tests/test_interaction_language.py`

- [ ] **Step 1: Run impact analysis**

Run GitNexus impact analysis for symbols that consume this module in this plan: `FooterState`, `_render_footer`, `_format_envelope_status`, and `ExecutionEnvelopeView`.

Expected: low to medium risk. If any symbol reports HIGH or CRITICAL risk, stop and summarize the blast radius before editing code.

- [ ] **Step 2: Write failing tests for elapsed formatting and label resolution**

Add `tests/test_interaction_language.py`:

```python
from __future__ import annotations

from loom.platform.interaction_language import (
    HeartbeatState,
    _LABELS_ZH_TW,
    format_elapsed,
    format_parallel_reason,
    resolve_tool_action,
    synthesize_envelope_intent,
    stale_threshold_for_tool,
)


def test_format_elapsed_under_one_minute_uses_seconds() -> None:
    assert format_elapsed(0.0) == "0s"
    assert format_elapsed(7.2) == "7s"
    assert format_elapsed(59.9) == "59s"


def test_format_elapsed_over_one_minute_uses_mmss() -> None:
    assert format_elapsed(60.0) == "1:00"
    assert format_elapsed(125.4) == "2:05"


def test_resolve_tool_action_for_run_bash_uses_command_root() -> None:
    action = resolve_tool_action("run_bash", {"command": "pytest tests/test_app.py -q"})
    assert action.label == "執行指令"
    assert action.subject == "pytest"


def test_resolve_tool_action_for_read_file_uses_path_subject() -> None:
    action = resolve_tool_action("read_file", {"path": "loom/platform/cli/app.py"})
    assert action.label == "查詢檔案"
    assert action.subject == "loom/platform/cli/app.py"


def test_labels_use_locale_registry_shape() -> None:
    assert _LABELS_ZH_TW["read_file"][0] == "查詢檔案"
    assert _LABELS_ZH_TW["run_bash"][0] == "執行指令"


def test_resolve_tool_action_unknown_tool_falls_back_to_name() -> None:
    action = resolve_tool_action("new_tool", {"value": "abc"})
    assert action.label == "new_tool"
    assert action.subject == ""


def test_stale_threshold_defaults_and_long_runner_override() -> None:
    assert stale_threshold_for_tool("read_file") == 30.0
    assert stale_threshold_for_tool("run_bash") == 90.0
    assert stale_threshold_for_tool("gitnexus_query") == 90.0


def test_heartbeat_state_names_are_stable() -> None:
    assert HeartbeatState.THINKING.value == "thinking"
    assert HeartbeatState.STALLED.value == "stalled"


def test_parallel_reason_formats_as_human_label() -> None:
    assert format_parallel_reason("fan_out_replicas") == "多次驗證"
    assert format_parallel_reason("competing_strategies") == "多策略對比"


def test_synthesize_envelope_intent_uses_parallel_reason_and_dominant_tool() -> None:
    intent = synthesize_envelope_intent(
        parallel_reason="fan_out_replicas",
        tool_names=["pytest", "pytest", "pytest"],
    )
    assert intent == "重複跑同一組 pytest"
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
pytest -q tests/test_interaction_language.py
```

Expected: FAIL with `ModuleNotFoundError: No module named 'loom.platform.interaction_language'`.

- [ ] **Step 4: Implement the helper module**

Create `loom/platform/interaction_language.py`:

```python
"""Shared interaction-language helpers for Loom UI surfaces.

This module is intentionally platform-adjacent rather than core session logic:
it translates runtime events into user-facing labels and display states for CLI
and Discord without teaching the LLM how to author system status text.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from shlex import split as shell_split
from typing import Any


class HeartbeatState(str, Enum):
    """Runtime heartbeat states.

    The spec also mentions a ``compacting`` state, but slice 1 keeps compaction
    display on the legacy ``FooterState.compacting`` flag (``⚡ 壓縮中…``) to avoid
    expanding the wiring surface. ``compacting`` will be re-added here when
    ``CompressStarted`` is wired into heartbeat in a later slice. Do not add it
    to the enum yet — an orphan enum member confuses future readers about what
    is supposed to drive it.
    """

    IDLE = "idle"
    THINKING = "thinking"
    TOOLING = "tooling"
    LONG_TOOLING = "long_tooling"
    STALLED = "stalled"
    PAUSED_BLOCKING = "paused_blocking"


class EnvelopeOutcome(str, Enum):
    FULFILLED = "fulfilled"
    PARTIAL = "partial"
    UNFULFILLED = "unfulfilled"
    PIVOTED = "pivoted"
    ABORTED = "aborted"


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
    try:
        parts = shell_split(command)
    except ValueError:
        parts = command.split()
    return parts[0] if parts else ""


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
```

- [ ] **Step 5: Run tests**

Run:

```bash
pytest -q tests/test_interaction_language.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

Before committing:

```bash
npx gitnexus detect-changes --scope staged
```

Then:

```bash
git add loom/platform/interaction_language.py tests/test_interaction_language.py
git commit -m "feat(ui): add interaction language helpers"
```

## Task 2: Envelope Intent And Outcome Metadata

**Files:**
- Modify: `loom/core/events.py`
- Modify: `tests/test_event_consumer_map.py`
- Modify: `tests/test_ledger_envelope_projector.py`
- Modify or add focused tests near existing envelope view tests

- [ ] **Step 1: Run impact analysis**

Run impact analysis for `ExecutionEnvelopeView`, `EnvelopeStarted`, `EnvelopeCompleted`, and the projector that builds `ExecutionEnvelopeView`.

Expected: medium risk because CLI, Discord, tests, and ledger projector consume envelope view models.

- [ ] **Step 2: Write failing tests for default envelope metadata**

Add to `tests/test_ledger_envelope_projector.py` or the closest existing envelope-view test file:

```python
from loom.platform.interaction_language import ParallelReason


def test_execution_envelope_view_defaults_interaction_metadata() -> None:
    from loom.core.events import ExecutionEnvelopeView

    view = ExecutionEnvelopeView(
        envelope_id="e1",
        session_id="s1",
        turn_index=1,
        status="running",
        node_count=1,
        parallel_groups=1,
    )

    assert view.intent == ""
    assert view.parallel_reason == ParallelReason.UNSPECIFIED.value
    assert view.outcome == ""
    assert view.outcome_summary == ""
```

- [ ] **Step 3: Run the focused test to verify failure**

Run:

```bash
pytest -q tests/test_ledger_envelope_projector.py::test_execution_envelope_view_defaults_interaction_metadata
```

Expected: FAIL because `ExecutionEnvelopeView` has no `intent`, `parallel_reason`, `outcome`, or `outcome_summary`.

- [ ] **Step 4: Add additive dataclass fields**

Modify `loom/core/events.py`:

```python
@dataclass
class ExecutionEnvelopeView:
    """Aggregate view for one tool-use batch — the primary UI unit.

    Built by ``LoomSession._build_envelope_view()`` (projection layer)
    and yielded as part of ``EnvelopeStarted / Updated / Completed``
    stream events.  TUI and Discord both consume this same structure.

    ``intent`` / ``parallel_reason`` / ``outcome`` / ``outcome_summary`` are
    interaction-language metadata. Empty outcome means unknown and must be
    derived by producers or renderers; it never means success.
    """

    envelope_id: str
    session_id: str
    turn_index: int
    status: str
    node_count: int
    parallel_groups: int
    elapsed_ms: float = 0.0
    levels: list[list[str]] = field(default_factory=list)
    nodes: list[ExecutionNodeView] = field(default_factory=list)
    intent: str = ""
    parallel_reason: str = "unspecified"
    outcome: str = ""
    outcome_summary: str = ""
```

Keep these as strings in the dataclass to avoid importing platform helper enums into `loom.core`.

- [ ] **Step 5: Update event docstrings**

In `EnvelopeStarted` docstring, add:

```text
Interaction metadata:
    envelope.intent and envelope.parallel_reason may be displayed by platform
    consumers for multi-node envelopes.
```

In `EnvelopeCompleted` docstring, add:

```text
Interaction metadata:
    envelope.outcome and envelope.outcome_summary describe the batch result.
```

- [ ] **Step 6: Run static event tests**

Run:

```bash
pytest -q tests/test_event_consumer_map.py tests/test_ledger_envelope_projector.py
```

Expected: PASS after updating any constructor expectations.

- [ ] **Step 7: Commit**

Run:

```bash
npx gitnexus detect-changes --scope staged
git add loom/core/events.py tests/test_event_consumer_map.py tests/test_ledger_envelope_projector.py
git commit -m "feat(events): add envelope interaction metadata"
```

## Task 3: CLI Heartbeat State Model

**Files:**
- Modify: `loom/platform/cli/app.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Run impact analysis**

Run impact analysis for `FooterState`, `_ActiveEnvelope`, `_render_footer`, `_render_thinking`, and `LoomApp.invalidate`.

Expected: medium risk because footer rendering is hot and heavily tested.

- [ ] **Step 2: Sweep `_ActiveEnvelope` usage and plan removal**

Run:

```bash
rg -n "_ActiveEnvelope|active_envelopes" loom tests
```

Expected before editing: matches in `loom/platform/cli/app.py`, `loom/platform/cli/main.py`, and `tests/test_app.py`.

This task removes `_ActiveEnvelope` completely. Do not leave it as a transition alias unless another package import outside `loom/` and `tests/` is found and documented in this plan.

- [ ] **Step 3: Write failing footer heartbeat tests**

Update imports in `tests/test_app.py`:

```python
from loom.platform.interaction_language import HeartbeatState
```

Add tests under `TestFooterRender`:

```python
def test_heartbeat_replaces_active_envelope_label(self, app: LoomApp) -> None:
    import time as _t

    app.footer.heartbeat_state = HeartbeatState.TOOLING.value
    app.footer.heartbeat_label = "執行指令"
    app.footer.heartbeat_subject = "pytest"
    app.footer.heartbeat_started_monotonic = _t.monotonic() - 8

    text = _flat_text(app._render_footer())

    assert "執行指令" in text
    assert "pytest" in text
    assert "▸ run_bash" not in text
    assert app.footer.heartbeat_state == HeartbeatState.TOOLING.value


def test_stalled_heartbeat_uses_warning_copy(self, app: LoomApp) -> None:
    import time as _t

    app.footer.heartbeat_state = HeartbeatState.STALLED.value
    app.footer.heartbeat_label = "執行指令"
    app.footer.heartbeat_subject = "pytest"
    app.footer.heartbeat_started_monotonic = _t.monotonic() - 95

    text = _flat_text(app._render_footer())

    assert "still waiting" in text
    assert "執行指令" in text
```

- [ ] **Step 4: Run focused tests to verify failure**

Run:

```bash
pytest -q tests/test_app.py::TestFooterRender::test_heartbeat_replaces_active_envelope_label tests/test_app.py::TestFooterRender::test_stalled_heartbeat_uses_warning_copy
```

Expected: FAIL because `FooterState` lacks heartbeat fields.

- [ ] **Step 5: Replace active-envelope footer fields with heartbeat fields**

In `loom/platform/cli/app.py`, delete the `_ActiveEnvelope` dataclass and remove `active_envelopes` from `FooterState`. Add new fields to `FooterState`:

```python
    heartbeat_state: str = "idle"
    heartbeat_label: str = ""
    heartbeat_subject: str = ""
    heartbeat_started_monotonic: float = 0.0
    heartbeat_last_event_monotonic: float = 0.0
    heartbeat_stale_after_s: float = 30.0
```

Add methods to `LoomApp`:

```python
    def start_heartbeat(
        self,
        *,
        state: str,
        label: str,
        subject: str = "",
        stale_after_s: float = 30.0,
    ) -> None:
        now = monotonic()
        self.footer.heartbeat_state = state
        self.footer.heartbeat_label = label
        self.footer.heartbeat_subject = subject
        self.footer.heartbeat_started_monotonic = now
        self.footer.heartbeat_last_event_monotonic = now
        self.footer.heartbeat_stale_after_s = stale_after_s
        self.invalidate()

    def touch_heartbeat(self) -> None:
        if self.footer.heartbeat_state != "idle":
            self.footer.heartbeat_last_event_monotonic = monotonic()
            self.invalidate()

    def stop_heartbeat(self) -> None:
        self.footer.heartbeat_state = "idle"
        self.footer.heartbeat_label = ""
        self.footer.heartbeat_subject = ""
        self.footer.heartbeat_started_monotonic = 0.0
        self.footer.heartbeat_last_event_monotonic = 0.0
        self.invalidate()
```

- [ ] **Step 6: Render heartbeat in `_render_footer()`**

Replace the active envelope section with:

```python
        # Runtime heartbeat — system-driven liveness signal. It replaces the
        # old raw active-envelope label with action-language copy.
        heartbeat_active = (
            s.heartbeat_state
            and s.heartbeat_state != "idle"
            and s.heartbeat_label
        )
        if heartbeat_active:
            import time as _t
            from loom.platform.interaction_language import format_elapsed

            elapsed = max(0.0, _t.monotonic() - s.heartbeat_started_monotonic)
            quiet_for = max(0.0, _t.monotonic() - s.heartbeat_last_event_monotonic)
            # Only tool-execution states can go "stalled" from quiet. THINKING /
            # PAUSED_BLOCKING are waiting on agent or user respectively and must
            # not render "still waiting" — that mirrors the Discord watchdog
            # suppression for the pause window.
            stalled = (
                s.heartbeat_state == "stalled"
                or (
                    s.heartbeat_state in ("tooling", "long_tooling")
                    and quiet_for >= s.heartbeat_stale_after_s
                )
            )
            prefix = "still waiting · " if stalled else ""
            label = f"{prefix}{s.heartbeat_label}"
            if s.heartbeat_subject:
                label += f" · {s.heartbeat_subject}"
            label += f" · {format_elapsed(elapsed)}"
            parts.append(("class:footer", "  "))
            style = "class:footer.budget.warn" if stalled else "class:footer.envelope"
            parts.append((style, label))
```

Delete the old active-envelope render block entirely. Change last-turn stats gating from `if not envs:` to `if not heartbeat_active:`.

- [ ] **Step 7: Update old active-envelope tests**

Delete `test_active_envelope_shown_with_elapsed` and `test_multiple_envelopes_show_count_prefix`. Keep coverage for last-turn stats suppression by setting `app.footer.heartbeat_state`, `heartbeat_label`, and `heartbeat_started_monotonic` instead of appending `_ActiveEnvelope`.

- [ ] **Step 8: Verify `_ActiveEnvelope` is gone**

Run:

```bash
rg -n "_ActiveEnvelope|active_envelopes" loom tests
```

Expected: no matches.

- [ ] **Step 9: Run tests**

Run:

```bash
pytest -q tests/test_app.py tests/test_interaction_language.py
```

Expected: PASS.

- [ ] **Step 10: Commit**

Run:

```bash
npx gitnexus detect-changes --scope staged
git add loom/platform/cli/app.py tests/test_app.py
git commit -m "feat(cli): add runtime heartbeat footer state"
```

## Task 4: CLI Stream Event Wiring For Heartbeat

**Files:**
- Modify: `loom/platform/cli/main.py`
- Modify: `loom/platform/cli/app.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Run impact analysis**

Run impact analysis for `_run_streaming_turn`, `ToolBegin`, `ToolEnd`, `TurnDone`, `CompressDone`, and `TurnPaused` handling in `loom/platform/cli/main.py`.

Expected: medium risk because this is the main CLI event loop.

- [ ] **Step 2: Add a small helper in `main.py`**

Near `_run_streaming_turn()` helper definitions, add:

```python
def _start_tool_heartbeat(loom_app: Any, name: str, args: dict[str, Any]) -> None:
    from loom.platform.interaction_language import HeartbeatState, resolve_tool_action

    action = resolve_tool_action(name, args)
    loom_app.start_heartbeat(
        state=HeartbeatState.TOOLING.value,
        label=action.label,
        subject=action.subject,
        stale_after_s=action.stale_after_s,
    )
```

- [ ] **Step 3: Retire the old thinking line**

Heartbeat is the single source of truth for "thinking". In `loom/platform/cli/app.py`, remove `footer.thinking`, `thinking_window`, `_render_thinking()`, and tests that assert `_render_thinking()` output. Do not keep both the old thinking animation and the new heartbeat; two animation sources would compete in the same bottom area.

- [ ] **Step 4: Wire turn start**

Where the CLI currently sets `app.footer.thinking = True` before `stream_turn`, replace that assignment with:

```python
from loom.platform.interaction_language import HeartbeatState

app.start_heartbeat(
    state=HeartbeatState.THINKING.value,
    label="Loom is thinking",
    stale_after_s=30.0,
)
```

- [ ] **Step 5: Wire `ToolBegin`**

In the `ToolBegin` branch, replace the `_ActiveEnvelope` append block with:

```python
                loom_app = getattr(session, "_loom_app", None)
                if loom_app is not None:
                    _start_tool_heartbeat(loom_app, event.name, event.args)
```

- [ ] **Step 6: Wire `ToolEnd` and `TurnDone`**

In the `ToolEnd` branch, replace active-envelope removal with:

```python
                loom_app = getattr(session, "_loom_app", None)
                if loom_app is not None:
                    loom_app.touch_heartbeat()
                    loom_app.stop_heartbeat()
```

In the `TurnDone` branch, before last-turn stats are written:

```python
                    loom_app.stop_heartbeat()
```

- [ ] **Step 7: Wire compaction and pause**

For `CompressDone`, ensure compaction clears heartbeat:

```python
                if (loom_app := getattr(session, "_loom_app", None)) is not None:
                    loom_app.stop_heartbeat()
                    loom_app.show_transient_hint(
                        f"🗜 compacted → {event.fact_count} facts",
                        severity="info",
                        duration_s=3.0,
                    )
```

For `TurnPaused`, suppress heartbeat before showing the modal:

```python
                if (loom_app := getattr(session, "_loom_app", None)) is not None:
                    from loom.platform.interaction_language import HeartbeatState
                    loom_app.start_heartbeat(
                        state=HeartbeatState.PAUSED_BLOCKING.value,
                        label="等待你的決定",
                    )
```

Then call `loom_app.stop_heartbeat()` after the pause choice resolves.

- [ ] **Step 8: Add a heartbeat transition test**

Add this focused test to `tests/test_app.py`:

```python
def test_heartbeat_state_sequence_thinking_tooling_stalled_idle(app: LoomApp) -> None:
    import time as _t

    app.start_heartbeat(
        state=HeartbeatState.THINKING.value,
        label="Loom is thinking",
        stale_after_s=30.0,
    )
    assert app.footer.heartbeat_state == HeartbeatState.THINKING.value

    app.start_heartbeat(
        state=HeartbeatState.TOOLING.value,
        label="執行指令",
        subject="pytest",
        stale_after_s=90.0,
    )
    assert app.footer.heartbeat_state == HeartbeatState.TOOLING.value
    assert "執行指令" in _flat_text(app._render_footer())

    app.footer.heartbeat_last_event_monotonic = _t.monotonic() - 91
    text = _flat_text(app._render_footer())
    assert "still waiting" in text

    app.stop_heartbeat()
    assert app.footer.heartbeat_state == HeartbeatState.IDLE.value


def test_paused_blocking_heartbeat_does_not_render_stalled_prefix(app: LoomApp) -> None:
    import time as _t

    app.start_heartbeat(
        state=HeartbeatState.PAUSED_BLOCKING.value,
        label="等待你的決定",
        stale_after_s=30.0,
    )
    # Simulate user thinking for two minutes — heartbeat is waiting on user,
    # not on agent, so footer must not call it "still waiting".
    app.footer.heartbeat_last_event_monotonic = _t.monotonic() - 120

    text = _flat_text(app._render_footer())
    assert "等待你的決定" in text
    assert "still waiting" not in text
```

- [ ] **Step 9: Run CLI app tests**

Run:

```bash
pytest -q tests/test_app.py tests/test_session.py::TestStreamTurnLocking tests/test_cache_display.py
```

Then run the full session test file because the stream loop is broadly coupled:

```bash
pytest -q tests/test_session.py
```

Expected: PASS.

- [ ] **Step 10: Commit**

Run:

```bash
npx gitnexus detect-changes --scope staged
git add loom/platform/cli/main.py tests/test_app.py
git commit -m "feat(cli): drive heartbeat from stream events"
```

## Task 5: Envelope Status Rendering With Intent And Outcome

**Files:**
- Modify: `loom/platform/discord/bot.py`
- Create: `tests/test_discord_interaction_language.py`

- [ ] **Step 1: Run impact analysis**

Run impact analysis for `_format_envelope_status` and `_run_turn`.

Expected: medium risk for Discord display, low risk for core behavior.

- [ ] **Step 2: Write formatter tests**

Create `tests/test_discord_interaction_language.py`:

```python
from __future__ import annotations

from loom.core.events import ExecutionEnvelopeView, ExecutionNodeView
from loom.platform.discord.bot import _format_envelope_status
from loom.platform.interaction_language import EnvelopeOutcome, ParallelReason


def _node(node_id: str, tool_name: str, state: str = "executing") -> ExecutionNodeView:
    return ExecutionNodeView(
        node_id=node_id,
        call_id=f"call-{node_id}",
        action_id=node_id,
        tool_name=tool_name,
        level=0,
        state=state,
        trust_level="SAFE",
    )


def test_single_node_envelope_keeps_compact_header() -> None:
    view = ExecutionEnvelopeView(
        envelope_id="e1",
        session_id="s1",
        turn_index=1,
        status="running",
        node_count=1,
        parallel_groups=1,
        levels=[["n1"]],
        nodes=[_node("n1", "read_file")],
    )

    text = _format_envelope_status(view)

    assert "Envelope e1" in text
    assert "read_file" in text
    assert "▸" not in text


def test_multi_node_envelope_renders_intent_and_parallel_reason() -> None:
    view = ExecutionEnvelopeView(
        envelope_id="e2",
        session_id="s1",
        turn_index=1,
        status="running",
        node_count=2,
        parallel_groups=1,
        levels=[["n1", "n2"]],
        nodes=[_node("n1", "pytest"), _node("n2", "pytest")],
        intent="重複跑測試確認穩定性",
        parallel_reason=ParallelReason.FAN_OUT_REPLICAS.value,
    )

    text = _format_envelope_status(view)

    assert "▸ 重複跑測試確認穩定性" in text
    assert "多次驗證" in text
    assert "fan_out_replicas" not in text


def test_multi_node_envelope_synthesizes_intent_when_agent_omits_it() -> None:
    view = ExecutionEnvelopeView(
        envelope_id="e4",
        session_id="s1",
        turn_index=1,
        status="running",
        node_count=3,
        parallel_groups=1,
        levels=[["n1", "n2", "n3"]],
        nodes=[_node("n1", "pytest"), _node("n2", "pytest"), _node("n3", "pytest")],
        parallel_reason=ParallelReason.FAN_OUT_REPLICAS.value,
    )

    text = _format_envelope_status(view)

    assert "▸ 重複跑同一組 pytest" in text
    assert "多次驗證" in text


def test_unfulfilled_envelope_renders_outcome_summary() -> None:
    view = ExecutionEnvelopeView(
        envelope_id="e3",
        session_id="s1",
        turn_index=1,
        status="failed",
        node_count=2,
        parallel_groups=1,
        levels=[["n1", "n2"]],
        nodes=[_node("n1", "pytest", "observed"), _node("n2", "pytest", "timed_out")],
        intent="驗證測試狀態",
        outcome=EnvelopeOutcome.UNFULFILLED.value,
        outcome_summary="其中一組測試逾時，需要重跑",
    )

    text = _format_envelope_status(view)

    assert "⚠ 其中一組測試逾時，需要重跑" in text


def test_empty_outcome_infers_from_failed_status_without_claiming_success() -> None:
    view = ExecutionEnvelopeView(
        envelope_id="e5",
        session_id="s1",
        turn_index=1,
        status="failed",
        node_count=1,
        parallel_groups=1,
        levels=[["n1"]],
        nodes=[_node("n1", "pytest", "timed_out")],
        outcome="",
    )

    text = _format_envelope_status(view)

    assert "failed" in text or "⚠" in text
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
pytest -q tests/test_discord_interaction_language.py
```

Expected: FAIL because `_format_envelope_status()` ignores new metadata.

- [ ] **Step 4: Update `_format_envelope_status()`**

In `loom/platform/discord/bot.py`, import:

```python
from loom.platform.interaction_language import (
    EnvelopeOutcome,
    ParallelReason,
    format_parallel_reason,
    synthesize_envelope_intent,
)
```

Update `_format_envelope_status()` header logic:

```python
    is_multi = view.node_count > 1
    if is_multi:
        intent = view.intent or synthesize_envelope_intent(
            parallel_reason=view.parallel_reason,
            tool_names=[n.tool_name for n in view.nodes],
        )
        header = f"-# ▸ {intent}"
        reason_label = format_parallel_reason(view.parallel_reason)
        if reason_label:
            header += f" · {reason_label}"
    else:
        header = f"-# Envelope {view.envelope_id} · {view.node_count} actions"
```

After node lines, add:

```python
    outcome = view.outcome
    if not outcome and view.status == "failed":
        outcome = EnvelopeOutcome.UNFULFILLED.value
    if outcome and outcome != EnvelopeOutcome.FULFILLED.value:
        glyph = {
            EnvelopeOutcome.PARTIAL.value: "◐",
            EnvelopeOutcome.UNFULFILLED.value: "⚠",
            EnvelopeOutcome.PIVOTED.value: "↪",
            EnvelopeOutcome.ABORTED.value: "🛑",
        }.get(outcome, "◐")
        if view.outcome_summary:
            lines.append(f"-# {glyph} {view.outcome_summary}")
        elif outcome == EnvelopeOutcome.UNFULFILLED.value:
            lines.append(f"-# {glyph} envelope did not fulfill its intent")
```

- [ ] **Step 5: Run tests**

Run:

```bash
pytest -q tests/test_discord_interaction_language.py tests/test_discord_embed_v2.py tests/test_task_write_discord_reminder.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
npx gitnexus detect-changes --scope staged
git add loom/platform/discord/bot.py tests/test_discord_interaction_language.py
git commit -m "feat(discord): render envelope intent and outcome"
```

## Task 6: Mechanical Outcome Derivation

**Files:**
- Modify: `loom/platform/interaction_language.py`
- Modify: `loom/core/ledger/envelope_view.py`
- Modify: `loom/core/session.py`
- Modify: `tests/test_interaction_language.py`
- Modify: `tests/test_ledger_envelope_projector.py`
- Modify: `tests/test_ledger_session_wiring.py`

- [ ] **Step 1: Run impact analysis**

Run impact analysis for `ExecutionEnvelopeView` producer symbols, especially the ledger projector and `LoomSession._build_envelope_view`.

Expected: medium risk because envelope metadata is consumed cross-platform.

- [ ] **Step 2: Write failing tests**

Add to `tests/test_interaction_language.py`:

```python
from loom.platform.interaction_language import derive_envelope_outcome


def test_derive_envelope_outcome_fulfilled_when_all_success_states() -> None:
    assert derive_envelope_outcome(["memorialized", "committed"]) == "fulfilled"


def test_derive_envelope_outcome_partial_when_mixed_success_and_failure() -> None:
    assert derive_envelope_outcome(["memorialized", "timed_out"]) == "partial"


def test_derive_envelope_outcome_unfulfilled_when_all_failed() -> None:
    assert derive_envelope_outcome(["denied", "aborted"]) == "unfulfilled"


def test_derive_envelope_outcome_aborted_when_abort_dominates() -> None:
    assert derive_envelope_outcome(["aborted"]) == "aborted"


def test_derive_envelope_outcome_never_mechanically_pivots() -> None:
    assert derive_envelope_outcome(["memorialized", "timed_out"]) != "pivoted"
```

- [ ] **Step 3: Implement helper**

Add to `loom/platform/interaction_language.py`:

```python
_SUCCESS_STATES = {"observed", "validated", "committed", "memorialized"}
_FAILURE_STATES = {"denied", "timed_out", "reverted", "failed"}


def derive_envelope_outcome(states: list[str]) -> str:
    if not states:
        return EnvelopeOutcome.FULFILLED.value
    lowered = [s.lower() for s in states]
    success_count = sum(1 for s in lowered if s in _SUCCESS_STATES)
    failure_count = sum(1 for s in lowered if s in _FAILURE_STATES)
    aborted_count = sum(1 for s in lowered if s == "aborted")
    if aborted_count and not success_count and not failure_count:
        return EnvelopeOutcome.ABORTED.value
    if failure_count or aborted_count:
        return EnvelopeOutcome.PARTIAL.value if success_count else EnvelopeOutcome.UNFULFILLED.value
    return EnvelopeOutcome.FULFILLED.value
```

Mechanical derivation intentionally never returns `pivoted`; pivoted means the agent changed strategy and requires LLM-authored judgement. If the LLM omits that judgement, the system falls back to fulfilled / partial / unfulfilled / aborted only.

- [ ] **Step 4: Run tests**

Run:

```bash
pytest -q tests/test_interaction_language.py
```

Expected: PASS.

- [ ] **Step 5: Wire producer metadata in `LedgerEnvelopeProjector.build_view()`**

In `loom/core/ledger/envelope_view.py`, import:

```python
from loom.platform.interaction_language import derive_envelope_outcome
```

Inside `LedgerEnvelopeProjector.build_view()`, before returning `ExecutionEnvelopeView`, compute:

```python
        outcome = derive_envelope_outcome([node.state for node in nodes])
```

Then pass it into the constructor:

```python
            outcome=outcome,
```

Keep `intent=""` and `parallel_reason="unspecified"` in this task; Task 8 covers prompt-authored intent.

- [ ] **Step 6: Wire empty-stub outcome in `LoomSession._build_envelope_view()`**

In `loom/core/session.py`, update the no-ledger stub constructor:

```python
        return ExecutionEnvelopeView(
            envelope_id=f"e{self._envelope_counter}",
            session_id=self.session_id,
            turn_index=self._turn_index,
            status="running",
            node_count=0,
            parallel_groups=0,
            outcome="",
        )
```

- [ ] **Step 7: Add projector outcome assertions**

In `tests/test_ledger_envelope_projector.py`, extend the existing completed and failed view tests with:

```python
    assert view.outcome == "fulfilled"
```

for the all-success case, and:

```python
    assert view.outcome in {"partial", "unfulfilled"}
```

for mixed or failed terminal states, depending on the fixture's node states.

- [ ] **Step 8: Run tests**

Run:

```bash
pytest -q tests/test_interaction_language.py tests/test_ledger_envelope_projector.py tests/test_ledger_session_wiring.py
```

Expected: PASS.

- [ ] **Step 9: Commit**

Run:

```bash
npx gitnexus detect-changes --scope staged
git add loom/platform/interaction_language.py loom/core/ledger/envelope_view.py loom/core/session.py tests/test_interaction_language.py tests/test_ledger_envelope_projector.py tests/test_ledger_session_wiring.py
git commit -m "feat(ui): derive envelope outcomes"
```

## Task 7: Discord Stalled Proxy

**Files:**
- Modify: `loom/platform/discord/bot.py`
- Test: add lightweight helper tests if the edit decision can be extracted

- [ ] **Step 1: Run impact analysis**

Run impact analysis for `LoomDiscordBot._run_turn` and any helper extracted from it.

Expected: medium risk because Discord turn streaming is asynchronous and rate-limited.

- [ ] **Step 2: Extract a pure stalled-decision helper**

Add near Discord display helpers:

```python
def _should_emit_stalled_status(
    *,
    now: float,
    last_event_at: float,
    threshold_s: float,
    already_emitted: bool,
    suppressed: bool = False,
) -> bool:
    if suppressed:
        return False
    return (not already_emitted) and now - last_event_at >= threshold_s
```

- [ ] **Step 3: Add tests**

Create or extend `tests/test_discord_interaction_language.py`:

```python
from loom.platform.discord.bot import _should_emit_stalled_status


def test_stalled_status_emits_once_after_threshold() -> None:
    assert _should_emit_stalled_status(
        now=100.0,
        last_event_at=9.0,
        threshold_s=90.0,
        already_emitted=False,
    )
    assert not _should_emit_stalled_status(
        now=100.0,
        last_event_at=9.0,
        threshold_s=90.0,
        already_emitted=True,
    )


def test_stalled_status_does_not_emit_before_threshold() -> None:
    assert not _should_emit_stalled_status(
        now=100.0,
        last_event_at=20.0,
        threshold_s=90.0,
        already_emitted=False,
    )


def test_stalled_status_is_suppressed_while_waiting_for_user() -> None:
    assert not _should_emit_stalled_status(
        now=200.0,
        last_event_at=0.0,
        threshold_s=90.0,
        already_emitted=False,
        suppressed=True,
    )
```

- [ ] **Step 4: Wire minimal Discord stalled signal**

Inside `_run_turn`, initialize:

```python
        _last_runtime_event_at = time.monotonic()
        _stalled_emitted = False
        _active_stale_threshold_s = 90.0
        _turn_done = False
        _stall_watchdog_suppressed = False
```

Add a watchdog task after initialization:

```python
        async def _stall_watchdog() -> None:
            nonlocal _stalled_emitted
            while not _turn_done:
                await asyncio.sleep(5.0)
                now = time.monotonic()
                if _should_emit_stalled_status(
                    now=now,
                    last_event_at=_last_runtime_event_at,
                    threshold_s=_active_stale_threshold_s,
                    already_emitted=_stalled_emitted,
                    suppressed=_stall_watchdog_suppressed,
                ):
                    _stalled_emitted = True
                    await _safe_edit(
                        status_msg,
                        (tool_buf.lstrip() or "-# ◌ working…")
                        + f"\n-# still waiting · {int(now - _last_runtime_event_at)}s",
                    )

        _stall_task = asyncio.create_task(_stall_watchdog())
```

On each received event, reset the runtime timestamp:

```python
                    _last_runtime_event_at = time.monotonic()
                    _stalled_emitted = False
```

In the `TurnPaused` branch, suppress the watchdog before waiting for user reply and unsuppress it after `resume()`, `resume_with()`, cancellation, or timeout:

```python
                        _stall_watchdog_suppressed = True
                        try:
                            reply = await self._client.wait_for(
                                "message", check=_pause_check, timeout=120.0
                            )
                            ...
                        finally:
                            _stall_watchdog_suppressed = False
                            _last_runtime_event_at = time.monotonic()
```

At the top of `loom/platform/discord/bot.py`, add:

```python
import contextlib
```

In the `finally` section around the stream loop, stop the watchdog:

```python
            finally:
                _turn_done = True
                _stall_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await _stall_task
```

- [ ] **Step 5: Run tests**

Run:

```bash
pytest -q tests/test_discord_interaction_language.py tests/test_discord_safe_send.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
npx gitnexus detect-changes --scope staged
git add loom/platform/discord/bot.py tests/test_discord_interaction_language.py
git commit -m "feat(discord): prepare stalled status decision helper"
```

## Task 8: Planner Prompt Contract For Envelope Intent

**Files:**
- Modify: `loom/core/cognition/prompt_stack.py`
- Modify: `tests/test_prompt_stack.py`

- [ ] **Step 1: Run impact analysis**

Run impact analysis for `PromptStack.load` and `PromptStack.composed_prompt`.

Expected: medium risk because prompt changes can affect agent behavior broadly.

- [ ] **Step 2: Add a prompt-stack test**

In `tests/test_prompt_stack.py`, add:

```python
class TestBuiltInInteractionLanguageLayer:
    def test_builtin_layer_is_loaded_after_agent_before_personality(self, tmp_soul, tmp_agent, tmp_personalities):
        p_path = tmp_personalities / "adversarial.md"
        stack = PromptStack(
            soul_path=tmp_soul,
            agent_path=tmp_agent,
            personality_path=p_path,
            personalities_dir=tmp_personalities,
        )

        prompt = stack.load()
        sep = PromptStack.LAYER_SEPARATOR

        assert stack.layer_names == [
            "soul",
            "agent",
            "interaction_language",
            "personality",
        ]
        assert (
            "I am Agent."
            + sep
            + PromptStack.INTERACTION_LANGUAGE_INSTRUCTIONS
            + sep
            + "I challenge assumptions."
        ) in prompt

    def test_builtin_layer_mentions_envelope_intent_contract(self, tmp_agent):
        stack = PromptStack(agent_path=tmp_agent)

        prompt = stack.load()

        assert "multi-tool batch" in prompt
        assert "one-line intent" in prompt
        assert "outcome judgement" in prompt
```

- [ ] **Step 3: Import instruction text from interaction_language module**

The prompt instruction string lives in `loom/platform/interaction_language.py`
(added in Task 1) so the contract and the UI behaviour that depends on it stay
in the same module. `prompt_stack.py` only needs to import and layer it.

In `loom/core/cognition/prompt_stack.py`:

```python
from loom.platform.interaction_language import INTERACTION_LANGUAGE_INSTRUCTIONS
```

Expose it as a class attribute for backwards-compatible test access:

```python
class PromptStack:
    ...
    INTERACTION_LANGUAGE_INSTRUCTIONS = INTERACTION_LANGUAGE_INSTRUCTIONS
```

In `PromptStack.load()`, insert the built-in layer after the optional agent layer and before the optional personality layer, only when at least one user prompt layer was loaded:

```python
        if self._layers:
            self._layers.append(PromptLayer(
                "interaction_language",
                INTERACTION_LANGUAGE_INSTRUCTIONS,
                None,
            ))
```

- [ ] **Step 4: Update existing prompt-stack expectations**

In `tests/test_prompt_stack.py`, update tests that assert exact `load()` output or `layer_names` for stacks with a soul or agent layer. For example, change:

```python
assert stack.layer_names == ["soul"]
```

to:

```python
assert stack.layer_names == ["soul", "interaction_language"]
```

and change composed prompt expectations from:

```python
assert result == "I am SOUL."
```

to:

```python
sep = PromptStack.LAYER_SEPARATOR
assert result == f"I am SOUL.{sep}{PromptStack.INTERACTION_LANGUAGE_INSTRUCTIONS}"
```

Keep `test_empty_stack_returns_empty_string` unchanged; the built-in layer is not loaded when no real prompt layer exists.

- [ ] **Step 5: Run tests**

Run:

```bash
pytest -q tests/test_prompt_stack.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
npx gitnexus detect-changes --scope staged
git add loom/core/cognition/prompt_stack.py tests/test_prompt_stack.py
git commit -m "feat(prompt): request envelope intent summaries"
```

## Task 9: Documentation And Decision Closure

**Files:**
- Modify: `docs/superpowers/specs/2026-05-20-ui-ux-interaction-language-design.md`
- Modify: `docs/superpowers/plans/2026-05-21-ui-ux-interaction-language.md`

- [ ] **Step 1: Resolve the eight open decisions in the spec**

Recommended defaults for the first implementation pass:

1. Stale thresholds: keep 30s default, 90s long-runner.
2. Single-node envelope header: skip header by default.
3. Outcome source of truth: mechanical outcome is system truth; LLM summary is display enrichment.
4. Empty `outcome` means unknown, never fulfilled; renderers infer from `status` when producer metadata is absent.
5. Persona voice: precision wins for heartbeat.
6. TaskList vs envelope intent: keep independent; envelope intent may mirror active task text but does not link in v1.
7. Discord transient hints: suppress ambient hints, persist real warnings.
8. Traditional Chinese is the default heartbeat language; label data is stored in a locale-shaped registry so English can be added later without changing resolver control flow.
9. `ToolProgress` is deferred; timeout-based stale detection is v1.
10. Mechanical outcome derivation never returns `pivoted`; pivoted is only available from LLM-authored judgement.

- [ ] **Step 2: Edit the spec's `Decisions To Confirm` section**

Replace the section with:

```markdown
## Decisions Closed For First Implementation

1. Stale thresholds start at 30 seconds default and 90 seconds for known long-runners.
2. Single-node envelopes do not render intent headers by default.
3. Mechanical outcome is the system truth; LLM-authored summary is display enrichment.
4. Empty `outcome` means unknown, never fulfilled.
5. Heartbeat labels prioritize precision over persona immersion.
6. TaskList and envelope intent stay independent in v1.
7. Discord suppresses ambient transient hints and persists real warnings.
8. Traditional Chinese is the default heartbeat language; labels live in a locale-shaped registry.
9. `ToolProgress` is deferred; timeout-based stale detection is v1.
10. Mechanical outcome derivation never returns `pivoted`; pivoted requires LLM-authored judgement.
```

- [ ] **Step 3: Add explicit deferred-scope list**

Add this section to the spec after `Decisions Closed For First Implementation`:

```markdown
## Deferred From First Implementation

- `PermissionLeaseGranted` event: not in slice 1. Existing footer grant badge remains the durable display; timeline lease event can be added after heartbeat lands.
- `MemoryDedupSkipped` event: not in slice 1. Dedup counts remain summary material until memory event surfacing is designed.
- L3 expandable details slash command or inline toggle: not in slice 1. Existing `/think` remains the only explicit detail entry point.
- Discord transient hint matrix: slice 1 only applies the broad rule. Concrete defaults are: suppress `📍 turn N milestone` on Discord, persist `⚠️ context 80%` as a warning line.
- `ToolProgress`: not in slice 1. Long-runner progress remains timeout-based.
- English heartbeat labels: not in slice 1. The label registry shape must make this additive later.
- Heartbeat `compacting` state: not in slice 1. Compaction display stays on the legacy `FooterState.compacting` flag (`⚡ 壓縮中…`). The enum value is intentionally absent until `CompressStarted` is wired into heartbeat in a later slice.
```

- [ ] **Step 4: Run documentation checks**

Run:

```bash
rg -n "Decisions To Confirm|TBD|FIXME" docs/superpowers/specs/2026-05-20-ui-ux-interaction-language-design.md docs/superpowers/plans/2026-05-21-ui-ux-interaction-language.md
git diff --check
```

Expected: no matches for unresolved placeholders and clean diff check.

- [ ] **Step 5: Commit**

Run:

```bash
npx gitnexus detect-changes --scope staged
git add docs/superpowers/specs/2026-05-20-ui-ux-interaction-language-design.md docs/superpowers/plans/2026-05-21-ui-ux-interaction-language.md
git commit -m "docs: close UI interaction language implementation decisions"
```

## Final Verification

After all tasks are complete, run:

```bash
pytest -q tests/test_interaction_language.py tests/test_app.py tests/test_event_consumer_map.py tests/test_ledger_envelope_projector.py tests/test_discord_interaction_language.py tests/test_discord_embed_v2.py tests/test_task_write_discord_reminder.py tests/test_cache_display.py
git diff --check
npx gitnexus detect-changes --scope staged
```

Expected:

- All selected tests pass.
- `git diff --check` reports no whitespace errors.
- GitNexus reports only the expected changed symbols and no unexpected high-risk flows.

## Positions From Second-Planner Review

- Keep `interaction_language.py` under `loom/platform/`; core event types stay as data structures.
- Do not pull `ToolProgress` into slice 1.
- Accept CLI true heartbeat plus Discord `typing()` / debounced status asymmetry.
- Keep intent in slice 1, but add mechanical placeholder fallback for omitted LLM intent.
- Keep Traditional Chinese as the day-one default while storing labels in a locale-shaped registry.
