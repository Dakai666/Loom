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
    _verify_run_bash,
    make_filesystem_tools,
    make_memorize_tool,
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
