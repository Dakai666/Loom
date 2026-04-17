---
name: task_list
description: Multi-step task planning and tracking for the main agent. Use task_plan when a goal needs multiple coordinated steps.
tags: [core, planning, tracking]
---

# TaskList — Core Planning Tool

TaskList is a **cognitive exoskeleton** for the main agent — a checklist you maintain yourself to stay on track during complex multi-step tasks. It is NOT a sub-agent scheduler.

## When to use it

Use `task_plan` when you face a goal that requires more than 2–3 tool calls with dependencies between steps. Examples:
- A multi-file refactor with a known sequence
- A research task with distinct analysis phases
- Any task where forgetting a sub-step would be costly

**Do NOT use it** for simple one-off actions.

## Tool Chain

| Tool | Purpose |
|------|---------|
| `task_plan` | Create the list with task nodes |
| `task_status` | Check current state (who's pending/done/failed) |
| `task_modify` | Add/remove/update nodes mid-flight |
| `task_done` | Mark a node completed (with result) or failed (with error) |
| `task_read` | Pull full result of a completed node (supports section=head/tail/N-M/keyword) |

## Important Rules

- **`depends_on` is documentation only** — harness does not schedule based on it. You read it and decide the order.
- **Result size limit: 5000 chars** — if your output is longer, write it to a file first and put only the path/summary in the result.
- **Pre-final-response self-check** — if you try to end your turn with pending nodes, the system will inject a reminder. You must either continue executing or call `task_done(node_id=..., error="reason")` to explicitly abandon.
- **One agent turn ≈ one node** — do not try to finish 3 nodes in a single assistant message.
- **If the task is no longer viable**: call `task_done(node_id=..., error="reason")` for each pending node. Do not silently leave work undone.

## Result Truncation

When `task_done(result=...)` exceeds 5000 chars, it is hard-truncated. The system adds a notice pointing to the file path — write long outputs to disk and record only the path in the result.

## Abandoning the Whole List

Currently there is no `task_abandon` tool. To abandon: call `task_done(node_id=..., error="reason")` for each active node. Follow-up Issue #154 may add a dedicated abandon tool.
