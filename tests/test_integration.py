"""Harness/tool integration, including the retained legacy episodic primitive.

The explicit test callback writes legacy fixtures; LoomSession and subagent
runtime producers no longer write episodic rows (#650 S3 PR-D). Their contract
is tested in test_memory_producers_retired.py. No real LLM API calls are made.
"""

import os
import sys
import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import MagicMock

from loom.core.harness.middleware import (
    ToolCall, ToolResult,
    MiddlewarePipeline, TraceMiddleware, BlastRadiusMiddleware, LogMiddleware,
)
from loom.core.harness.permissions import PermissionContext, TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.memory.episodic import EpisodicEntry, EpisodicMemory
from loom.core.memory.store import SQLiteStore
from loom.platform.cli.tools import (
    make_filesystem_tools,
    make_run_bash_tool,
)
from pathlib import Path as _Path
_tmp_bash_tool = make_run_bash_tool(_Path.cwd())
_run_bash = _tmp_bash_tool.executor


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_workspace(tmp_path):
    return tmp_path


@pytest_asyncio.fixture
async def db_conn(tmp_path):
    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    async with store.connect() as db:
        yield db


def make_call(tool_name, args=None, trust=TrustLevel.SAFE, session_id="sess-int"):
    return ToolCall(
        tool_name=tool_name,
        args=args or {},
        trust_level=trust,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# 1. Pipeline → episodic memory integration
# ---------------------------------------------------------------------------

class TestPipelineToMemory:
    @pytest.mark.asyncio
    async def test_trace_callback_writes_to_episodic(self, db_conn):
        em = EpisodicMemory(db_conn)

        async def on_trace(call, result):
            await em.write(EpisodicEntry(
                session_id=call.session_id,
                event_type="tool_result",
                content=f"Tool '{call.tool_name}' {'ok' if result.success else 'failed'}",
                metadata={"tool_name": call.tool_name, "success": result.success},
            ))

        async def handler(call):
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=True, output="result_data")

        pipeline = MiddlewarePipeline([TraceMiddleware(on_trace=on_trace)])
        await pipeline.execute(make_call("read_file"), handler)
        await pipeline.execute(make_call("list_dir"), handler)

        entries = await em.read_session("sess-int")
        assert len(entries) == 2
        tool_names = [e.metadata["tool_name"] for e in entries]
        assert "read_file" in tool_names
        assert "list_dir" in tool_names

    @pytest.mark.asyncio
    async def test_failed_tool_also_written_to_episodic(self, db_conn):
        em = EpisodicMemory(db_conn)

        async def on_trace(call, result):
            await em.write(EpisodicEntry(
                session_id=call.session_id,
                event_type="tool_result",
                content=f"fail: {result.error}",
                metadata={"success": result.success},
            ))

        async def bad_handler(call):
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="permission denied")

        pipeline = MiddlewarePipeline([TraceMiddleware(on_trace=on_trace)])
        await pipeline.execute(make_call("write_file"), bad_handler)

        entries = await em.read_session("sess-int")
        assert len(entries) == 1
        assert entries[0].metadata["success"] is False

    @pytest.mark.asyncio
    async def test_multiple_sessions_isolated_in_memory(self, db_conn):
        em = EpisodicMemory(db_conn)

        async def on_trace(call, result):
            await em.write(EpisodicEntry(
                session_id=call.session_id, event_type="tool_result",
                content=call.tool_name,
            ))

        async def handler(call):
            return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True)

        pipeline = MiddlewarePipeline([TraceMiddleware(on_trace=on_trace)])
        for sid in ["sessionA", "sessionB", "sessionB"]:
            c = ToolCall(tool_name="t", args={}, trust_level=TrustLevel.SAFE,
                         session_id=sid)
            await pipeline.execute(c, handler)

        assert await em.count_session("sessionA") == 1
        assert await em.count_session("sessionB") == 2


# ---------------------------------------------------------------------------
# 2. Built-in tool executors
# ---------------------------------------------------------------------------

class TestBuiltinTools:
    # Helper: extract executor from workspace-bound filesystem tools
    @staticmethod
    def _fs(tmp_workspace):
        tools = make_filesystem_tools(tmp_workspace)
        return {t.name: t.executor for t in tools}

    @pytest.mark.asyncio
    async def test_read_file_success(self, tmp_workspace):
        f = tmp_workspace / "hello.txt"
        f.write_text("hello loom", encoding="utf-8")
        call = make_call("read_file", args={"path": str(f)})
        result = await self._fs(tmp_workspace)["read_file"](call)
        assert result.success is True
        assert result.output == "hello loom"

    @pytest.mark.asyncio
    async def test_read_file_missing(self, tmp_workspace):
        call = make_call("read_file", args={"path": str(tmp_workspace / "nope.txt")})
        result = await self._fs(tmp_workspace)["read_file"](call)
        assert result.success is False
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_write_file_creates_file(self, tmp_workspace):
        dest = tmp_workspace / "output.txt"
        call = make_call("write_file", args={"path": str(dest), "content": "written!"})
        result = await self._fs(tmp_workspace)["write_file"](call)
        assert result.success is True
        assert dest.read_text() == "written!"

    @pytest.mark.asyncio
    async def test_write_file_creates_parent_dirs(self, tmp_workspace):
        dest = tmp_workspace / "deep" / "nested" / "file.txt"
        call = make_call("write_file", args={"path": str(dest), "content": "deep"})
        result = await self._fs(tmp_workspace)["write_file"](call)
        assert result.success is True
        assert dest.exists()

    @pytest.mark.asyncio
    async def test_list_dir_shows_contents(self, tmp_workspace):
        (tmp_workspace / "a.txt").write_text("a")
        (tmp_workspace / "b.txt").write_text("b")
        (tmp_workspace / "subdir").mkdir()
        call = make_call("list_dir", args={"path": str(tmp_workspace)})
        result = await self._fs(tmp_workspace)["list_dir"](call)
        assert result.success is True
        assert "a.txt" in result.output
        assert "b.txt" in result.output
        assert "subdir" in result.output

    @pytest.mark.asyncio
    async def test_list_dir_missing_path(self, tmp_workspace):
        call = make_call("list_dir", args={"path": str(tmp_workspace / "ghost")})
        result = await self._fs(tmp_workspace)["list_dir"](call)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_run_bash_success(self):
        call = make_call("run_bash", args={"command": "echo hello_loom"})
        result = await _run_bash(call)
        assert result.success is True
        assert "hello_loom" in result.output

    @pytest.mark.asyncio
    async def test_run_bash_nonzero_exit_is_failure(self):
        call = make_call("run_bash", args={"command": "exit 1"})
        result = await _run_bash(call)
        assert result.success is False
        assert result.metadata.get("exit_code") == 1

    @pytest.mark.asyncio
    async def test_run_bash_timeout(self):
        command = f'"{sys.executable}" -c "import time; time.sleep(10)"'
        call = make_call("run_bash", args={"command": command, "timeout": 1})
        result = await _run_bash(call)
        assert result.success is False
        assert "timed out" in result.error.lower()

    def test_builtin_tools_registered_in_list(self, tmp_workspace):
        bash_tool = make_run_bash_tool(tmp_workspace)
        assert bash_tool.name == "run_bash"
        fs_names = {t.name for t in make_filesystem_tools(tmp_workspace)}
        assert fs_names == {"read_file", "write_file", "list_dir", "see_image"}

    def test_trust_levels_correct(self, tmp_workspace):
        from loom.core.harness.permissions import ToolCapability
        fs_trust = {t.name: t.trust_level for t in make_filesystem_tools(tmp_workspace)}
        assert fs_trust["read_file"] == TrustLevel.SAFE
        assert fs_trust["list_dir"] == TrustLevel.SAFE
        assert fs_trust["write_file"] == TrustLevel.GUARDED
        bash_tool = make_run_bash_tool(tmp_workspace)
        assert bash_tool.trust_level == TrustLevel.GUARDED
        assert bash_tool.capabilities & ToolCapability.EXEC


# ---------------------------------------------------------------------------
# 3. Full pipeline: harness guards + memory write
# ---------------------------------------------------------------------------

class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_guarded_tool_blocked_without_confirm(self, db_conn):
        em = EpisodicMemory(db_conn)
        traces = []

        async def on_trace(call, result):
            traces.append(result.success)
            await em.write(EpisodicEntry(
                session_id=call.session_id, event_type="tool_result",
                content=str(result.success),
            ))

        async def deny(call):
            return False   # user denies

        ctx = PermissionContext("s1")
        console = MagicMock()

        pipeline = MiddlewarePipeline([
            LogMiddleware(console),
            TraceMiddleware(on_trace=on_trace),
            BlastRadiusMiddleware(perm_ctx=ctx, confirm_fn=deny),
        ])

        async def handler(call):
            return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True)

        call = ToolCall(tool_name="write_file", args={},
                        trust_level=TrustLevel.GUARDED, session_id="s1")
        result = await pipeline.execute(call, handler)

        assert result.success is False
        # Trace still fired (wraps blast radius)
        assert traces == [False]
        # Written to episodic even on denial
        entries = await em.read_session("s1")
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_full_happy_path_read_write_cycle(self, tmp_workspace, db_conn):
        """read_file → write_file → verify episodic has 2 entries."""
        src = tmp_workspace / "input.txt"
        src.write_text("original content")
        dst = tmp_workspace / "output.txt"

        em = EpisodicMemory(db_conn)

        async def on_trace(call, result):
            await em.write(EpisodicEntry(
                session_id="full-test",
                event_type="tool_result",
                content=f"Tool '{call.tool_name}' → {'ok' if result.success else 'fail'}",
                metadata={"tool_name": call.tool_name, "success": result.success},
            ))

        async def always_allow(call):
            return True

        ctx = PermissionContext("full-test")
        ctx.authorize("read_file")

        pipeline = MiddlewarePipeline([
            TraceMiddleware(on_trace=on_trace),
            BlastRadiusMiddleware(perm_ctx=ctx, confirm_fn=always_allow),
        ])

        registry = ToolRegistry()
        for tool in make_filesystem_tools(tmp_workspace):
            registry.register(tool)

        # Step 1: read
        read_call = ToolCall(tool_name="read_file", args={"path": str(src)},
                             trust_level=TrustLevel.SAFE, session_id="full-test")
        read_result = await pipeline.execute(read_call, registry.get("read_file").executor)
        assert read_result.success is True

        # Step 2: write (content from read)
        write_call = ToolCall(
            tool_name="write_file",
            args={"path": str(dst), "content": read_result.output + " [modified]"},
            trust_level=TrustLevel.GUARDED,
            session_id="full-test",
        )
        write_result = await pipeline.execute(write_call, registry.get("write_file").executor)
        assert write_result.success is True
        assert dst.read_text() == "original content [modified]"

        # Both recorded in episodic
        entries = await em.read_session("full-test")
        assert len(entries) == 2
