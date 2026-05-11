# Daily Safety-Net Compaction Design

## Context

Episodic-to-semantic compaction currently runs from the turn-driven subscriber:
`LoomSession._compaction_subscriber_loop()` listens for ledger `turn_end` events,
then calls `_run_compaction_check()`. That path protects normal active sessions,
but it leaves Discord sessions with sparse or interrupted traffic exposed:
episodic entries can sit uncompressed for days when the threshold is not reached,
when no new turn occurs, or when the bot restarts before a final turn-driven check.

PR #355 added time-aware reads as a recovery path. This change adds the write-side
safety net so long-running Discord bots periodically promote idle episodic memory
into semantic memory.

## Goals

- Add `LoomSession.force_compact()` as a public async method that runs the same
  compression path as `_run_compaction_check()` without the episodic threshold gate.
- Keep all compaction serialized by the existing `_compaction_lock`, including
  concurrent forced and turn-driven triggers.
- Add a Discord daily task that calls `force_compact()` on every in-memory
  `LoomSession` at 05:00 in the bot host's local timezone.
- Add a resume-time safety pass: when Discord lazily resumes a thread into an
  in-memory session, run `force_compact()` once so historical uncompressed entries
  for that resumed session are handled.
- Preserve the existing turn-driven threshold logic and event display behavior.

## Non-Goals

- Do not compact sessions that are only present in SQLite but not currently loaded
  as a `LoomSession`; that needs a standalone compressor bootstrap and is a later
  iteration.
- Do not add idle-minute timers or CLI/TUI idle compaction.
- Do not change the default `episodic_compress_threshold` or the subscriber's
  turn-driven behavior.

## Architecture

`LoomSession.force_compact()` will acquire `_compaction_lock` and call a shared
private compaction helper with `force=True`. `_run_compaction_check()` will acquire
the same lock and call the helper with `force=False`. The helper owns the common
count, `compress_session`, pending `CompressDone`, refresh, and exception logging
logic. This avoids duplicating the LLM compression path while making the lock
contract explicit for all callers.

The subscriber loop will stop taking the lock itself and simply call
`_run_compaction_check()`, because the method will become the lock boundary. This
lets tests and external callers exercise the same serialization behavior without
needing to know about internal locking.

`LoomDiscordBot` will create a `discord.ext.tasks.loop` instance in `__init__`,
start it from `on_ready()` if it is not already running, and cancel it from a new
`close()` method before closing all active sessions and the Discord client. The
loop body will snapshot `self._sessions.items()` and call `force_compact()` on
each session independently, logging failures and continuing with the rest.

The resume-time pass will run near the end of `_start_session()`, after the
session has started and Discord-specific tools/middleware are installed but before
the session is returned to message handling. This targets only sessions that are
already in memory, matching the issue's scope.

## Error Handling

Forced compaction should be best-effort. A failed session logs the exception and
does not stop the daily pass. `LoomSession` keeps the existing behavior where
`compress_session` failures are swallowed and logged, because compaction must not
break user turns or Discord startup.

## Testing

- Extend `tests/test_memory_compaction_subscriber.py` with a failing test proving
  `force_compact()` compresses below threshold and appends `CompressDone`.
- Add a concurrency test proving a forced compaction and threshold check cannot
  run `compress_session` at the same time.
- Add Discord bot tests covering the daily safety pass over in-memory sessions
  and the resume-time pass in `_start_session()`.
- Re-run the focused memory/session/Discord tests plus the existing memory suite.
