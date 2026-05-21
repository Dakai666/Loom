# UI/UX Interaction Language Design

Date: 2026-05-20

## Goal

Define Loom's shared interaction language before deepening the CLI or Discord
surfaces.

The core direction is hybrid: Loom can feel like a warm working partner during
conversation, but system behavior must read like reliable instrumentation when
tools, permissions, memory, task state, or risk are involved.

This design is intentionally a planning document. It captures the UX grammar
from brainstorming so it can later be merged with code-level findings into an
implementation plan.

## Current Context

The Textual TUI has been retired. The active user-facing surfaces are:

- `loom chat`, backed by prompt_toolkit, Rich rendering, a persistent footer,
  confirm/pause widgets, TaskList display, slash commands, and theme tokens.
- Discord threads, backed by embeds, buttons, thread sessions, tool summaries,
  task reminders, and confirmation views.
- Shared harness events: tool lifecycle, permission decisions, scope grants,
  compaction, memory governance, task updates, model/tier changes, and errors.

The existing CLI Refresh direction already points toward linear stream plus live
footer, but the deeper product question is not only layout. It is whether users
can understand what the agent is doing, why an event matters, and whether they
need to intervene.

## Design Thesis

Important events should be visible. Visible events should have a clear source.
Users should be able to understand what the agent is doing now, what important
turning points already happened, and where to inspect details when needed.

The working sentence:

> TaskList is the story; footer heartbeat is the vital sign.

TaskList can express the agent's plan and work narrative, but it is agent-authored
and should not be the only signal that runtime work is still alive. Runtime
heartbeat must be system-driven.

## Layout Aesthetic

Loom is a vertical-flow surface. Everything happens above the input field, in
chronological order, top to bottom. There is no left/right partitioning, no
sidebar, no dashboard grid. The input field is the anchor at the bottom of the
viewport; the eye travels upward to read history and downward to type the next
turn.

This holds for both CLI and Discord. Discord inherits this naturally from its
message stream. CLI must enforce it deliberately because terminal layout
libraries can host two-column panes — we choose not to.

Concrete consequences:

- TaskList renders as an inline block above the input separator, not as a side
  panel.
- Heartbeat lives in the footer line, immediately above the input field, not
  in a status pane to one side.
- Confirm, pause, and redirect widgets occupy the same vertical band that the
  TaskList would; they replace it momentarily, never sit beside it.
- Long tool output may push earlier history out of view; this is acceptable
  because the action timeline preserves the important turning points. Detailed
  history retrieval is a slash-command or scrollback action, not a persistent
  side pane.
- Multi-pane proposals (split scrollback, dedicated permission column, persistent
  reasoning panel) are out of scope. The Textual TUI tried adjacent regions and
  is retired; do not reintroduce them under a different name.

When the spec refers to a "panel" below, the word means an inline vertical
block within the same column, not a windowed region on the side.

## Event Grammar

Every user-visible event should be classified by these fields:

- Source: agent, harness, tool, permission, memory, task, notification, or
  platform.
- Importance: ambient, normal, important, warning, or blocking.
- State: pending, active, committed, failed, aborted, frozen, or stale.
- User intervention: none, optional inspect, confirm required, or blocked.
- Surface placement: footer heartbeat, timeline stream, modal/widget, TaskList,
  Discord embed, Discord button, or thread summary.

This grammar should be shared by CLI and Discord even when the visual components
are different.

## Attention Rules

Green lights stay quiet. Red lights become visible.

Successful routine events should not flood the stream. They can update ambient
state, footer heartbeat, or a collapsed summary. Warnings, denials, stalls,
failures, and user decisions should leave visible evidence.

Modal or blocking UI is reserved for decisions the user must make now, such as
permission approval or HITL redirection. Everything else should prefer footer,
timeline, or expandable detail.

## Voice Rules

Agent text can be conversational and warm.

Harness, tool, permission, and runtime state should be short, precise, and
source-labeled. The goal is not to remove personality from Loom; it is to avoid
letting system state masquerade as agent prose.

Heartbeat labels use a mixed naming rule:

- System and tool activity should use action descriptions, not raw implementation
  labels when a clearer user-facing phrase exists.
- Agent thinking, synthesis, and result digestion may use more natural partner
  language.
- Critical or risky states should prefer precision over charm.

Examples:

```text
查詢影響中 · 00:08
等待測試輸出 · 00:31
整理工具結果 · 00:05
我還在整理剛剛的結果 · 00:12
壓縮 context 中 · 00:44
等待授權 · run_bash · guarded
```

## State Rules

Active work should have the highest visual priority. Completed work should be
visible briefly, then recede.

Recommended state ladder:

- Active: animated, bright enough to read immediately, includes elapsed time.
- Committed: success/failure is clear, no longer animated.
- Frozen: dimmed or collapsed, still available as history.
- Stale: if no heartbeat or progress event appears past a threshold, show a
  still-waiting or possibly-stalled hint.

The UI should avoid a single static "currently doing X" line that sits unchanged
for a long time. Long-running work needs a visible sense of time passing.

## Narrative Layers

### 1. System Heartbeat

Answers: "Is Loom still doing something?"

This layer is runtime-driven and reliable. It should show:

- Current activity label.
- Elapsed time.
- Optional spinner or animation variant.
- Last meaningful event age when useful.
- Stalled or waiting hint after a threshold.

The heartbeat should be lively without becoming noisy. Variation should come
from context-aware labels and animation states, not repeated stream messages.

### 2. Action Timeline

Answers: "What important steps happened?"

This layer is append-only or collapsible history. It should capture important
turning points:

- Impact analysis completed.
- Permission requested, granted, denied, or expired.
- Tool started, completed, failed, or aborted.
- Tests failed and retry began.
- Memory write rejected or deduplicated.
- Context compaction started and completed.
- User interrupted or redirected work.

Routine successful green lights should collapse or stay out of the timeline.

### 3. Expandable Details

Answers: "What exactly happened?"

Details should be available on demand:

- Tool arguments.
- Command output tail.
- Error summary.
- Affected symbols or flows.
- Reasoning chain entry point.
- Discord embed payload details.

The default view should not require reading full tool output to understand the
turn.

### 4. Agent Task Story

Answers: "Why is Loom doing this?"

TaskList remains useful as the agent-authored plan and phase narrative. It should
align with heartbeat and timeline, but it should not replace runtime truth. If
TaskList says "testing" while the runtime is waiting for permission, runtime
state wins in the footer and blocking UI.

## Surface Mapping

CLI should use:

- Footer heartbeat for current runtime activity.
- Linear stream for important timeline events.
- Confirm/pause/redirect widgets for blocking user decisions.
- TaskList panel for agent-authored work narrative.
- Expandable or command-driven detail entry points for tool output and reasoning.

Discord should use:

- Compact embeds for important timeline events.
- Buttons for decisions.
- Thread summaries or pinned/collapsed summaries for current work state.
- Task progress embeds for agent-authored narrative.
- Follow-up messages only for events that need attention.

The same event should keep the same semantic identity across surfaces. For
example, "permission denied" is a warning timeline event whether it appears as a
CLI harness line or a Discord embed.

## Proposed Approaches

### Recommended: Event Language First, CLI Heartbeat as First Slice

Define the event grammar and source/importance/state rules first. Then implement
or refine CLI heartbeat as the first proof of the language.

This gives the best balance: the design is not just a local CLI polish pass, but
it also has a concrete first landing point.

### Alternative: CLI Heartbeat First

Improve `loom chat` immediately with spinner, elapsed time, contextual labels,
and TaskList alignment.

This has faster user-visible payoff, but risks growing CLI-specific language that
Discord later has to reinterpret.

### Alternative: Cross-Surface Mapping Table First

Write a complete CLI/Discord mapping matrix before any implementation.

This is useful for governance and consistency, but it may feel abstract unless
paired with a concrete surface improvement.

## Initial Implementation Slices

1. Inventory existing event emitters and current display paths.
2. Create a canonical event taxonomy or adapter layer for interaction language.
3. Add or refine CLI footer heartbeat labels, elapsed time, and animation states.
4. Align TaskList display with heartbeat without treating TaskList as runtime
   truth.
5. Add timeline rules for important events and quiet rules for routine green
   lights.
6. Map the same semantic events to Discord embeds, buttons, and thread summaries.

## Step 1 — Event Taxonomy

This taxonomy labels every user-visible event by the five axes from Event
Grammar. It is the source of truth that later code-level adapters consume. New
events added to the harness must be entered here before they reach a surface.

Sources are abbreviated as: `agent`, `harness`, `tool`, `cognition`,
`permission`, `memory`, `task`, `notification`, `platform`.

### Agent narrative events

| Event | Source | Importance | State | Intervention | Default surface |
|---|---|---|---|---|---|
| `TextChunk` (streamed assistant prose) | agent | ambient | active | none | timeline (CLI inline · Discord `⬥` flush) |
| `ThinkCollapsed` (reasoning summary) | agent | ambient | committed | optional inspect | timeline collapsed; `/think` to expand |
| `ReasoningContinuation` (max_tokens extension) | cognition | normal | active | none | timeline one-line hint |

### Tool and envelope events

| Event | Source | Importance | State | Intervention | Default surface |
|---|---|---|---|---|---|
| `ToolBegin` | tool | normal | active | none | heartbeat (label) + timeline (row) |
| `ToolEnd` (success) | tool | ambient | committed | optional inspect | timeline row, freeze after settle delay |
| `ToolEnd` (failure) | tool | warning | failed | optional inspect | timeline row, no freeze |
| `EnvelopeStarted` | harness | normal | active | none | timeline header (multi-node only) |
| `EnvelopeUpdated` | harness | ambient | active | none | heartbeat only (no timeline line) |
| `EnvelopeCompleted` (fulfilled) | harness | ambient | committed | optional inspect | timeline freeze |
| `EnvelopeCompleted` (partial / unfulfilled / pivoted) | harness | important | committed | optional inspect | timeline with outcome line |
| `ActionRolledBack` | harness | important | failed | optional inspect | timeline |
| `ActionStateChange` (declared / authorized / prepared / observed / validated / memorialized) | harness | ambient | various | none | suppressed; available in expandable detail |

### Turn lifecycle events

| Event | Source | Importance | State | Intervention | Default surface |
|---|---|---|---|---|---|
| Turn start (user input accepted) | platform | ambient | active | none | heartbeat transition |
| `TurnPaused` (HITL gate) | harness | blocking | pending | confirm required | modal widget (CLI) / buttons (Discord) |
| `TurnDropped` (stop_reason ≠ end_turn) | harness | warning | aborted | optional inspect | timeline |
| `TurnDone` | harness | important | committed | none | footer last-turn stats + summary |
| Turn cancelled (Ctrl-C / `/stop`) | platform | warning | aborted | none | timeline marker |

### Permission and scope events

| Event | Source | Importance | State | Intervention | Default surface |
|---|---|---|---|---|---|
| Tool confirm request | permission | blocking | pending | confirm required | modal (CLI widget · Discord buttons) |
| Confirm decision: allow once | permission | ambient | committed | none | suppressed |
| Confirm decision: lease (granted, TTL > 0) | permission | normal | committed | none | footer grant badge + timeline once |
| Confirm decision: auto-approve (class) | permission | important | committed | none | timeline (rare, important) |
| Confirm decision: deny | permission | warning | failed | none | timeline |
| Scope grant expired | permission | normal | stale | optional inspect | timeline (only when used recently) |
| Scope grant revoked | permission | normal | aborted | none | timeline |

### Memory and cognition events

| Event | Source | Importance | State | Intervention | Default surface |
|---|---|---|---|---|---|
| `CompressDone` (smart compaction) | memory | normal | committed | optional inspect | transient hint (CLI) · `-#` line (Discord) |
| Compaction in progress | memory | ambient | active | none | heartbeat (`⚡ 壓縮中`) |
| Memory write deduplicated | memory | ambient | committed | optional inspect | suppressed; counts roll up in turn summary |
| Memory write rejected (governance) | memory | warning | failed | optional inspect | timeline |
| `TierChanged` | cognition | important | committed | optional inspect | transient hint + timeline once |
| `TierExpiryHint` | cognition | warning | stale | optional inspect | footer badge color shift |

### Task and platform events

| Event | Source | Importance | State | Intervention | Default surface |
|---|---|---|---|---|---|
| Task list updated (`task_write`) | task | normal | various | optional inspect | TaskList panel (CLI) · task embed (Discord) |
| Task completed (last item) | task | normal | committed | none | TaskList collapse to `✓ N/N` |
| Notification fan-out (chime, interrupt, inject) | notification | important | various | various | Discord embed · CLI inline marker |
| Slash command accepted | platform | ambient | committed | none | timeline (own prefix) |
| Slash command unknown | platform | warning | failed | none | timeline |
| Welcome / session resume banner | platform | ambient | committed | none | session header (one-shot) |
| Stalled-tool hint | harness | warning | stale | optional inspect | heartbeat state change |

The same row drives both CLI and Discord rendering. Surface columns name the
abstract placement; Step 4 maps each placement to concrete CLI and Discord
components.

## Step 2 — Heartbeat Specification

Heartbeat is the L1 narrative layer. It is runtime-driven, never authored by
the LLM, and must continue to update even when the agent is silent.

### Heartbeat states

The heartbeat is a state machine with one state visible at a time. State
transitions are triggered by harness events, not by clock alone.

1. **idle** — no active turn. Footer shows brand and model only.
2. **thinking** — turn started, no tool yet. Label `Loom is thinking`, dotted
   animation, elapsed counter.
3. **tooling** — a tool is executing. Label derived from the tool's human
   action verb plus a short subject. Spinner. Elapsed counter.
4. **long_tooling** — same as tooling but elapsed crosses the long threshold.
   Animation softens (slower spinner or different glyph). Subscript may show
   the action that has been running longest.
5. **stalled** — no event of any kind has been received for the stale
   threshold. Label prepends `still waiting`. Color shifts to warning.
6. **compacting** — `CompressStarted` to `CompressDone`. Label `⚡ 壓縮 context`.
7. **paused_blocking** — HITL or confirm modal owns the screen. Heartbeat
   is suppressed and the modal is the sole focus.

The transient hints (cross-threshold warnings such as `context 80% — auto-compact
soon`) are not heartbeat states; they overlay the heartbeat briefly and then
expire.

### Tool to action label map

Heartbeat labels for `tooling` come from a static map keyed by tool name. This
is the system layer per Voice Rules: action verbs, not raw implementation names.
LLM authored content does not feed this label.

| Tool | Heartbeat label | Subject derivation |
|---|---|---|
| `read_file` | 查詢檔案 | `path` truncated |
| `write_file` | 寫入檔案 | `path` truncated |
| `edit_file` / `edit` | 編輯檔案 | `path` truncated |
| `run_bash` | 執行指令 | first word of `command`, no flags |
| `grep` / `ripgrep` | 搜尋 | `pattern` truncated |
| `find` / `glob` | 列檔 | `pattern` truncated |
| `gitnexus_*` | 查詢 GitNexus | tool suffix |
| `memorize` / governed write | 寫入記憶 | `type` if available |
| `task_write` | 更新任務 | none |
| `impact_analysis` | 分析影響 | `target` symbol |
| `compact` | 整理 context | none |
| MCP `<server>__<tool>` | label per server registry, fallback to tool name | first arg if string |
| unknown / new tool | tool name unchanged | none |

The map lives next to the harness, not in the LLM prompt. New tools fall back
to the raw name until they are registered.

### Stale threshold

Default stale threshold is 30 seconds with no harness event of any kind on the
active tool. Tools registered as long-runners (`run_bash`, `pytest`,
`gitnexus_*` reindex, MCP fetches) use 90 seconds. Threshold is configurable
per tool in the action label map.

When stalled, the heartbeat keeps the original label and prepends `still
waiting · `. It does not silently revert.

### Elapsed display

Elapsed time uses a one-decimal seconds counter under one minute and a
`mm:ss` form past one minute. Counter resolution is one second to avoid
visual flicker; the label and elapsed update on the same tick.

### CLI surface

The footer keeps its current geometry. The active envelope segment is
replaced by the heartbeat segment, with the system label on the left and
elapsed on the right. The existing `▸ name · elapsed` form is retired.

### Discord surface

Discord does not run a true heartbeat ticker because edit-rate limits and
channel economy would make it noisy. The Discord heartbeat proxy is:

- `channel.typing()` covers the `thinking` and `tooling` states.
- The active `status_msg` carries the current envelope intent and node list.
  Edits are still debounced at 0.5s as today.
- Only the `stalled` transition produces a visible signal: a one-time edit to
  the active `status_msg` adding `· still waiting Ns` so the user knows the
  silence is not the bot dying.

## Step 3 — Envelope Intent and Outcome

This is the L4 Agent Task Story layer at the envelope scale. The decision in
brainstorming was to anchor justification at the envelope level (Mode C). This
section specifies the contract.

### Data model additions

`EnvelopeStarted` gains:

- `intent: str` — one line, agent-authored, conversational tone. Required for
  multi-node envelopes; optional and ignored for single-node envelopes.
- `parallel_reason: enum | None` — planner-set, system label. Values:
  `serial`, `fan_out_replicas`, `fan_out_independent`,
  `competing_strategies`, `multi_target`, `unspecified`. Rendered as a small
  tag next to the intent, not as prose.

`EnvelopeCompleted` gains:

- `outcome: enum` — `fulfilled`, `partial`, `unfulfilled`, `pivoted`,
  `aborted`. Required at completion.
- `outcome_summary: str | None` — one line, agent-authored. Required when
  `outcome` is not `fulfilled` or `aborted` (aborted has its own treatment).

### Prompt contract

The system prompt instructs the agent that when it batches multiple tool calls
in one turn, it must precede the batch with a one-line intent and produce an
outcome judgement after the envelope's last tool returns. Single-tool
envelopes do not require either field.

The prompt does not provide a fixed template. Wording is free as long as the
result fits one display line.

### LLM cost

Per multi-node envelope the additional output is approximately fifteen tokens
for the intent and five to twenty tokens for the outcome and summary. For
single-tool envelopes the cost is zero. The expected steady-state overhead is
under one percent of turn output volume.

### Render rules (CLI)

- Single-node envelope: render the tool row only. No header.
- Multi-node envelope, active: print `▸ <intent>` plus `· <parallel_reason>`
  tag when not `serial` or `unspecified`. Below it, the node list.
- Multi-node envelope, fulfilled: header and node rows freeze to muted style
  after a short settle delay. No outcome line.
- Multi-node envelope, partial: header keeps accent; append a `◐ <summary>`
  outcome line.
- Multi-node envelope, unfulfilled: header turns warning; append `⚠ <summary>`.
- Multi-node envelope, pivoted: header turns informational accent; append
  `↪ <summary>`.
- Multi-node envelope, aborted: existing 🛑 treatment, no outcome line.

### Render rules (Discord)

- Single-node envelope: existing tool-row edit on `status_msg`.
- Multi-node envelope, active: `status_msg` opens with `▸ <intent>` and a
  parallel tag, then node lines. Debounced as today.
- Multi-node envelope, fulfilled: freeze the `status_msg`, open a fresh one.
- Multi-node envelope, partial / unfulfilled / pivoted: freeze the
  `status_msg` and append a follow-up persistent message with the outcome
  glyph and summary.
- Multi-node envelope, aborted: existing 🛑 treatment.

### Defensive fallbacks

When the LLM omits a required `intent` on a multi-node envelope, the harness
synthesises a placeholder from the parallel reason and the dominant tool
(`fan_out_replicas` over `pytest` becomes `重複跑同一組測試`). The placeholder
renders in muted style so the omission is visible without breaking the layout.

When the LLM omits a required `outcome`, the harness derives one mechanically:
all nodes succeeded means `fulfilled`; any failed or reverted node means at
least `partial`. The mechanical outcome is rendered as the same outcome glyph
but without a summary line.

## Step 4 — Discord Surface Mapping

Discord cannot mirror CLI ergonomics one to one. This table maps each abstract
surface in the taxonomy to a concrete Discord component, with a channel-economy
note where the cost is non-obvious.

| Abstract placement | Discord component | Notes |
|---|---|---|
| Footer heartbeat | `channel.typing()` plus `status_msg` content | Heartbeat is implicit; only stalled state writes a visible edit. |
| Timeline (ambient successes) | suppressed | Quiet by design. Counts roll up into the turn summary. |
| Timeline (normal events) | edit to active `status_msg` | Debounced; freezes when envelope completes. |
| Timeline (important events) | new `-#` persistent message | Includes tier changes, envelope outcomes that are not fulfilled, rollbacks, drops. |
| Timeline (warning events) | new persistent message without `-#` prefix | More visually present. Examples: deny, governance rejection, stalled. |
| Modal / widget (blocking) | `View` with buttons | Existing confirm flow; HITL pause uses message-wait protocol. |
| TaskList | dedicated task embed | Edited in place per task update. Distinct from `status_msg`. |
| Thread summary | turn-end summary message or embed | Existing `/summary on|off|detail` modes. |
| Transient hint | none — escalate or drop | Discord has no transient channel. Decide per event whether to drop (ambient) or escalate to important. |

## Metadata Gaps

These existing event types do not carry enough metadata to drive the surfaces
specified above. Each is a small additive change to the event payload, not a
breaking change to consumers.

- `ToolBegin` needs no schema change; the action-label map lives in the
  rendering layer and reads `tool_name` plus `args`.
- `EnvelopeStarted` needs `intent` and `parallel_reason` as defined in Step 3.
- `EnvelopeCompleted` needs `outcome` and `outcome_summary`.
- Long-running tools currently emit no progress event. Stale detection by
  timeout is acceptable for v1, but a periodic `ToolProgress` event (no
  payload required, just a heartbeat tick from inside the tool) would let
  long pytest or build runs be distinguished from genuine hangs. Optional
  for first slice; tracked under Open Questions.
- Permission `lease` decisions currently produce footer state but no timeline
  line. Add a one-time `PermissionLeaseGranted` event so the timeline reflects
  the decision moment, even though the badge remains the durable display.
- Memory dedup hits are not surfaced today. Add `MemoryDedupSkipped` (count
  per turn rolled into summary, not per event).

## Decisions Closed For First Implementation

Settled during slice 1 (PRs #425–#430). Each item is keyed back to the
implementation that locks it in so future-spec readers can find the seam.

1. **Stale thresholds — 30 s default, 90 s for known long-runners.** Locked
   in ``stale_threshold_for_tool`` (#416). The threshold is configurable per
   tool name in the same registry as the action labels, so adding a new
   long-runner is a one-line table edit.
2. **Single-node envelope header — skip the intent row.** ``Envelope <id> · N
   actions`` keeps a routine ``read_file`` quiet; the per-node line already
   names the tool, so an intent line would just duplicate it (#420).
3. **Outcome judgement — mechanical is system truth; LLM summary is display
   enrichment.** ``derive_envelope_outcome`` always runs at terminal status
   and writes ``view.outcome`` (#421). LLM-authored ``view.outcome_summary``
   is the warm display when present, but absence is silent — the renderer
   pairs the mechanical outcome with the glyph regardless.
4. **Empty ``outcome`` means unknown, never fulfilled.** Renderers infer
   ``UNFULFILLED`` from ``status == "failed"`` when the producer left
   outcome empty (#420 inference path). Locked by
   ``test_empty_outcome_infers_from_failed_status_without_claiming_success``.
5. **Heartbeat labels — precision wins over persona immersion.** The system
   layer stays neutral (``查詢檔案`` / ``執行指令``); personas adjust their own
   narration but do not retheme the heartbeat. Implemented in the static
   ``_LABELS_ZH_TW`` registry (#416), no persona hook.
6. **TaskList and envelope intent stay independent in v1.** Envelope intent
   describes one batch; TaskList spans turns. They may mirror text but do
   not link — kept simple to avoid runtime coupling between two
   agent-authored surfaces.
7. **Discord transient hints — suppress ambient, persist real warnings.**
   Concrete defaults: ``📍 turn N milestone`` suppressed on Discord;
   ``⚠️ context 80%`` persisted as a regular message. Same rule is
   implemented by the Discord status pipeline today.
8. **Default heartbeat language — Traditional Chinese.** Labels live in a
   locale-shaped registry (``_LABELS_ZH_TW``) so English can be added later
   as a sibling map without touching the resolver control flow (#416).
9. **``ToolProgress`` — deferred.** Stale detection in v1 is timeout-based
   via the heartbeat (CLI #419) and the Discord watchdog (#422). Adding a
   true progress event is high-value but doesn't block the rest of the
   slice; revisited in a later iteration.
10. **Mechanical outcome derivation never returns ``pivoted``.** Pivoted
    means the agent changed strategy on purpose — only LLM-authored
    judgement can claim it. The helper falls back to
    ``fulfilled / partial / unfulfilled / aborted`` (#421).
11. **Envelope metadata producer wiring — hybrid v1.** ``parallel_reason`` is
    dispatch-classified from the batch shape: identical repeated calls become
    ``fan_out_replicas``, same tool with different args becomes
    ``multi_target``, and mixed tools become ``fan_out_independent``. Agent
    intent uses a lightweight ``▸ `` marker in the text immediately before a
    multi-tool batch. For terminal multi-node envelopes whose mechanical
    outcome is not ``fulfilled`` or ``aborted``, the session asks the active
    model for one bounded Traditional Chinese outcome line before emitting
    ``EnvelopeCompleted`` (#431). Post-batch outcome glyph markers in the
    agent's normal follow-up are also captured for recent-envelope memory, but
    the user-visible completed row is produced before that follow-up exists.

## Deferred From First Implementation

Slice 1 intentionally narrowed scope on these items. Tracked separately so
the chain becomes complete in subsequent work.

- **Producer-side capture for envelope metadata — #431** (slice 2, landed).
  ``view.parallel_reason`` is now dispatch-classified, ``view.intent`` is
  captured from the agent's ``▸ `` marker before multi-tool batches, and
  ``view.outcome_summary`` is populated by a bounded active-model summary for
  non-fulfilled multi-node envelopes before Discord freezes the completed row.
- **``PermissionLeaseGranted`` event.** Not in slice 1. The footer's
  ``🔑 N·M:SS`` badge stays the durable display for active grants; the
  timeline-level lease event lands after heartbeat is settled.
- **``MemoryDedupSkipped`` event.** Not in slice 1. Dedup counts remain
  summary material until memory event surfacing is designed.
- **L3 expandable-details command.** Not in slice 1. ``/think`` remains the
  only explicit detail entry point.
- **``ToolProgress`` event.** See decision 9 above. Long-runner progress
  stays timeout-based for now.
- **English heartbeat labels.** Not in slice 1. The label registry shape
  must keep this additive (sibling ``_LABELS_EN_US`` map plus a locale
  selector).
- **Heartbeat ``compacting`` state.** Not in slice 1. Compaction display
  stays on the legacy ``FooterState.compacting`` flag (``⚡ 壓縮中…``); the
  enum value is intentionally absent until ``CompressStarted`` is wired
  into heartbeat.

## Non-Goals

- No return to the retired Textual TUI.
- No full dashboard as the default interaction surface.
- No stream spam for every successful internal event.
- No replacement of TaskList with runtime events.
- No attempt to make Discord and CLI visually identical.

## Open Questions

- What timeout should turn a quiet active operation into a still-waiting hint?
- Which existing events already carry enough context for user-facing action
  labels, and which need richer metadata?
- Should expandable details be driven by slash commands, inline toggles, or both?
- How should Discord represent heartbeat without annoying channel spam?
- Which labels should be localized or configurable?

## Test And Review Plan

Design review:

- Confirm that every important event has a source and placement.
- Confirm that routine green lights do not flood the stream.
- Confirm that blocking decisions are never hidden in ambient UI.
- Confirm that TaskList and runtime heartbeat have separate responsibilities.

Implementation review later:

- CLI heartbeat updates while long-running tools execute.
- Elapsed time remains accurate and does not flicker.
- Stalled hints appear only after the chosen threshold.
- Timeline events appear for warnings, denials, failures, interrupts, and key
  completions.
- Discord mappings preserve the same semantics with appropriate channel economy.
- Existing tests around CLI app, theme, Discord embeds, task reminders, and
  permission confirmations remain passing.
