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
