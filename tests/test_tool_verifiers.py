"""
Tests for tool post_validators (Issue #196).

Covers run_bash heuristic verifier directly (pure function, many cases) and
write_file / memorize verifiers via their ToolDefinition executors.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from loom.core.harness.middleware import ToolCall, ToolResult, VerifierResult
from loom.core.harness.permissions import TrustLevel
from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.facade import MemoryFacade
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.relational import RelationalMemory
from loom.core.memory.search import MemorySearch
from loom.core.memory.semantic import SemanticMemory
from loom.core.memory.store import SQLiteStore
from loom.platform.cli.tools import (
    _verify_fetch_url,
    _verify_run_bash,
    _verify_spawn_agent,
    make_filesystem_tools,
    make_memorize_tool,
    sanitize_untrusted_text,
)


def _call(tool_name: str = "run_bash", **args) -> ToolCall:
    return ToolCall(
        id=f"call_{tool_name}",
        tool_name=tool_name,
        args=args,
        trust_level=TrustLevel.GUARDED,
        session_id="test-session",
    )


def _ok_result(output: str) -> ToolResult:
    return ToolResult(
        call_id="call_run_bash", tool_name="run_bash",
        success=True, output=output,
    )


class TestRunBashVerifier:
    """Heuristic verifier for run_bash catches exit-0 silent failures."""

    async def test_passes_on_empty_output(self) -> None:
        verdict = await _verify_run_bash(_call(), _ok_result(""))
        assert verdict.passed is True

    async def test_passes_on_benign_output_mentioning_error(self) -> None:
        """False-positive guard: `grep error file.log` finding the word 'error'
        should not flag as failure."""
        output = "line with the word error in it\nanother line"
        verdict = await _verify_run_bash(_call(), _ok_result(output))
        assert verdict.passed is True

    async def test_passes_on_clean_pytest_summary(self) -> None:
        output = "======= 10 passed in 0.34s ======="
        verdict = await _verify_run_bash(_call(), _ok_result(output))
        assert verdict.passed is True

    async def test_detects_python_traceback(self) -> None:
        output = (
            "Starting script...\n"
            "Traceback (most recent call last):\n"
            "  File \"x.py\", line 1, in <module>\n"
            "    raise ValueError('boom')\n"
            "ValueError: boom\n"
        )
        verdict = await _verify_run_bash(_call(), _ok_result(output))
        assert verdict.passed is False
        assert verdict.signal == "python_traceback"
        assert "traceback" in (verdict.reason or "").lower()

    async def test_detects_pytest_failed_summary(self) -> None:
        output = "======= 1 failed, 3 passed in 0.12s ======="
        verdict = await _verify_run_bash(_call(), _ok_result(output))
        assert verdict.passed is False
        assert verdict.signal == "pytest_failed"
        assert "1 failing test" in (verdict.reason or "")

    async def test_detects_js_test_failure_summary(self) -> None:
        output = (
            "Test Suites: 1 failed, 2 passed, 3 total\n"
            "Tests:       2 failed, 5 passed, 7 total"
        )
        verdict = await _verify_run_bash(_call(), _ok_result(output))
        assert verdict.passed is False
        assert verdict.signal == "js_test_failed"

    async def test_detects_go_test_fail_marker(self) -> None:
        output = (
            "=== RUN   TestFoo\n"
            "--- FAIL: TestFoo (0.01s)\n"
            "    foo_test.go:10: expected 2 got 3\n"
        )
        verdict = await _verify_run_bash(_call(), _ok_result(output))
        assert verdict.passed is False
        assert verdict.signal == "go_test_failed"

    async def test_detects_tsc_error_lines(self) -> None:
        output = (
            "src/foo.ts(10,5): error TS2345: Argument of type 'string' "
            "is not assignable to parameter of type 'number'.\n"
        )
        verdict = await _verify_run_bash(_call(), _ok_result(output))
        assert verdict.passed is False
        assert verdict.signal == "tsc_error"

    async def test_detects_command_not_found(self) -> None:
        output = "bash: xxxnonexistent: command not found"
        verdict = await _verify_run_bash(_call(), _ok_result(output))
        assert verdict.passed is False
        assert verdict.signal == "cmd_not_found"

    async def test_detects_no_such_file(self) -> None:
        output = "cat: missing.txt: No such file or directory"
        verdict = await _verify_run_bash(_call(), _ok_result(output))
        assert verdict.passed is False
        assert verdict.signal == "cmd_not_found"


class TestWriteFileVerifier:
    """write_file post_validator does a content roundtrip check."""

    async def test_passes_on_correct_roundtrip(self, tmp_path: Path) -> None:
        tools = {t.name: t for t in make_filesystem_tools(tmp_path)}
        write = tools["write_file"]
        call = ToolCall(
            id="c1", tool_name="write_file",
            args={"path": "hello.txt", "content": "hi there"},
            trust_level=TrustLevel.GUARDED, session_id="s",
        )
        result = await write.executor(call)
        assert result.success

        verdict = await write.post_validator(call, result)
        assert verdict.passed is True

    async def test_fails_when_file_deleted_between_write_and_verify(
        self, tmp_path: Path,
    ) -> None:
        tools = {t.name: t for t in make_filesystem_tools(tmp_path)}
        write = tools["write_file"]
        call = ToolCall(
            id="c1", tool_name="write_file",
            args={"path": "doomed.txt", "content": "bye"},
            trust_level=TrustLevel.GUARDED, session_id="s",
        )
        result = await write.executor(call)
        assert result.success

        # Simulate disappearance between write and verify.
        (tmp_path / "doomed.txt").unlink()

        verdict = await write.post_validator(call, result)
        assert verdict.passed is False
        assert verdict.signal == "file_missing"

    async def test_fails_on_content_mismatch(self, tmp_path: Path) -> None:
        tools = {t.name: t for t in make_filesystem_tools(tmp_path)}
        write = tools["write_file"]
        call = ToolCall(
            id="c1", tool_name="write_file",
            args={"path": "f.txt", "content": "original"},
            trust_level=TrustLevel.GUARDED, session_id="s",
        )
        result = await write.executor(call)
        assert result.success

        # Tamper after write
        (tmp_path / "f.txt").write_text("different", encoding="utf-8")

        verdict = await write.post_validator(call, result)
        assert verdict.passed is False
        assert verdict.signal == "content_mismatch"


@pytest_asyncio.fixture
async def memory_facade(tmp_path):
    store = SQLiteStore(str(tmp_path / "test.db"))
    await store.initialize()
    async with store.connect() as conn:
        semantic = SemanticMemory(conn)
        procedural = ProceduralMemory(conn)
        episodic = EpisodicMemory(conn)
        relational = RelationalMemory(conn)
        facade = MemoryFacade(
            semantic=semantic, procedural=procedural,
            relational=relational, episodic=episodic,
            search=MemorySearch(semantic, procedural),
        )
        yield facade


class TestMemorizeVerifier:
    """memorize post_validator does a roundtrip read back from semantic memory."""

    async def test_passes_on_successful_memorize(self, memory_facade: MemoryFacade) -> None:
        tool = make_memorize_tool(memory_facade)
        call = ToolCall(
            id="c1", tool_name="memorize",
            args={"key": "test:fact", "value": "hello world", "confidence": 0.9},
            trust_level=TrustLevel.GUARDED, session_id="s",
        )
        result = await tool.executor(call)
        assert result.success
        assert result.metadata.get("_memorized_key") == "test:fact"

        verdict = await tool.post_validator(call, result)
        assert verdict.passed is True

    async def test_passes_when_write_was_skipped(
        self, memory_facade: MemoryFacade, tmp_path: Path,
    ) -> None:
        """Governor-skipped writes have no _memorized_key metadata — that's
        a legitimate outcome, not a verifier failure."""
        tool = make_memorize_tool(memory_facade)
        # Fake a skipped-write result (no _memorized_key in metadata)
        call = ToolCall(
            id="c1", tool_name="memorize",
            args={"key": "test:skipped", "value": "v", "confidence": 0.5},
            trust_level=TrustLevel.GUARDED, session_id="s",
        )
        skipped_result = ToolResult(
            call_id="c1", tool_name="memorize",
            success=True, output="Memorize skipped...",
            metadata={},  # no _memorized_key
        )
        verdict = await tool.post_validator(call, skipped_result)
        assert verdict.passed is True

    async def test_fails_when_key_not_retrievable(
        self, memory_facade: MemoryFacade,
    ) -> None:
        """Simulate: memorize claimed success for a key, but semantic.get
        returns None."""
        tool = make_memorize_tool(memory_facade)
        call = ToolCall(
            id="c1", tool_name="memorize",
            args={"key": "phantom:key", "value": "v"},
            trust_level=TrustLevel.GUARDED, session_id="s",
        )
        # Fabricate a result that claims write without actually writing
        fake_result = ToolResult(
            call_id="c1", tool_name="memorize",
            success=True, output="Memorized: 'phantom:key'",
            metadata={"_memorized_key": "phantom:key"},
        )
        verdict = await tool.post_validator(call, fake_result)
        assert verdict.passed is False
        assert verdict.signal == "key_not_found"


class TestSpawnAgentVerifier:
    """spawn_agent post_validator catches hollow successes — 0 turns or
    near-empty output despite success=True (Issue #196, PR #198 review)."""

    async def test_passes_on_healthy_subagent_result(self) -> None:
        result = ToolResult(
            call_id="c1", tool_name="spawn_agent",
            success=True,
            output="[sub-agent sub-abc] 5 turn(s), 3 tool call(s)\n\n"
                   "The analysis found three relevant patterns in the codebase...",
            metadata={
                "subagent_agent_id": "sub-abc",
                "subagent_turns_used": 5,
                "subagent_tool_calls": 3,
                "subagent_output_len": 250,
            },
        )
        verdict = await _verify_spawn_agent(
            _call("spawn_agent", task="analyze"), result,
        )
        assert verdict.passed is True

    async def test_fails_on_zero_turns_executed(self) -> None:
        result = ToolResult(
            call_id="c1", tool_name="spawn_agent",
            success=True,
            output="[sub-agent sub-abc] 0 turn(s), 0 tool call(s)\n\nok",
            metadata={
                "subagent_agent_id": "sub-abc",
                "subagent_turns_used": 0,
                "subagent_tool_calls": 0,
                "subagent_output_len": 2,
            },
        )
        verdict = await _verify_spawn_agent(
            _call("spawn_agent", task="analyze"), result,
        )
        assert verdict.passed is False
        assert verdict.signal == "no_execution"

    async def test_fails_on_suspiciously_short_output(self) -> None:
        result = ToolResult(
            call_id="c1", tool_name="spawn_agent",
            success=True,
            output="[sub-agent sub-abc] 4 turn(s), 2 tool call(s)\n\ndone",
            metadata={
                "subagent_agent_id": "sub-abc",
                "subagent_turns_used": 4,
                "subagent_tool_calls": 2,
                "subagent_output_len": 4,
            },
        )
        verdict = await _verify_spawn_agent(
            _call("spawn_agent", task="analyze"), result,
        )
        assert verdict.passed is False
        assert verdict.signal == "empty_output"

    async def test_passes_when_metadata_absent(self) -> None:
        """Defensive: if metadata is missing for some reason, don't false-
        positive — we'd rather miss a true failure than block legitimate ones."""
        result = ToolResult(
            call_id="c1", tool_name="spawn_agent",
            success=True, output="some output",
            metadata={},
        )
        verdict = await _verify_spawn_agent(
            _call("spawn_agent", task="analyze"), result,
        )
        assert verdict.passed is True


class TestFetchUrlVerifier:
    """fetch_url post_validator catches HTTP-2xx silent failures —
    error-page templates, CDN challenges, thin SPA responses (Issue #199)."""

    async def test_passes_on_normal_article(self) -> None:
        """Happy path: proper article with title + substantial body."""
        output = _fetch_url_output(
            "Introduction to Python Generators",
            "Python generators are a powerful feature that let you iterate "
            "over potentially large datasets without loading them all into "
            "memory. They use the yield keyword to produce values lazily, "
            "pausing execution between each call. This is especially useful "
            "when processing files or streams.",
        )
        result = ToolResult(
            call_id="c1", tool_name="fetch_url",
            success=True, output=output,
        )
        verdict = await _verify_fetch_url(
            _call("fetch_url", url="https://example.com/py"), result,
        )
        assert verdict.passed is True

    async def test_passes_on_json_response(self) -> None:
        """Non-HTML responses (no 'Title:' prefix) are never flagged as
        thin/error — a JSON API returning {\"status\":\"ok\"} is legit."""
        output = sanitize_untrusted_text('{"status":"ok"}')
        result = ToolResult(
            call_id="c1", tool_name="fetch_url",
            success=True, output=output,
        )
        verdict = await _verify_fetch_url(
            _call("fetch_url", url="https://api.example.com/status"), result,
        )
        assert verdict.passed is True

    async def test_fails_on_404_error_page(self) -> None:
        output = _fetch_url_output("404 Not Found", "The page you requested does not exist.")
        result = ToolResult(
            call_id="c1", tool_name="fetch_url",
            success=True, output=output,
        )
        verdict = await _verify_fetch_url(
            _call("fetch_url", url="https://example.com/missing"), result,
        )
        assert verdict.passed is False
        assert verdict.signal == "html_error_page"
        assert "404 Not Found" in (verdict.reason or "")

    async def test_fails_on_access_denied_title(self) -> None:
        output = _fetch_url_output(
            "Access Denied",
            "You do not have permission to view this resource. " * 5,
        )
        result = ToolResult(
            call_id="c1", tool_name="fetch_url",
            success=True, output=output,
        )
        verdict = await _verify_fetch_url(
            _call("fetch_url", url="https://example.com/secret"), result,
        )
        assert verdict.passed is False
        assert verdict.signal == "html_error_page"

    async def test_fails_on_cloudflare_challenge(self) -> None:
        output = _fetch_url_output(
            "Just a moment...",
            "Please wait while we verify your browser.",
        )
        result = ToolResult(
            call_id="c1", tool_name="fetch_url",
            success=True, output=output,
        )
        verdict = await _verify_fetch_url(
            _call("fetch_url", url="https://protected.example.com"), result,
        )
        assert verdict.passed is False
        assert verdict.signal == "html_error_page"

    async def test_passes_on_article_about_error_codes(self) -> None:
        """False-positive guard: an article titled '404 Error Handling in HTTP'
        must NOT match '404 Not Found' pattern — the regex requires a specific
        canonical status-code phrase."""
        output = _fetch_url_output(
            "404 Error Handling in HTTP — Best Practices",
            "When building web applications, handling 404 errors gracefully "
            "is important. This article explores the different strategies "
            "for returning useful responses when a resource is not found. "
            "We'll cover both server-side redirects and client-side fallbacks.",
        )
        result = ToolResult(
            call_id="c1", tool_name="fetch_url",
            success=True, output=output,
        )
        verdict = await _verify_fetch_url(
            _call("fetch_url", url="https://blog.example.com/http-404"), result,
        )
        assert verdict.passed is True

    async def test_passes_on_article_about_not_found_topic(self) -> None:
        """False-positive guard: 'Not Found: How to Handle Missing Resources'
        doesn't match because the pattern requires EXACT 'Not Found' title."""
        output = _fetch_url_output(
            "Not Found: How to Handle Missing Resources in REST APIs",
            "A common pattern in REST API design is returning 404 for missing "
            "resources. This post covers the nuances of when to use 404 vs "
            "other status codes. We'll examine real-world examples from "
            "several popular APIs.",
        )
        result = ToolResult(
            call_id="c1", tool_name="fetch_url",
            success=True, output=output,
        )
        verdict = await _verify_fetch_url(
            _call("fetch_url", url="https://blog.example.com/rest"), result,
        )
        assert verdict.passed is True

    async def test_fails_on_thin_html_content(self) -> None:
        """JS-heavy SPA or minimalist error page: title present but cleaned
        body is nearly empty."""
        output = _fetch_url_output("My SPA", "Loading...")  # 10 chars
        result = ToolResult(
            call_id="c1", tool_name="fetch_url",
            success=True, output=output,
        )
        verdict = await _verify_fetch_url(
            _call("fetch_url", url="https://spa.example.com"), result,
        )
        assert verdict.passed is False
        assert verdict.signal == "thin_html_content"

    async def test_skips_async_mode_results(self) -> None:
        """async_mode returns a job_id — no content to verify yet."""
        result = ToolResult(
            call_id="c1", tool_name="fetch_url",
            success=True,
            output="Submitted as job_xyz. Poll with jobs_status or jobs_await.",
            metadata={"job_id": "job_xyz", "async": True},
        )
        verdict = await _verify_fetch_url(
            _call("fetch_url", url="https://big.example.com"), result,
        )
        assert verdict.passed is True

    async def test_skips_empty_output(self) -> None:
        result = ToolResult(
            call_id="c1", tool_name="fetch_url",
            success=True, output="",
        )
        verdict = await _verify_fetch_url(
            _call("fetch_url", url="https://example.com"), result,
        )
        assert verdict.passed is True


class TestMemorizeDoubleFailureEdgeCase:
    """PR #198 review — pin the correct behavior when both post_validator
    AND rollback_fn fire: the returned result must remain failed, regardless
    of whether rollback itself succeeds."""

    async def test_rollback_success_does_not_mask_semantic_failure(
        self, memory_facade: MemoryFacade,
    ) -> None:
        """Setup: memorize claims success but readback fails (phantom key).
        LifecycleMiddleware should run rollback_fn, which may succeed (or not),
        but the final returned ToolResult must stay success=False."""
        from loom.core.harness.middleware import (
            LifecycleMiddleware, MiddlewarePipeline,
        )
        from loom.core.harness.registry import ToolRegistry

        tool = make_memorize_tool(memory_facade)

        # Wrap executor so it always produces a "phantom" success — the
        # validator will fail, exercising the validator+rollback path.
        async def phantom_executor(call: ToolCall) -> ToolResult:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=True,
                output="Memorized: 'nonexistent:key'",
                metadata={"_memorized_key": "nonexistent:key"},
            )

        reg = ToolRegistry()
        reg.register(tool)
        pipeline = MiddlewarePipeline([LifecycleMiddleware(registry=reg)])

        call = ToolCall(
            id="c1", tool_name="memorize",
            args={"key": "nonexistent:key", "value": "v"},
            trust_level=TrustLevel.GUARDED, session_id="s",
        )
        result = await pipeline.execute(call, phantom_executor)

        # Critical: validator failure → rollback → result stays failed.
        assert result.success is False
        assert result.failure_type == "semantic_failure"
        assert result.metadata.get("rolled_back") is True
        # The rollback's own success/failure must not leak into `success`.
        assert "key_not_found" in str(result.metadata.get("verifier_signal") or "")
