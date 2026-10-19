"""Tests for Issue #154 — JobStore + Scratchpad."""

from __future__ import annotations

import asyncio

import pytest

from loom.core.jobs import Job, JobState, JobStore, Scratchpad


async def _await_terminal(store: JobStore, job_id: str, timeout: float = 1.0) -> Job:
    await store.await_jobs([job_id], timeout=timeout)
    job = store.get(job_id)
    assert job is not None
    assert job.is_terminal
    return job


# --- Scratchpad ------------------------------------------------------


class TestScratchpad:
    def test_write_returns_uri(self):
        pad = Scratchpad()
        uri = pad.write("abc", "hello")
        assert uri == "scratchpad://abc"

    def test_read_roundtrip_text(self):
        pad = Scratchpad()
        pad.write("abc", "hello world")
        assert pad.read("abc") == "hello world"

    def test_read_accepts_full_uri(self):
        pad = Scratchpad()
        pad.write("abc", "data")
        assert pad.read("scratchpad://abc") == "data"

    def test_read_accepts_single_colon_uri(self):
        """Issue #243: JIT/masking placeholders used ``scratchpad:ref`` (single
        colon) but ``_strip_uri`` only handled ``scratchpad://``. Agents that
        copied the ref from placeholder text got KeyError on lookup."""
        pad = Scratchpad()
        pad.write("auto_read_file_abcdef", "big content")
        # Single-colon form — the format JIT placeholders originally emitted
        assert pad.read("scratchpad:auto_read_file_abcdef") == "big content"
        assert pad.size("scratchpad:auto_read_file_abcdef") == len(b"big content")
        assert "scratchpad:auto_read_file_abcdef" in pad

    def test_read_bytes_decode(self):
        pad = Scratchpad()
        pad.write("b", b"bytes payload")
        assert pad.read("b") == "bytes payload"

    def test_size_counts_bytes(self):
        pad = Scratchpad()
        pad.write("x", "héllo")
        assert pad.size("x") == len("héllo".encode("utf-8"))

    def test_missing_ref_raises(self):
        pad = Scratchpad()
        with pytest.raises(KeyError):
            pad.read("nope")

    def test_invalid_ref_rejected(self):
        pad = Scratchpad()
        with pytest.raises(ValueError):
            pad.write("with/slash", "x")
        with pytest.raises(ValueError):
            pad.write("", "x")
        with pytest.raises(ValueError):
            pad.write(".hidden", "x")

    def test_clear_removes_all(self):
        pad = Scratchpad()
        pad.write("a", "1")
        pad.write("b", "2")
        pad.clear()
        assert pad.list_refs() == []

    def test_list_refs_sorted(self):
        pad = Scratchpad()
        pad.write("zeta", "z")
        pad.write("alpha", "a")
        pad.write("mid", "m")
        assert pad.list_refs() == ["alpha", "mid", "zeta"]

    def test_contains(self):
        pad = Scratchpad()
        pad.write("x", "1")
        assert "x" in pad
        assert "scratchpad://x" in pad
        assert "nope" not in pad

    def test_head(self):
        pad = Scratchpad()
        body = "\n".join(f"line-{i}" for i in range(1, 101))
        pad.write("log", body)
        result = pad.read("log", head=50)
        assert result.splitlines()[:50] == [f"line-{i}" for i in range(1, 51)]
        assert "line-51" not in result

    def test_tail(self):
        pad = Scratchpad()
        body = "\n".join(f"line-{i}" for i in range(1, 101))
        pad.write("log", body)
        result = pad.read("log", tail=50)
        assert result.splitlines()[:50] == [f"line-{i}" for i in range(51, 101)]

    def test_lines_range(self):
        pad = Scratchpad()
        pad.write("log", "a\nb\nc\nd\ne")
        assert pad.read("log", lines="2-4").startswith("b\nc\nd\n")

    def test_grep(self):
        pad = Scratchpad()
        pad.write("log", "apple\nbanana\napricot\ncarrot")
        assert pad.read("log", grep="ap").startswith("apple\napricot\n")

    def test_lines_zero_lower_bound_clamps_to_one(self):
        """Issue #302 F2: "0-N" reads from the start as the caller intended."""
        pad = Scratchpad()
        pad.write("log", "\n".join(f"line-{i}" for i in range(1, 21)))
        result = pad.read("log", lines="0-5")
        assert result.splitlines()[:5] == [f"line-{i}" for i in range(1, 6)]
        assert "line-6" not in result

    def test_lines_past_end_returns_remaining(self):
        pad = Scratchpad()
        pad.write("log", "\n".join(f"line-{i}" for i in range(1, 11)))
        result = pad.read("log", lines="8-9999")
        assert result.splitlines()[:3] == [f"line-{i}" for i in range(8, 11)]

    @pytest.mark.parametrize("bad", ["5-2", "abc", "3", "1-x"])
    def test_lines_malformed_raises(self, bad):
        """Malformed / inverted ranges are caller errors, not silent empties."""
        pad = Scratchpad()
        pad.write("log", "a\nb")
        with pytest.raises(ValueError):
            pad.read("log", lines=bad)

    def test_filters_are_mutually_exclusive(self):
        pad = Scratchpad()
        pad.write("log", "a\nb")
        with pytest.raises(ValueError):
            pad.read("log", head=1, grep="a")

    # --- 2026-10-19 section-filter diagnosis (T1 / T3 / T4) ----------

    def test_hyphenated_grep_is_keyword_not_range(self):
        """T4: a hyphenated keyword like "2024-2025" must match lines that
        contain it — never be reinterpreted as a line range."""
        pad = Scratchpad()
        body = "\n".join(f"filler {i}" for i in range(1, 3001))
        body += "\nthe 2024-2025 cohort"
        pad.write("log", body)
        result = pad.read("log", grep="2024-2025")
        assert "the 2024-2025 cohort" in result
        assert "filler 2024" not in result

    def test_tail_reaches_real_end_of_large_body(self):
        """T1: tail must return the end of the whole body, even when the
        body exceeds max_bytes. max_bytes caps the output, not the input."""
        pad = Scratchpad()
        pad.write("log", "\n".join(f"line-{i:05d}" for i in range(1, 3001)))
        result = pad.read("log", tail=50, max_bytes=30_000)
        assert "line-03000" in result
        assert "line-00723" not in result

    def test_grep_searches_beyond_max_bytes(self):
        """T1 corollary: grep sees the full body, not a byte-prefix of it."""
        pad = Scratchpad()
        pad.write("log", "x" * 100_000 + "\nneedle here")
        assert "needle here" in pad.read("log", grep="needle", max_bytes=1000)

    def test_zero_matches_is_reported_not_disguised_as_truncation(self):
        """T3: no match must say so, and must not carry a truncation notice
        that invites a futile max_bytes retry."""
        pad = Scratchpad()
        pad.write("log", "y\n" * 50_000)
        result = pad.read("log", grep="NonexistentTerm", max_bytes=30_000)
        assert "matched 0" in result
        assert "truncated" not in result

    def test_match_count_reported(self):
        pad = Scratchpad()
        pad.write("log", "apple\nbanana\napricot")
        assert "matched 2 of 3 lines" in pad.read("log", grep="ap")

    def test_truncation_never_splits_utf8_char(self):
        """T1 second harm: byte-cutting mid-character used to inject U+FFFD."""
        pad = Scratchpad()
        pad.write("doc", "中文內容" * 10_000)
        result = pad.read("doc", max_bytes=30_001)  # 3-byte chars: odd cut
        assert "\ufffd" not in result
        assert "truncated" in result

    def test_max_bytes_truncates_with_notice(self):
        pad = Scratchpad()
        pad.write("big", "x" * 1000)
        result = pad.read("big", max_bytes=100)
        assert result.startswith("x" * 100)
        assert "truncated at 100 of 1000 bytes" in result

    def test_max_bytes_not_applied_when_under(self):
        pad = Scratchpad()
        pad.write("small", "tiny")
        assert pad.read("small", max_bytes=1000) == "tiny"

    def test_max_bytes_applies_after_filter(self):
        pad = Scratchpad()
        pad.write("log", "aaa\nbbb\nccc\nddd")
        result = pad.read("log", head=3, max_bytes=7)
        assert result.startswith("aaa\nbbb")
        assert "ccc" not in result.split("\n\n[")[0]
        assert "truncated" in result


# --- JobStore: submission + completion -------------------------------


class TestJobStoreSubmit:
    async def test_submit_returns_id_and_runs(self):
        store = JobStore()

        async def run():
            return "ref_done", "42 bytes", None

        job_id = store.submit("fake_fn", {"a": 1}, run)
        assert job_id.startswith("job_")

        job = await _await_terminal(store, job_id)
        assert job.state == JobState.DONE
        assert job.result_ref == "ref_done"
        assert job.result_summary == "42 bytes"
        assert job.error is None
        assert job.started_at is not None
        assert job.finished_at is not None

    async def test_submit_captures_exception(self):
        store = JobStore()

        async def boom():
            raise RuntimeError("kapow")

        job_id = store.submit("fake_fn", {}, boom)

        job = await _await_terminal(store, job_id)
        assert job.state == JobState.FAILED
        assert "kapow" in job.error

    async def test_submit_soft_error(self):
        """Factory returns error in the third tuple slot — job goes FAILED."""
        store = JobStore()

        async def soft_fail():
            return None, None, "permission denied"

        job_id = store.submit("fake_fn", {}, soft_fail)

        job = await _await_terminal(store, job_id)
        assert job.state == JobState.FAILED
        assert job.error == "permission denied"


# --- JobStore: reaping ----------------------------------------------


class TestJobStoreReap:
    async def test_reap_since_last_idempotent(self):
        store = JobStore()

        async def quick():
            return "r", "done", None

        job_id = store.submit("fake", {}, quick)
        await _await_terminal(store, job_id)

        new, running = store.reap_since_last()
        assert [j.id for j in new] == [job_id]
        assert running == []

        # Second reap: already reported, should be empty
        new2, running2 = store.reap_since_last()
        assert new2 == []
        assert running2 == []

    async def test_reap_reports_running(self):
        store = JobStore()
        started = asyncio.Event()
        gate = asyncio.Event()

        async def slow():
            started.set()
            await gate.wait()
            return "r", "done", None

        job_id = store.submit("slow", {}, slow)
        await asyncio.wait_for(started.wait(), timeout=1.0)

        new, running = store.reap_since_last()
        assert new == []
        assert [j.id for j in running] == [job_id]

        gate.set()
        await _await_terminal(store, job_id)

        new, running = store.reap_since_last()
        assert [j.id for j in new] == [job_id]
        assert running == []

    async def test_list_active_excludes_terminal(self):
        store = JobStore()

        async def quick():
            return None, "ok", None

        slow_started = asyncio.Event()
        slow_gate = asyncio.Event()

        async def slow():
            slow_started.set()
            await slow_gate.wait()
            return None, "nope", None

        q = store.submit("q", {}, quick)
        s = store.submit("s", {}, slow)

        await _await_terminal(store, q)
        await asyncio.wait_for(slow_started.wait(), timeout=1.0)

        active_ids = [j.id for j in store.list_active()]
        assert q not in active_ids
        assert s in active_ids

        store.cancel(s, reason="cleanup")


# --- JobStore: cancel ------------------------------------------------


class TestJobStoreCancel:
    async def test_cancel_requires_reason(self):
        store = JobStore()

        async def noop():
            return None, None, None

        job_id = store.submit("x", {}, noop)
        with pytest.raises(ValueError):
            store.cancel(job_id, "")

    async def test_cancel_preserves_trace(self):
        store = JobStore()
        started = asyncio.Event()
        gate = asyncio.Event()

        async def slow():
            started.set()
            await gate.wait()
            return None, None, None

        job_id = store.submit("s", {}, slow)
        await asyncio.wait_for(started.wait(), timeout=1.0)

        store.cancel(job_id, reason="user_abort")

        job = store.get(job_id)
        assert job.state == JobState.CANCELLED
        assert job.cancel_reason == "user_abort"
        assert job.finished_at is not None

        # Cancelled jobs are still in list_all
        assert any(j.id == job_id for j in store.list_all())

    async def test_cancel_terminal_is_noop(self):
        store = JobStore()

        async def quick():
            return None, None, None

        job_id = store.submit("q", {}, quick)
        await _await_terminal(store, job_id)

        # Already DONE — cancel should be silent
        store.cancel(job_id, reason="too_late")
        assert store.get(job_id).state == JobState.DONE
        assert store.get(job_id).cancel_reason is None

    async def test_cancel_unknown_raises(self):
        store = JobStore()
        with pytest.raises(KeyError):
            store.cancel("job_nonexistent", reason="x")

    async def test_cancel_all(self):
        store = JobStore()
        started_count = 0
        all_started = asyncio.Event()
        gate = asyncio.Event()

        async def slow():
            nonlocal started_count
            started_count += 1
            if started_count == 3:
                all_started.set()
            await gate.wait()
            return None, None, None

        ids = [store.submit(f"s{i}", {}, slow) for i in range(3)]
        await asyncio.wait_for(all_started.wait(), timeout=1.0)

        await store.cancel_all(reason="session_ended")

        for jid in ids:
            job = store.get(jid)
            assert job.state == JobState.CANCELLED
            assert job.cancel_reason == "session_ended"

    async def test_cancel_all_requires_reason(self):
        store = JobStore()
        with pytest.raises(ValueError):
            await store.cancel_all("")


# --- JobStore: await -------------------------------------------------


class TestJobStoreAwait:
    async def test_await_all_complete(self):
        store = JobStore()

        async def quick():
            return None, "ok", None

        ids = [store.submit("q", {}, quick) for _ in range(3)]
        finished, running = await store.await_jobs(ids, timeout=1.0)
        assert len(finished) == 3
        assert running == []

    async def test_await_timeout_returns_running(self):
        store = JobStore()
        started = asyncio.Event()
        gate = asyncio.Event()

        async def slow():
            started.set()
            await gate.wait()
            return None, None, None

        job_id = store.submit("s", {}, slow)
        await asyncio.wait_for(started.wait(), timeout=1.0)
        finished, running = await store.await_jobs([job_id], timeout=0.05)
        assert finished == []
        assert [j.id for j in running] == [job_id]

        store.cancel(job_id, reason="cleanup")

    async def test_await_unknown_ids_silent(self):
        store = JobStore()
        finished, running = await store.await_jobs(["job_nope"], timeout=0.1)
        assert finished == []
        assert running == []
