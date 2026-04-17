"""
JobStore — session-scoped async job registry.

Lets tools submit background work (IO-heavy operations) and return a
job_id immediately, instead of forcing the agent to block. The harness
reaps completed jobs at turn boundaries and injects a status update so
the agent can notice progress without polling.

Design origin: Issue #154.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable


class JobState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = (JobState.DONE, JobState.FAILED, JobState.CANCELLED)


@dataclass
class Job:
    id: str
    fn_name: str
    args: dict[str, Any]
    state: JobState = JobState.PENDING
    submitted_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result_ref: str | None = None           # scratchpad URI, if any
    result_summary: str | None = None       # short blurb for the inject message
    error: str | None = None
    cancel_reason: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def elapsed_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or time.time()
        return end - self.started_at


class JobStore:
    """Registry of async jobs scoped to a single session.

    Not thread-safe. All mutation happens on the session's asyncio loop.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._reaped_ids: set[str] = set()

    # -- submission ----------------------------------------------------

    def submit(
        self,
        fn_name: str,
        args: dict[str, Any],
        coro_factory: Callable[[], Awaitable[tuple[str | None, str | None, str | None]]],
    ) -> str:
        """Submit an async job.

        ``coro_factory`` is a zero-arg callable that returns a fresh coroutine
        each call. When awaited, it must resolve to a tuple:
            (result_ref, result_summary, error)
        where at most one of result_ref/error is non-None.

        Returns the job_id.
        """
        job_id = f"job_{uuid.uuid4().hex[:8]}"
        job = Job(id=job_id, fn_name=fn_name, args=dict(args))
        self._jobs[job_id] = job

        task = asyncio.create_task(self._run(job_id, coro_factory))
        self._tasks[job_id] = task
        return job_id

    async def _run(
        self,
        job_id: str,
        coro_factory: Callable[[], Awaitable[tuple[str | None, str | None, str | None]]],
    ) -> None:
        job = self._jobs[job_id]
        job.state = JobState.RUNNING
        job.started_at = time.time()
        try:
            result_ref, summary, error = await coro_factory()
        except asyncio.CancelledError:
            # Cooperative cancel — cancel() already wrote the reason.
            raise
        except Exception as exc:
            job.finished_at = time.time()
            job.state = JobState.FAILED
            job.error = f"{type(exc).__name__}: {exc}"
            return
        job.finished_at = time.time()
        if error:
            job.state = JobState.FAILED
            job.error = error
        else:
            job.state = JobState.DONE
            job.result_ref = result_ref
            job.result_summary = summary

    # -- inspection ----------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_active(self) -> list[Job]:
        return [j for j in self._jobs.values() if not j.is_terminal]

    def list_all(self) -> list[Job]:
        return list(self._jobs.values())

    # -- reaping -------------------------------------------------------

    def reap_since_last(self) -> tuple[list[Job], list[Job]]:
        """Return (newly_finished, still_running) since the last call.

        Idempotent: jobs that were already reported as finished in a prior
        call are excluded from ``newly_finished``.
        """
        new_finished: list[Job] = []
        for job in self._jobs.values():
            if job.is_terminal and job.id not in self._reaped_ids:
                new_finished.append(job)
                self._reaped_ids.add(job.id)
        running = [j for j in self._jobs.values() if not j.is_terminal]
        return new_finished, running

    # -- cancellation --------------------------------------------------

    def cancel(self, job_id: str, reason: str) -> None:
        if not reason:
            raise ValueError("cancel() requires a non-empty reason")
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"Unknown job_id: {job_id}")
        if job.is_terminal:
            return
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        job.state = JobState.CANCELLED
        job.cancel_reason = reason
        job.finished_at = time.time()

    async def cancel_all(self, reason: str) -> None:
        if not reason:
            raise ValueError("cancel_all() requires a non-empty reason")
        active_ids = [j.id for j in self.list_active()]
        for jid in active_ids:
            self.cancel(jid, reason)
        if self._tasks:
            await asyncio.gather(
                *(t for t in self._tasks.values() if not t.done()),
                return_exceptions=True,
            )

    # -- awaits --------------------------------------------------------

    async def await_jobs(
        self, job_ids: list[str], timeout: float | None = None
    ) -> tuple[list[Job], list[Job]]:
        """Wait for all given jobs to terminate (or timeout).

        Returns (finished, still_running). Does not raise on timeout.
        Unknown IDs are silently skipped.
        """
        tasks = [self._tasks[jid] for jid in job_ids if jid in self._tasks]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        finished = [self._jobs[jid] for jid in job_ids if jid in self._jobs and self._jobs[jid].is_terminal]
        running = [self._jobs[jid] for jid in job_ids if jid in self._jobs and not self._jobs[jid].is_terminal]
        return finished, running
