---
name: async_jobs
description: Background job submission and Scratchpad for parallel IO (fetch_url, run_bash in async_mode). Use when multiple independent IO operations should not block sequentially.
tags: [core, concurrency, io]
---

# Async Jobs — Parallel IO without Sub-Agents

`async_mode=True` lets IO tools (`fetch_url`, `run_bash`) run in the
background and return a `job_id` immediately. Results land in a
session-scoped **Scratchpad**. The harness tells you which jobs finished
at turn boundaries — you never need to poll blindly.

## When to use

- Fetching multiple URLs whose bodies don't depend on each other
- Long-running shell commands (builds, test runs) where you have other
  work to do in parallel
- Any IO batch where serial execution wastes wall-clock time

**Do NOT use** for:
- Single operations — `async_mode=False` is simpler and faster for one-shot work
- Operations whose result you need **immediately** to decide the next step — just await
- Anything where the output is only a few hundred bytes — the bookkeeping overhead beats the latency win

## Tool chain

| Tool | Purpose |
|------|---------|
| `fetch_url(url, async_mode=True)` | Submit URL fetch; returns `{job_id}` |
| `run_bash(command, async_mode=True)` | Submit shell command; returns `{job_id}` |
| `jobs_list(state=?)` | List all jobs; optionally filter by `active`/`done`/`failed`/`cancelled` |
| `jobs_status(job_id)` | Full state for one job |
| `jobs_await(job_ids, timeout)` | Block until all complete or timeout (returns finished + still_running) |
| `jobs_cancel(job_id, reason)` | Cancel in-flight job — `reason` is mandatory |
| `scratchpad_read(ref, section?)` | Read the body; omit `ref` to list available refs |

## Typical flow (parallel fetch)

```
1. fetch_url(url=A, async_mode=True)  → job_a
2. fetch_url(url=B, async_mode=True)  → job_b
3. fetch_url(url=C, async_mode=True)  → job_c
4. jobs_await(job_ids=[a,b,c], timeout=60)
5. scratchpad_read(ref=job_a.result_ref)   # now go through the bodies
6. scratchpad_read(ref=job_b.result_ref)
7. scratchpad_read(ref=job_c.result_ref)
```

## Rules

- **Autonomy defaults to `async_mode=False`.** Autonomy has time; stability
  and predictability matter more than wall-clock wins. Only turn on
  `async_mode` in autonomy if the parallel benefit is clearly worth it
  and you explicitly `jobs_await` the results.
- **Cancel always requires a reason.** The trace is preserved in
  `jobs_list()` so you (and future turns) can see why it was stopped.
- **Trust/confirm happens at submit time**, not at reap. A denied job
  won't be submitted, so you won't end up holding a stale job_id.
- **Scratchpad is ephemeral** — cleared on session end. If you need the
  content long-term, pull it with `scratchpad_read` and write it to a
  file (or semantic memory) while the session is still open.
- **Harness injects a jobs update** before you send a final response,
  whenever a job has newly completed or is still running. No polling
  required — just react to the reminder when it arrives.

## Cancel on session end

Session stop calls `cancel_all(reason="session_ended")` automatically;
Scratchpad is cleared. Any in-flight work at that point is lost — if
stability matters more than parallelism for this task, just use
synchronous mode.
