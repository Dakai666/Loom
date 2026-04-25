"""Integration tests for Issue #154 — job tools + async_mode paths."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from loom.core.jobs import JobStore, Scratchpad
from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.core.session import _build_jobs_inject_message
from loom.platform.cli.tools import (
    make_fetch_url_tool,
    make_run_bash_tool,
    make_jobs_await_tool,
    make_jobs_cancel_tool,
    make_jobs_list_tool,
    make_jobs_status_tool,
    make_scratchpad_read_tool,
)


def _call(tool_name: str, args: dict) -> ToolCall:
    return ToolCall(
        id=f"call_{tool_name}",
        tool_name=tool_name,
        args=args,
        trust_level=TrustLevel.SAFE,
        session_id="test_session",
        abort_signal=None,
    )


# --- async_mode on run_bash -----------------------------------------


class TestRunBashAsyncMode:
    async def test_async_mode_submits_job_and_writes_scratchpad(self, tmp_path: Path):
        store = JobStore()
        pad = Scratchpad()
        tool = make_run_bash_tool(tmp_path, jobstore=store, scratchpad=pad)

        result = await tool.executor(_call("run_bash", {
            "command": "echo hello async",
            "justification": "test",
            "async_mode": True,
        }))
        assert result.success
        assert result.metadata["async"] is True
        job_id = result.metadata["job_id"]

        finished, _ = await store.await_jobs([job_id], timeout=5.0)
        assert len(finished) == 1
        job = finished[0]
        assert job.state.value == "done"
        assert job.result_ref
        body = pad.read(job.result_ref)
        assert "hello async" in body

    async def test_sync_mode_unchanged(self, tmp_path: Path):
        store = JobStore()
        pad = Scratchpad()
        tool = make_run_bash_tool(tmp_path, jobstore=store, scratchpad=pad)

        result = await tool.executor(_call("run_bash", {
            "command": "echo sync path",
            "justification": "test",
        }))
        assert result.success
        assert "sync path" in result.output
        assert not result.metadata.get("async", False)
        assert store.list_all() == []  # no jobs submitted

    async def test_async_mode_without_jobstore_falls_back(self, tmp_path: Path):
        """If jobstore/scratchpad aren't wired, async_mode silently degrades."""
        tool = make_run_bash_tool(tmp_path)  # no jobstore
        result = await tool.executor(_call("run_bash", {
            "command": "echo fallback",
            "justification": "test",
            "async_mode": True,
        }))
        assert result.success
        assert "fallback" in result.output


# --- jobs_* tools ----------------------------------------------------


class TestJobsTools:
    async def test_jobs_list_and_status(self):
        store = JobStore()

        async def quick():
            return "r1", "summary", None

        job_id = store.submit("demo", {}, quick)
        await store.await_jobs([job_id], timeout=1.0)

        list_tool = make_jobs_list_tool(store)
        status_tool = make_jobs_status_tool(store)

        lst = await list_tool.executor(_call("jobs_list", {}))
        payload = json.loads(lst.output)
        assert payload["count"] == 1
        assert payload["jobs"][0]["id"] == job_id
        assert payload["jobs"][0]["state"] == "done"
        assert payload["jobs"][0]["result_ref"] == "scratchpad://r1"

        s = await status_tool.executor(_call("jobs_status", {"job_id": job_id}))
        assert s.success
        body = json.loads(s.output)
        assert body["state"] == "done"
        assert body["summary"] == "summary"

    async def test_jobs_list_filter_active(self):
        store = JobStore()

        async def slow():
            await asyncio.sleep(5)
            return None, None, None

        async def quick():
            return None, "done", None

        s_id = store.submit("slow", {}, slow)
        q_id = store.submit("quick", {}, quick)
        await store.await_jobs([q_id], timeout=1.0)

        tool = make_jobs_list_tool(store)
        result = await tool.executor(_call("jobs_list", {"state": "active"}))
        payload = json.loads(result.output)
        ids = [j["id"] for j in payload["jobs"]]
        assert s_id in ids
        assert q_id not in ids

        store.cancel(s_id, reason="cleanup")

    async def test_jobs_status_unknown_id(self):
        store = JobStore()
        tool = make_jobs_status_tool(store)
        r = await tool.executor(_call("jobs_status", {"job_id": "job_nope"}))
        assert not r.success
        assert "Unknown" in r.error

    async def test_jobs_await_timeout(self):
        store = JobStore()

        async def slow():
            await asyncio.sleep(5)
            return None, None, None

        job_id = store.submit("slow", {}, slow)
        tool = make_jobs_await_tool(store)

        r = await tool.executor(_call("jobs_await", {
            "job_ids": [job_id],
            "timeout": 0.05,
        }))
        assert r.success
        payload = json.loads(r.output)
        assert payload["timeout_hit"] is True
        assert payload["finished"] == []
        assert len(payload["still_running"]) == 1

        store.cancel(job_id, reason="cleanup")

    async def test_jobs_cancel_requires_reason(self):
        store = JobStore()

        async def noop():
            return None, None, None

        job_id = store.submit("x", {}, noop)
        tool = make_jobs_cancel_tool(store)

        r = await tool.executor(_call("jobs_cancel", {"job_id": job_id}))
        assert not r.success
        assert "reason" in r.error.lower()

        r2 = await tool.executor(_call("jobs_cancel", {"job_id": job_id, "reason": "got it"}))
        assert r2.success
        # Note: job may have been DONE already when cancel_terminal is a no-op
        # so we don't strictly assert CANCELLED state here.

    async def test_jobs_await_requires_ids(self):
        store = JobStore()
        tool = make_jobs_await_tool(store)
        r = await tool.executor(_call("jobs_await", {}))
        assert not r.success


# --- scratchpad_read -----------------------------------------------


class TestScratchpadTool:
    async def test_read_by_ref(self):
        pad = Scratchpad()
        pad.write("log", "hello world")
        tool = make_scratchpad_read_tool(pad)

        r = await tool.executor(_call("scratchpad_read", {"ref": "log"}))
        assert r.success
        assert r.output == "hello world"

    async def test_read_strips_uri_prefix(self):
        pad = Scratchpad()
        pad.write("log", "abc")
        tool = make_scratchpad_read_tool(pad)
        r = await tool.executor(_call("scratchpad_read", {"ref": "scratchpad://log"}))
        assert r.success
        assert r.output == "abc"

    async def test_list_when_no_ref(self):
        pad = Scratchpad()
        pad.write("a", "1")
        pad.write("b", "2")
        tool = make_scratchpad_read_tool(pad)
        r = await tool.executor(_call("scratchpad_read", {}))
        assert r.success
        payload = json.loads(r.output)
        assert payload["available_refs"] == ["a", "b"]
        # Issue #197 Phase 2 review: discoverability — refs categorized so
        # agent can scan its folded state without trial-and-error reads.
        assert payload["by_kind"]["other"] == ["a", "b"]
        assert payload["by_kind"]["jit_spilled"] == []
        assert payload["by_kind"]["observation_masked"] == []
        assert payload["by_kind"]["subagent_failure"] == []

    async def test_list_groups_refs_by_producer_prefix(self):
        """Refs from JIT, masking, sub-agent failure paths each land in
        the right bucket so agents can navigate their folded state."""
        pad = Scratchpad()
        pad.write("auto_fetch_url_a3f7", "JIT-spilled body")
        pad.write("masked_run_bash_b2c9", "masked older call")
        pad.write("subagent_failure:sub-xyz", "sub-agent trace")
        pad.write("ad_hoc_note", "agent-written")
        tool = make_scratchpad_read_tool(pad)

        r = await tool.executor(_call("scratchpad_read", {}))

        payload = json.loads(r.output)
        assert payload["by_kind"]["jit_spilled"] == ["auto_fetch_url_a3f7"]
        assert payload["by_kind"]["observation_masked"] == ["masked_run_bash_b2c9"]
        assert payload["by_kind"]["subagent_failure"] == ["subagent_failure:sub-xyz"]
        assert payload["by_kind"]["other"] == ["ad_hoc_note"]

    async def test_missing_ref_error(self):
        pad = Scratchpad()
        tool = make_scratchpad_read_tool(pad)
        r = await tool.executor(_call("scratchpad_read", {"ref": "nope"}))
        assert not r.success

    async def test_default_max_bytes_caps_output(self):
        pad = Scratchpad()
        pad.write("big", "y" * 1_000_000)
        tool = make_scratchpad_read_tool(pad)
        r = await tool.executor(_call("scratchpad_read", {"ref": "big"}))
        assert r.success
        # Default cap is 200_000; trailing truncation notice adds a few bytes
        assert len(r.output) < 210_000
        assert "truncated" in r.output

    async def test_explicit_max_bytes_override(self):
        pad = Scratchpad()
        pad.write("med", "z" * 5_000)
        tool = make_scratchpad_read_tool(pad)
        r = await tool.executor(_call("scratchpad_read", {"ref": "med", "max_bytes": 1000}))
        assert r.success
        assert "truncated at 1000 bytes" in r.output

    async def test_max_bytes_rejects_non_int(self):
        pad = Scratchpad()
        pad.write("x", "hi")
        tool = make_scratchpad_read_tool(pad)
        r = await tool.executor(_call("scratchpad_read", {"ref": "x", "max_bytes": "many"}))
        assert not r.success
        assert "max_bytes" in r.error


# --- event injection helper ----------------------------------------


class TestJobsInjectMessage:
    async def test_empty_returns_none(self):
        store = JobStore()
        assert _build_jobs_inject_message(store) is None

    async def test_reports_completed(self):
        store = JobStore()

        async def quick():
            return "ref1", "1.2KB", None

        job_id = store.submit("fetch_url", {"url": "x"}, quick)
        await store.await_jobs([job_id], timeout=1.0)

        msg = _build_jobs_inject_message(store)
        assert msg is not None
        assert "[Jobs update]" in msg
        assert "Completed since last turn" in msg
        assert job_id in msg
        assert "fetch_url" in msg
        assert "scratchpad://ref1" in msg

    async def test_idempotent(self):
        store = JobStore()

        async def quick():
            return None, "ok", None

        job_id = store.submit("fn", {}, quick)
        await store.await_jobs([job_id], timeout=1.0)

        # First call reports it
        first = _build_jobs_inject_message(store)
        assert first and job_id in first

        # Second call: already reaped, nothing to say
        second = _build_jobs_inject_message(store)
        assert second is None

    async def test_reports_running(self):
        store = JobStore()

        async def slow():
            await asyncio.sleep(5)
            return None, None, None

        job_id = store.submit("slow_fn", {}, slow)
        await asyncio.sleep(0.02)  # let RUNNING transition happen

        msg = _build_jobs_inject_message(store)
        assert msg is not None
        assert "Still running" in msg
        assert job_id in msg
        assert "running" in msg

        store.cancel(job_id, reason="cleanup")

    async def test_reports_failure_and_cancellation(self):
        store = JobStore()

        async def boom():
            return None, None, "kaboom"

        async def slow():
            await asyncio.sleep(5)
            return None, None, None

        f_id = store.submit("f", {}, boom)
        s_id = store.submit("s", {}, slow)
        await store.await_jobs([f_id], timeout=1.0)
        store.cancel(s_id, reason="unneeded")

        msg = _build_jobs_inject_message(store)
        assert msg and "failed" in msg
        assert "kaboom" in msg
        assert "cancelled" in msg
        assert "unneeded" in msg
